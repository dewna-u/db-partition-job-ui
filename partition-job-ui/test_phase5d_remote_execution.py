"""Phase 5D — target-aware scheduler + safe remote execution (unit + contract).

No real database, network, AWS or Datadog: target connections, the control
ledger and catalog checks are faked. Real two-database behaviour (control +
target COMMIT / ROLLBACK, ledger, history, schedule finalization, restart
recovery, routing, isolation, concurrency) is exercised on throwaway
PostgreSQL databases by scripts/disposable_db/run_disposable_db_test.sh
(stage 11, scripts/disposable_db/remote_execution_check.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from api.main import app
from database_targets import DatabaseTarget
from scheduler_backend import remote_execution as rx
from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.remote_execution import (
    FAILED,
    NOT_DISPATCHED,
    SUCCEEDED,
    UNKNOWN,
    ControlLedger,
    RemoteClaim,
    TargetOutcome,
    run_target_transaction,
    sanitize_error,
)
from scheduler_backend.retry_policy import HANDLED_STATUSES
from scheduler_backend.scheduler import PartitionScheduler
from scheduler_backend.target_runtime import (
    FrameworkCheckCache,
    SlotManager,
    SlotTimeout,
    TargetAvailability,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
MIG = os.path.join(ROOT, "sql", "migrations")
M03 = os.path.join(MIG, "20261117_03_remote_execution_ledger.sql")
M04 = os.path.join(MIG, "20261117_04_remote_execution_functions.sql")
T = datetime(2026, 11, 17, 2, 0)
ENV = {"PARTOPS_TARGET_DB02_DB_USER": "u2", "PARTOPS_TARGET_DB02_DB_PASSWORD": "p2",
       "DB_HOST": "control.local", "DB_USER": "cu", "DB_PASSWORD": "cp",
       "PARTITION_DB_SECRET_PROVIDER": "env"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def ctl(**kw):
    base = dict(target_id=1, target_key="db01", display_name="DB-01", database_name="partops",
                is_control_database=True)
    base.update(kw)
    return DatabaseTarget(**base)


def remote(**kw):
    base = dict(target_id=2, target_key="db02", display_name="DB-02", database_name="oms_db02",
                host="db02.internal", port=5432, credential_provider="env")
    base.update(kw)
    return DatabaseTarget(**base)


def claim(**kw):
    base = dict(attempt_id=41, job_id=5, database_target_id=2, target_key="db02",
                operation="CREATE", table_schema="oms", table_name="orders",
                partition_unit="day", partition_period=1, create_drop_interval="P1M",
                db_config={"lock_timeout": "30s"})
    base.update(kw)
    return RemoteClaim(**base)


class SqlError(Exception):
    def __init__(self, msg, sqlstate):
        super().__init__(msg)
        self.sqlstate = sqlstate


class FakeTargetConn:
    """Target connection double: identity, lock, set_config, worker, commit."""

    def __init__(self, dbname="oms_db02", lock=True, worker_exc=None, commit_exc=None,
                 rollback_exc=None, break_on_worker=False):
        self.dbname, self.lock = dbname, lock
        self.worker_exc, self.commit_exc, self.rollback_exc = worker_exc, commit_exc, rollback_exc
        self.break_on_worker = break_on_worker
        self.statements = []
        self.commits = self.rollbacks = 0
        self.closed = self.broken = False

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                conn.statements.append((sql, params))
                self.sql = sql
                if "_table_partition(" in sql and conn.worker_exc is not None:
                    if conn.break_on_worker:
                        conn.broken = True
                    raise conn.worker_exc

            def fetchone(self):
                if "current_database" in self.sql:
                    return (conn.dbname,)
                if "pg_try_advisory_xact_lock" in self.sql:
                    return (conn.lock,)
                return None

        return Cur()

    def commit(self):
        if self.commit_exc is not None:
            raise self.commit_exc
        self.commits += 1

    def rollback(self):
        if self.rollback_exc is not None and self.statements and any(
                "_table_partition(" in s for s, _ in self.statements):
            raise self.rollback_exc
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def worker_calls(self):
        return [s for s, _ in self.statements if "_table_partition(" in s]


class FakeLedger:
    def __init__(self, dispatch_state="DISPATCHING", fail=()):
        self.calls = []
        self.dispatch_state = dispatch_state
        self.fail = set(fail)

    def _hit(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail:
            raise RuntimeError("control DB down")

    def ledger(self):
        def mark(a):
            self._hit("mark_dispatching", a)
            return self.dispatch_state
        return ControlLedger(
            mark_dispatching=mark,
            record_success=lambda a, d: self._hit("record_success", a, d),
            record_failure=lambda a, c, s, d: self._hit("record_failure", a, c, s, d),
            mark_unknown=lambda a, s: self._hit("mark_unknown", a, s),
        )

    def names(self):
        return [c[0] for c in self.calls]


def run(conn, led, c=None, target=None):
    with patch.dict(os.environ, ENV):
        return run_target_transaction(target or remote(), c or claim(), led.ledger(),
                                      connect=lambda **k: conn)


# ---------------------------------------------------------------------------
# 1. Target transaction classification (the distributed-transaction rule)
# ---------------------------------------------------------------------------
class TargetTransactionTests(unittest.TestCase):
    def test_success_commits_then_records_success(self):
        conn, led = FakeTargetConn(), FakeLedger()
        out = run(conn, led)
        self.assertEqual(out.kind, SUCCEEDED)
        self.assertEqual(led.names(), ["mark_dispatching", "record_success"])
        self.assertEqual(conn.commits, 1)
        self.assertEqual(len(conn.worker_calls()), 1)
        self.assertTrue(conn.closed)

    def test_connect_failure_is_not_dispatched_and_retryable(self):
        led = FakeLedger()
        with patch.dict(os.environ, ENV):
            out = run_target_transaction(remote(), claim(), led.ledger(),
                                         connect=MagicMock(side_effect=OSError("refused")))
        self.assertEqual((out.kind, out.error_class), (NOT_DISPATCHED, "UNREACHABLE"))
        self.assertEqual(led.names(), ["record_failure"])  # never DISPATCHING

    def test_identity_mismatch_never_dispatches(self):
        conn, led = FakeTargetConn(dbname="some_other_db"), FakeLedger()
        out = run(conn, led)
        self.assertEqual((out.kind, out.error_class), (NOT_DISPATCHED, "WRONG_DATABASE"))
        self.assertEqual(conn.worker_calls(), [])

    def test_table_lock_busy_is_not_dispatched(self):
        conn, led = FakeTargetConn(lock=False), FakeLedger()
        out = run(conn, led)
        self.assertEqual((out.kind, out.error_class), (NOT_DISPATCHED, "TARGET_LOCKED"))
        self.assertNotIn("mark_dispatching", led.names())
        self.assertEqual(conn.worker_calls(), [])

    def test_worker_error_with_proven_rollback_is_known_failure(self):
        conn = FakeTargetConn(worker_exc=SqlError("partition exists", "P0001"))
        led = FakeLedger()
        out = run(conn, led)
        self.assertEqual((out.kind, out.error_class), (FAILED, "WORKER_ERROR"))
        self.assertEqual(led.names(), ["mark_dispatching", "record_failure"])
        self.assertEqual(conn.commits, 0)
        self.assertGreaterEqual(conn.rollbacks, 1)

    def test_statement_timeout_on_healthy_session_is_known_failure(self):
        conn = FakeTargetConn(worker_exc=SqlError("canceling statement due to statement timeout", "57014"))
        self.assertEqual(run(conn, FakeLedger()).kind, FAILED)

    def test_connection_lost_during_worker_is_unknown(self):
        conn = FakeTargetConn(worker_exc=SqlError("server closed the connection", "08006"),
                              break_on_worker=True)
        led = FakeLedger()
        out = run(conn, led)
        self.assertEqual(out.kind, UNKNOWN)
        self.assertEqual(led.names(), ["mark_dispatching", "mark_unknown"])
        self.assertNotIn("record_failure", led.names())

    def test_error_without_sqlstate_is_unknown(self):
        out = run(FakeTargetConn(worker_exc=RuntimeError("socket gone")), FakeLedger())
        self.assertEqual(out.kind, UNKNOWN)

    def test_rollback_not_proven_is_unknown(self):
        conn = FakeTargetConn(worker_exc=SqlError("boom", "P0001"), rollback_exc=OSError("lost"))
        self.assertEqual(run(conn, FakeLedger()).kind, UNKNOWN)

    def test_commit_error_is_unknown_never_failed(self):
        for exc in (OSError("connection reset during COMMIT"), SqlError("could not commit", "40001")):
            conn, led = FakeTargetConn(commit_exc=exc), FakeLedger()
            out = run(conn, led)
            self.assertEqual(out.kind, UNKNOWN, exc)
            self.assertEqual(led.names(), ["mark_dispatching", "mark_unknown"])

    def test_control_unavailable_before_dispatch_never_calls_worker(self):
        conn, led = FakeTargetConn(), FakeLedger(fail={"mark_dispatching"})
        out = run(conn, led)
        self.assertEqual((out.kind, out.error_class), (NOT_DISPATCHED, "CONTROL_UNAVAILABLE"))
        self.assertEqual(conn.worker_calls(), [])
        self.assertEqual(conn.commits, 0)

    def test_attempt_not_prepared_never_calls_worker(self):
        conn, led = FakeTargetConn(), FakeLedger(dispatch_state="FINALIZED")
        out = run(conn, led)
        self.assertEqual((out.kind, out.error_class), (NOT_DISPATCHED, "STATE_CONFLICT"))
        self.assertEqual(conn.worker_calls(), [])

    def test_success_but_control_write_fails_keeps_success(self):
        conn, led = FakeTargetConn(), FakeLedger(fail={"record_success"})
        out = run(conn, led)
        self.assertEqual(out.kind, SUCCEEDED)
        self.assertFalse(out.ledger_recorded)
        self.assertEqual(out.ledger_action, "success")
        self.assertEqual(len(conn.worker_calls()), 1)

    def test_unallowlisted_config_rejected_before_connecting(self):
        connect = MagicMock()
        out = run_target_transaction(remote(), claim(db_config={"search_path": "evil"}),
                                     FakeLedger().ledger(), connect=connect)
        self.assertEqual((out.kind, out.error_class), (NOT_DISPATCHED, "CONFIG_REJECTED"))
        connect.assert_not_called()

    def test_sql_is_parameterised_and_session_local(self):
        conn = FakeTargetConn()
        run(conn, FakeLedger(), c=claim(operation="DROP", table_name="orders; DROP TABLE x"))
        texts = [s for s, _ in conn.statements]
        lock_sql = [s for s in texts if "pg_try_advisory_xact_lock(87421032" in s]
        self.assertEqual(len(lock_sql), 1)
        cfg = [(s, p) for s, p in conn.statements if "set_config" in s]
        self.assertEqual(cfg[0][1], ("lock_timeout", "30s"))
        self.assertIn("true)", cfg[0][0])  # is_local
        worker = [(s, p) for s, p in conn.statements if "_table_partition(" in s][0]
        self.assertIn("mubasher_oms.drop_any_table_partition(", worker[0])
        self.assertNotIn("orders", worker[0])           # identifiers only as parameters
        self.assertEqual(worker[1][1], "orders; DROP TABLE x")

    def test_statements_survive_real_psycopg_parameter_parsing(self):
        # Fakes do not parse placeholders; psycopg does ('%' must be '%%').
        from psycopg._queries import PostgresQuery
        from psycopg.adapt import Transformer

        def converted(stmt, params):
            pq = PostgresQuery(Transformer())
            pq.convert(stmt, params)  # raises ProgrammingError on a stray '%'
            return pq.query

        for stmt, params in ((rx.TARGET_LOCK_SQL, ("oms", "orders")),
                             (rx._worker_sql("mubasher_oms", "CREATE"), rx._worker_params(claim())),
                             (rx._worker_sql("mubasher_oms", "DROP"), rx._worker_params(claim(operation="DROP"))),
                             ("SELECT set_config(%s, %s, true)", ("lock_timeout", "30s"))):
            self.assertNotIn(b"%%", converted(stmt, params))
        self.assertIn(b"% 2147483647", converted(rx.TARGET_LOCK_SQL, ("a", "b")))

    def test_routing_target_mismatch_refused(self):
        led = FakeLedger()
        out = run(FakeTargetConn(), led, c=claim(database_target_id=3))
        self.assertEqual(out.error_class, "TARGET_MISMATCH")

    def test_sanitize_error_redacts(self):
        exc = SqlError('password authentication failed for user "x" password=hunter2', "28P01")
        text = sanitize_error(exc)
        self.assertNotIn("hunter2", text)
        self.assertIn("28P01", text)


# ---------------------------------------------------------------------------
# 2. Scheduler: remote flow, retry, unknown, finalization, recovery
# ---------------------------------------------------------------------------
def make_scheduler(global_max=2, db01=1, db02=1):
    s = PartitionScheduler(SchedulerConfig(max_concurrent_jobs=global_max))
    s._set_control_target(ctl(max_concurrent_jobs=db01))
    s._targets = {1: ctl(max_concurrent_jobs=db01), 2: remote(max_concurrent_jobs=db02)}
    s._slots.set_limit(2, db02)
    return s


def remote_job(job_id=5, expected=T):
    return ScheduledJob(job_id, expected, -1.0, "J", True, database_target_id=2,
                        target_key="db02", is_control_target=False)


CLAIMED = {"decision": "CLAIMED", "attempt_id": 41, "attempt_state": "PREPARED",
           "database_target_id": 2, "target_key": "db02", "operation": "CREATE",
           "table_schema": "oms", "table_name": "orders", "partition_unit": "day",
           "partition_period": 1, "create_drop_interval": "P1M", "db_config": {},
           "failure_count": 0, "job_name": "J"}


class RemoteSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.s = make_scheduler()
        self.job = remote_job()
        self.s._occurrences = {self.job.key: self.job}
        self.p = {}
        patches = {
            "remote_claim": MagicMock(return_value=dict(CLAIMED)),
            "run_target_transaction": MagicMock(return_value=TargetOutcome(SUCCEEDED, duration_ms=12)),
            "remote_finalize": MagicMock(return_value={"decision": "FINALIZED", "result": "SUCCESS"}),
            "remote_record_failure": MagicMock(return_value={}),
            "check_target_health": MagicMock(return_value={"state": "HEALTHY_WITH_WARNINGS"}),
            "execute_scheduled_job": MagicMock(side_effect=AssertionError("local path used for remote job")),
            "reconcile_occurrence": MagicMock(return_value={"status": "MISSED_RESCHEDULED"}),
        }
        for name, mock in patches.items():
            p = patch("scheduler_backend.scheduler." + name, mock)
            self.p[name] = p.start()
            self.addCleanup(p.stop)

    def run1(self):
        asyncio.run(self.s._execute_one(self.job.job_id, self.job.expected_run_time))

    def test_success_finalizes_on_control_only(self):
        self.run1()
        self.assertEqual(self.p["run_target_transaction"].call_count, 1)
        self.p["remote_finalize"].assert_called_once()
        self.assertEqual(self.s.status.last_execution_result, "EXECUTED")
        self.assertIn(self.job.key, self.s._handled_occurrences._items)

    def test_known_failure_retries_bounded_then_reconciles(self):
        self.p["run_target_transaction"].return_value = TargetOutcome(FAILED, error_class="WORKER_ERROR")
        for _ in range(self.s.config.retry_max_attempts):
            self.run1()
        self.assertEqual(self.p["run_target_transaction"].call_count, self.s.config.retry_max_attempts)
        self.p["remote_finalize"].assert_not_called()
        self.assertTrue(self.s._retry.is_exhausted(self.job.key))
        self.run1()  # exhausted → DB reconcile (RETRY_EXHAUSTED), never the target
        self.p["reconcile_occurrence"].assert_called_once()
        self.assertEqual(self.p["reconcile_occurrence"].call_args[0][3], "RETRY_EXHAUSTED")
        self.assertEqual(self.p["run_target_transaction"].call_count, self.s.config.retry_max_attempts)

    def test_unknown_outcome_is_never_retried(self):
        self.p["run_target_transaction"].return_value = TargetOutcome(UNKNOWN, error_class="OUTCOME_UNKNOWN")
        self.run1()
        self.assertEqual(self.s.status.last_execution_result, "REMOTE_OUTCOME_UNKNOWN")
        self.assertIn("REMOTE_OUTCOME_UNKNOWN", HANDLED_STATUSES)
        self.assertIsNone(self.s._retry.get(self.job.key))
        for _ in range(5):
            self.run1()
        self.assertEqual(self.p["run_target_transaction"].call_count, 1)

    def test_finalization_failure_retries_finalization_only(self):
        self.p["remote_finalize"].side_effect = [RuntimeError("control down"),
                                                 {"decision": "FINALIZED", "result": "SUCCESS"}]
        self.run1()
        self.assertEqual(self.s.status.last_execution_result, "REMOTE_FINALIZE_PENDING")
        self.assertIn(41, self.s._finalize_pending)
        self.s._finalize_pending[41] = 0.0
        asyncio.run(self.s._drain_pending())
        self.assertEqual(self.p["remote_finalize"].call_count, 2)
        self.assertNotIn(41, self.s._finalize_pending)
        self.assertEqual(self.p["run_target_transaction"].call_count, 1)  # NO DDL retry

    def test_success_recorded_late_is_finalized_without_ddl(self):
        self.p["run_target_transaction"].return_value = TargetOutcome(
            SUCCEEDED, duration_ms=5, ledger_recorded=False, ledger_action="success")
        self.run1()
        self.assertIn(41, self.s._ledger_pending)
        with patch("scheduler_backend.scheduler.remote_record_success", return_value="TARGET_SUCCEEDED") as rs:
            asyncio.run(self.s._drain_pending())
            rs.assert_called_once()
        self.p["remote_finalize"].assert_called_once()
        self.assertEqual(self.p["run_target_transaction"].call_count, 1)

    def test_claim_finalize_pending_finalizes_without_dispatch(self):
        self.p["remote_claim"].return_value = {"decision": "FINALIZE_PENDING", "attempt_id": 41}
        self.run1()
        self.p["run_target_transaction"].assert_not_called()
        self.p["remote_finalize"].assert_called_once()

    def test_claim_needs_review_or_dispatching_never_dispatches(self):
        for decision in ("NEEDS_REVIEW", "ALREADY_DISPATCHING", "ALREADY_FINALIZED"):
            self.s._handled_occurrences = type(self.s._handled_occurrences)()
            self.p["remote_claim"].return_value = {"decision": decision, "attempt_id": 41}
            self.run1()
        self.p["run_target_transaction"].assert_not_called()

    def test_preflight_failure_blocks_execution(self):
        self.p["check_target_health"].return_value = {"state": "FRAMEWORK_INCOMPATIBLE"}
        self.run1()
        self.p["run_target_transaction"].assert_not_called()
        self.p["remote_record_failure"].assert_called_once()
        self.assertEqual(self.s.status.last_execution_result, "FAILED_TARGET_PREFLIGHT")

    def test_preflight_cached_between_attempts(self):
        self.run1()
        job2 = remote_job(job_id=6)
        self.s._occurrences[job2.key] = job2
        asyncio.run(self.s._execute_one(6, T))
        self.assertEqual(self.p["check_target_health"].call_count, 1)

    def test_unreachable_target_cools_down_and_skips_dispatch(self):
        self.p["run_target_transaction"].return_value = TargetOutcome(NOT_DISPATCHED, error_class="UNREACHABLE")
        self.run1()
        self.assertFalse(self.s._availability.available(2))
        self.assertEqual(self.s.status.last_execution_result, "FAILED_TARGET_UNAVAILABLE")

    def test_historical_remote_occurrence_reconciled_not_executed(self):
        self.s._ownership.observe(self.job.key, -3600.0)
        self.run1()
        self.p["run_target_transaction"].assert_not_called()
        self.p["reconcile_occurrence"].assert_called_once()
        self.assertEqual(self.p["reconcile_occurrence"].call_args[0][3], "MISSED_SCHEDULED_RUN")

    def test_control_job_keeps_local_path(self):
        local = ScheduledJob(9, T, -1.0, "L", True, database_target_id=1, target_key="db01",
                             is_control_target=True)
        self.s._occurrences[local.key] = local
        self.p["execute_scheduled_job"].side_effect = None
        self.p["execute_scheduled_job"].return_value = {"status": "EXECUTED", "is_create": True,
                                                        "next_run_time": datetime(2026, 11, 18, 2)}
        asyncio.run(self.s._execute_one(9, T))
        self.p["execute_scheduled_job"].assert_called_once()
        self.p["remote_claim"].assert_not_called()


class RestartRecoveryTests(unittest.TestCase):
    def test_recovery_finalizes_succeeded_and_never_redispatches(self):
        s = make_scheduler()
        rows = [{"attempt_id": 9, "previous_state": "TARGET_SUCCEEDED", "action": "FINALIZE"},
                {"attempt_id": 10, "previous_state": "DISPATCHING", "action": "MARKED_UNKNOWN"},
                {"attempt_id": 11, "previous_state": "PREPARED", "action": "RESUMABLE"}]
        with patch("scheduler_backend.scheduler.remote_recover_attempts", return_value=rows), \
                patch("scheduler_backend.scheduler.remote_finalize",
                      return_value={"decision": "FINALIZED", "result": "SUCCESS"}) as fin, \
                patch("scheduler_backend.scheduler.run_target_transaction") as rt:
            asyncio.run(s._recover_remote_attempts())
            self.assertEqual(set(s._finalize_pending), {9})
            asyncio.run(s._drain_pending())
        fin.assert_called_once_with(s.config, 9)
        rt.assert_not_called()
        self.assertEqual(s.status.extra["recovered_attempts"], 3)

    def test_recovery_failure_blocks_gate(self):
        from test_framework_phase5b import complete_snapshot
        import framework_manifest as fm

        m = fm.load_manifest()
        s = PartitionScheduler(SchedulerConfig(framework_check_enabled=True))
        with patch("scheduler_backend.scheduler.check_framework_compatibility",
                   return_value=fm.scheduler_gate(m, complete_snapshot(m))), \
                patch("scheduler_backend.scheduler.fetch_control_target", return_value=ctl()), \
                patch("scheduler_backend.scheduler.remote_recover_attempts", side_effect=RuntimeError("x")):
            ok = asyncio.run(s._ensure_framework())
        self.assertFalse(ok)
        self.assertEqual(s.status.extra["framework_failures"], ["remote attempt recovery failed"])


# ---------------------------------------------------------------------------
# 3. Global + per-target concurrency
# ---------------------------------------------------------------------------
class SlotManagerTests(unittest.TestCase):
    def test_two_targets_run_simultaneously_global_2(self):
        async def go():
            m = SlotManager(2)
            m.set_limit(1, 1)
            m.set_limit(2, 1)
            await m.acquire(1)
            await m.acquire(2)
            self.assertEqual(m.global_running, 2)
            with self.assertRaises(SlotTimeout):
                await m.acquire(1, timeout=0.05)
            await m.release(1)
            await m.acquire(1, timeout=0.5)
        asyncio.run(go())

    def test_limits_global3_db01_1_db02_2(self):
        async def go():
            m = SlotManager(3)
            m.set_limit(1, 1)
            m.set_limit(2, 2)
            await m.acquire(2)
            await m.acquire(2)
            with self.assertRaises(SlotTimeout):
                await m.acquire(2, timeout=0.05)
            await m.acquire(1)
            self.assertEqual((m.running(1), m.running(2), m.global_running), (1, 2, 3))
            with self.assertRaises(SlotTimeout):
                await m.acquire(1, timeout=0.05)
        asyncio.run(go())

    def test_saturated_target_does_not_consume_global_slots(self):
        async def go():
            m = SlotManager(2)
            m.set_limit(2, 1)
            m.set_limit(1, 2)
            await m.acquire(2)                       # db02 saturated
            waiters = [asyncio.create_task(m.acquire(2)) for _ in range(3)]
            await asyncio.sleep(0.05)
            self.assertEqual(m.waiting(2), 3)
            await m.acquire(1, timeout=0.2)          # global slot still available for db01
            self.assertEqual(m.global_running, 2)
            await m.release(2)
            await asyncio.sleep(0.05)
            # Freed db02 slot goes to the next db02 waiter (global: db01 + db02).
            self.assertEqual((m.running(2), m.running(1), m.global_running), (1, 1, 2))
            self.assertEqual(m.waiting(2), 2)
            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        asyncio.run(go())

    def test_scheduler_enforces_global_and_target_limits(self):
        s = make_scheduler(global_max=2, db01=1, db02=1)
        jobs = [ScheduledJob(i, T, -1.0, "J", True, database_target_id=tid,
                             is_control_target=(tid == 1)) for i, tid in ((1, 1), (2, 1), (3, 2), (4, 2))]
        s._occurrences = {j.key: j for j in jobs}
        active = {1: 0, 2: 0}
        peak = {1: 0, 2: 0, "global": 0, "both": False}

        async def fake_execute(job_id, expected):
            tid = s._occurrences[(job_id, expected)].database_target_id
            active[tid] += 1
            peak[tid] = max(peak[tid], active[tid])
            peak["global"] = max(peak["global"], active[1] + active[2])
            peak["both"] = peak["both"] or (active[1] and active[2])
            await asyncio.sleep(0.05)
            active[tid] -= 1

        async def go():
            with patch.object(s, "_execute_one", side_effect=fake_execute):
                for j in jobs:
                    s._active_keys.add(j.key)
                await asyncio.gather(*(s._run_job_with_slot(j.job_id, T) for j in jobs))
        asyncio.run(go())
        self.assertEqual((peak[1], peak[2], peak["global"]), (1, 1, 2))
        self.assertTrue(peak["both"])  # both targets progressed concurrently


class AvailabilityAndCacheTests(unittest.TestCase):
    def test_capped_exponential_cooldown(self):
        a = TargetAvailability(15, 300)
        delays = [a.record_failure(2, "UNREACHABLE", now=0.0) for _ in range(8)]
        self.assertEqual(delays[:5], [15, 30, 60, 120, 240])
        self.assertEqual(max(delays), 300)
        self.assertTrue(a.available(1, now=1.0))      # other targets unaffected
        a.record_success(2)
        self.assertTrue(a.available(2, now=1.0))

    def test_cooling_target_occurrence_requeued_while_healthy_target_dispatched(self):
        s = make_scheduler()
        j1 = ScheduledJob(1, T, -1.0, "A", True, database_target_id=1, is_control_target=True)
        j2 = remote_job(job_id=2)
        s._occurrences = {j1.key: j1, j2.key: j2}
        s._availability.record_failure(2, "UNREACHABLE")
        started = []

        async def fake_run(job_id, expected):
            started.append(job_id)

        async def go():
            now = time.monotonic()
            s.queue.push(1, T, now - 1)
            s.queue.push(2, T, now - 1)
            with patch.object(s, "_run_job_with_slot", side_effect=fake_run):
                s._spawn_due_jobs(now)
                await asyncio.sleep(0.01)
        asyncio.run(go())
        self.assertEqual(started, [1])
        self.assertIn(j2.key, {(jid, exp) for _d, jid, exp in [s.queue.peek()] if s.queue.peek()})

    def test_preflight_cache_bounded_and_invalidatable(self):
        c = FrameworkCheckCache(ttl_seconds=10, max_entries=2)
        c.put(1, True, "HEALTHY", now=0)
        c.put(2, True, "HEALTHY", now=1)
        c.put(3, True, "HEALTHY", now=2)
        self.assertIsNone(c.get(1, now=3))          # evicted (bounded)
        self.assertIsNotNone(c.get(2, now=3))
        self.assertIsNone(c.get(2, now=20))         # expired
        c.invalidate(3)
        self.assertIsNone(c.get(3, now=3))


# ---------------------------------------------------------------------------
# 4. Status + observability (no secrets, low cardinality)
# ---------------------------------------------------------------------------
class StatusAndMetricsTests(unittest.TestCase):
    def test_status_exposes_per_target_view_without_secrets(self):
        s = make_scheduler(global_max=4, db01=2, db02=1)
        s._targets[2] = remote(max_concurrent_jobs=1, credential_provider="aws",
                               secret_id="partops/hub/db02", aws_region="eu-west-1")
        s._update_concurrency_status()
        d = s.status.as_dict()
        self.assertEqual(d["global_max"], 4)
        self.assertEqual(set(d["targets"]), {"db01", "db02"})
        self.assertEqual(d["targets"]["db01"]["max_concurrent_jobs"], 2)
        for key in ("active_jobs", "waiting_jobs", "available", "cooldown_seconds_remaining"):
            self.assertIn(key, d["targets"]["db02"])
        text = json.dumps(d, default=str)
        for secret in ("partops/hub/db02", "db02.internal", "password"):
            self.assertNotIn(secret, text)

    def test_remote_metrics_are_low_cardinality(self):
        import observability

        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true", "PARTOPS_ENVIRONMENT": "development"}), \
                patch("observability._send", side_effect=lambda p: sent.append(p.decode())):
            observability.emit_remote_attempt(outcome="unknown", target="db02")
            observability.emit_remote_attempt(outcome="failed", target="db02")
            observability.emit_remote_finalization_retry(target="db02")
            observability.emit_target_slots(target="db02", active=1, waiting=2, limit=1, available=False)
            observability.emit_remote_unknown_outcomes(3)
            observability.emit_job_execution(status="REMOTE_OUTCOME_UNKNOWN", target="db02")
        out = "\n".join(sent)
        for metric in ("remote.execution_attempt", "remote.outcome_unknown", "remote.known_failure",
                       "remote.finalization_retry", "target.active_jobs:1", "target.waiting_jobs:2",
                       "target.available:0", "remote.unknown_outcomes:3", "status:remote_outcome_unknown"):
            self.assertIn(metric, out)
        tags = ",".join(line.split("|#", 1)[1] for line in out.splitlines() if "|#" in line)
        for forbidden in ("job_id:", "attempt_id:", "attempt:", "table:", "schema:", "host:",
                          "secret", "orders"):
            self.assertNotIn(forbidden, tags)


# ---------------------------------------------------------------------------
# 5. Static guarantees: stubs isolated; SQL contracts
# ---------------------------------------------------------------------------
STUB_MARKERS = ("TEST-ONLY STUB", "partops_test.", "stub_work")


class StubIsolationTests(unittest.TestCase):
    def test_stubs_only_in_disposable_assets(self):
        stub_dir = os.path.join(ROOT, "sql", "tests", "disposable", "stubs")
        for name in os.listdir(stub_dir):
            text = _read(os.path.join(stub_dir, name))
            self.assertIn("DISPOSABLE TEST DATABASE ONLY", text)
            self.assertIn("NOT PRODUCTION", text)

    def test_stubs_absent_from_production_assets(self):
        checked = []
        for base, dirs, files in os.walk(ROOT):
            rel = os.path.relpath(base, ROOT).replace("\\", "/")
            dirs[:] = [d for d in dirs if d not in (".venv", "node_modules", ".next", "frontend", "__pycache__", ".git")]
            if rel.startswith("sql/tests") or rel.startswith("scripts/disposable_db"):
                continue
            for f in files:
                if not (f.endswith(".sql") or f.endswith(".json") or f.endswith(".py")):
                    continue
                if f.startswith("test_"):
                    continue
                text = _read(os.path.join(base, f))
                checked.append(f)
                for marker in STUB_MARKERS:
                    self.assertNotIn(marker, text, "{}/{}".format(rel, f))
        self.assertIn("framework_manifest.json", checked)
        self.assertIn("20261117_04_remote_execution_functions.sql", checked)

    def test_no_legacy_worker_body_defined_in_migrations(self):
        for name in sorted(os.listdir(MIG)):
            text = _read(os.path.join(MIG, name))
            for legacy in ("create_any_table_partition", "drop_any_table_partition",
                           "cron_to_interval_or_next_run", "insert_data_to_partition_job_table"):
                self.assertNotRegex(text, r"(?i)CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+mubasher_oms\." + legacy, name)


class SqlContractTests(unittest.TestCase):
    def setUp(self):
        self.m03, self.m04 = _read(M03), _read(M04)

    def _fn(self, text, name):
        start = text.index("CREATE OR REPLACE FUNCTION mubasher_oms." + name + "(")
        end = text.index("$function$;", text.index("AS $function$", start))
        return text[start:end]

    def test_no_distributed_transaction_machinery(self):
        for text in (self.m03, self.m04, _read(os.path.join(ROOT, "scheduler_backend", "remote_execution.py"))):
            executable = re.sub(r"--[^\n]*", "", text)
            for forbidden in ("PREPARE TRANSACTION", "COMMIT PREPARED", "dblink", "postgres_fdw", "tpc_begin"):
                self.assertNotIn(forbidden, executable)

    def test_ledger_has_no_secrets_and_no_app_writes(self):
        table = self.m03[self.m03.index("CREATE TABLE IF NOT EXISTS mubasher_oms.partops_execution_attempt"):]
        table = table[: table.index(chr(10) + ");")]
        table = re.sub(r"--[^\n]*", "", table)  # column definitions only
        self.assertNotRegex(table, r"(?i)\b(password|secret_id|secret_value|access_key|host)\b")
        grants = re.findall(r"GRANT ([A-Z, ]+) ON mubasher_oms\.partops_execution_attempt TO", self.m03)
        self.assertEqual({g.strip() for g in grants}, {"SELECT"})
        self.assertIn("partops_execution_attempt_occurrence_uq", self.m03)
        self.assertRegex(self.m03, r"\(database_target_id, job_id, expected_run_time\)\s+WHERE execution_kind = 'SCHEDULED'")

    def test_state_machine_guard(self):
        guard = self._fn(self.m03, "fn_partops_execution_attempt_guard")
        self.assertIn("(OLD.state = 'DISPATCHING'      AND NEW.state IN ('TARGET_SUCCEEDED', 'TARGET_FAILED', 'OUTCOME_UNKNOWN'))", guard)
        self.assertIn("(OLD.state = 'OUTCOME_UNKNOWN'  AND NEW.state = 'FINALIZED')", guard)
        # An unknown outcome can never go back to PREPARED (= re-dispatch).
        self.assertNotRegex(guard, r"OLD.state = 'OUTCOME_UNKNOWN'\s+AND NEW.state IN \([^)]*PREPARED")
        self.assertNotRegex(guard, r"OLD.state = 'DISPATCHING'\s+AND NEW.state IN \([^)]*PREPARED")

    def test_definer_functions_are_pinned_and_revoked(self):
        for name in re.findall(r"CREATE OR REPLACE FUNCTION mubasher_oms\.(partops_\w+)\(", self.m04):
            body = self._fn(self.m04, name)
            header = body[: body.index("AS $function$")]
            if "SECURITY DEFINER" in header:
                self.assertIn("SET search_path = pg_catalog, mubasher_oms, pg_temp", header, name)
                self.assertRegex(self.m04, r"REVOKE ALL ON FUNCTION mubasher_oms\.{}\(".format(name), name)

    def test_unknown_outcome_pauses_job_and_records_attention(self):
        body = self._fn(self.m04, "partops_remote_mark_unknown")
        self.assertIn("'REMOTE_OUTCOME_UNKNOWN'", body)
        self.assertIn("SET next_run_time = NULL", body)
        self.assertIn("IF v_att.state <> 'DISPATCHING' THEN", body)

    def test_finalize_never_executes_ddl_and_keeps_is_enabled(self):
        body = self._fn(self.m04, "partops_remote_finalize")
        for forbidden in ("create_any_table_partition", "drop_any_table_partition", "is_enabled ="):
            self.assertNotIn(forbidden, body)
        self.assertIn("v_att.state <> 'TARGET_SUCCEEDED'", body)

    def test_reconcile_refuses_in_flight_attempt(self):
        body = self._fn(self.m04, "reconcile_missed_partition_job")
        self.assertIn("SKIPPED_ATTEMPT_ACTIVE", body)
        self.assertIn("IN ('DISPATCHING', 'TARGET_SUCCEEDED', 'OUTCOME_UNKNOWN')", body)
        self.assertNotIn("SKIPPED_REMOTE_TARGET", body)

    def test_claim_keeps_phase5a_ceiling_and_allowlist(self):
        body = self._fn(self.m04, "partops_remote_claim")
        self.assertIn("partition_job_max_execution_lateness()", body)
        self.assertIn("partition_job_config_param_allowed", body)
        self.assertIn("'NEEDS_REVIEW'", body)
        self.assertIn("p_confirm_drop IS NOT TRUE", body)

    def test_recovery_marks_dispatching_unknown(self):
        body = self._fn(self.m04, "partops_remote_recover_attempts")
        self.assertIn("IF v_rec.state = 'DISPATCHING' THEN", body)
        self.assertIn("partops_remote_mark_unknown", body)
        self.assertIn("action := 'FINALIZE'", body)

    def test_job_guard_blocks_critical_changes_and_reenable(self):
        guard = self._fn(self.m03, "fn_partops_job_execution_guard")
        for col in ("database_target_id", "table_schema", "table_name", "is_create", "job_schedule", "db_config_para"):
            self.assertIn("NEW.{} IS DISTINCT FROM OLD.{}".format(col, col), guard)
        self.assertIn("NEW.is_enabled IS TRUE", guard)
        self.assertIn("USING ERRCODE = '55006'", guard)

    def test_version(self):
        import framework_manifest as fm
        self.assertIn("'20261117_04'", self.m04)
        self.assertEqual(fm.load_manifest()["framework_version"], "20261117_04")


# ---------------------------------------------------------------------------
# 6. API: remote manual runs, resolution, history
# ---------------------------------------------------------------------------
class RemoteApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.job = {"job_id": 5, "is_create": False, "database_target_id": 2,
                    "table_schema": "oms", "table_name": "orders"}

    def _run(self, remote_result, body=None):
        with patch("api.routers.jobs.get_database_readiness", return_value={"config_table_select": True}), \
                patch("api.routers.jobs.get_partition_job", return_value=self.job), \
                patch("api.remote_manual.list_database_targets", return_value=([ctl(), remote()], [], True)), \
                patch("api.remote_manual.request_remote_manual_run", return_value=remote_result) as req, \
                patch("api.routers.jobs.run_partition_job_manual") as local:
            r = self.client.post("/api/jobs/5/run", json=body if body is not None else {"confirm_drop": True})
        return r, req, local

    def test_remote_manual_routes_to_scheduler_engine(self):
        r, req, local = self._run({"status": "MANUAL_SUCCESS", "message": "DROP done", "target_key": "db02"})
        self.assertEqual(r.status_code, 200)
        req.assert_called_once_with(5, confirm_drop=True, attention_id=None)
        local.assert_not_called()

    def test_drop_confirmation_preserved(self):
        r, req, _ = self._run({"status": "MANUAL_SUCCESS"}, body={})
        self.assertEqual(r.status_code, 400)
        req.assert_not_called()

    def test_outcome_mapping(self):
        for status, code in (("MANUAL_OUTCOME_UNKNOWN", 502), ("SCHEDULER_UNAVAILABLE", 503),
                             ("SCHEDULER_TIMEOUT", 504), ("MANUAL_TARGET_LOCKED", 409),
                             ("NEEDS_REVIEW", 409), ("MANUAL_FAIL", 502)):
            r, _, _ = self._run({"status": status, "message": "m"})
            self.assertEqual(r.status_code, code, status)

    def test_resolve_requires_confirm_and_typed_target_for_drop(self):
        attempt = {"attempt_id": 41, "state": "OUTCOME_UNKNOWN", "operation": "DROP",
                   "table_schema": "oms", "table_name": "orders", "target_key": "db02"}
        with patch("api.routers.attempts.get_execution_attempt", return_value=attempt), \
                patch("api.routers.attempts.resolve_remote_outcome", return_value={"result": "RESOLVED_NOT_APPLIED"}) as res, \
                patch("api.routers.attempts.notify_scheduler_refresh", return_value=(True, "ok")):
            body = {"resolution": "NOT_APPLIED", "note": "checked: partition still present"}
            self.assertEqual(self.client.post("/api/execution-attempts/41/resolve", json=body).status_code, 400)
            self.assertEqual(self.client.post("/api/execution-attempts/41/resolve",
                                              json=dict(body, confirm=True)).status_code, 400)
            r = self.client.post("/api/execution-attempts/41/resolve",
                                 json=dict(body, confirm=True, confirm_target="oms.orders"))
            self.assertEqual(r.status_code, 200)
            res.assert_called_once_with(41, "NOT_APPLIED", body["note"])

    def test_resolve_only_unknown(self):
        with patch("api.routers.attempts.get_execution_attempt",
                   return_value={"attempt_id": 41, "state": "FINALIZED", "operation": "CREATE"}):
            r = self.client.post("/api/execution-attempts/41/resolve",
                                 json={"confirm": True, "resolution": "APPLIED", "note": "ok ok"})
        self.assertEqual(r.status_code, 409)

    def test_unknown_attention_is_never_rerun(self):
        item = {"attention_id": 3, "status": "PENDING_REVIEW", "reason": "REMOTE_OUTCOME_UNKNOWN",
                "job_id": 5, "is_create": True, "table_schema": "oms", "table_name": "orders",
                "database_target_id": 2}
        with patch("api.routers.attention.get_partition_job_attention_item", return_value=item), \
                patch("api.routers.attention.recover_partition_job_attention") as local, \
                patch("api.remote_manual.request_remote_manual_run") as req:
            r = self.client.post("/api/attention/3/recover", json={"confirm": True})
        self.assertEqual(r.status_code, 409)
        local.assert_not_called()
        req.assert_not_called()

    def test_history_identifies_target_without_secrets(self):
        logs = [{"job_log_id": 1, "job_id": 5, "database_target_id": 2, "execution_attempt_id": 41},
                {"job_log_id": 2, "job_id": 9, "database_target_id": None, "execution_attempt_id": None}]
        with patch("api.routers.logs.query_partition_job_logs", return_value={"logs": logs, "limit": 100, "offset": 0, "has_more": False, "next_offset": None, "previous_offset": None}), \
                patch("api.routers.logs.list_database_targets",
                      return_value=([ctl(), remote(credential_provider="aws", secret_id="s/db02",
                                                   aws_region="eu-west-1")], [], True)):
            r = self.client.get("/api/logs")
        rows = r.json()["data"]["logs"] if "data" in r.json() else r.json()["logs"]
        self.assertEqual([x["target_key"] for x in rows], ["db02", "db01"])
        self.assertNotIn("s/db02", r.text)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Phase 5E.1 — manual-run transport classification (503 vs ambiguous)
# ---------------------------------------------------------------------------
class ManualRunTransportTests(unittest.TestCase):
    """'Nothing was executed' only when the scheduler was never reached."""

    def _call(self, exc):
        import scheduler_client
        with patch("scheduler_client.urllib.request.urlopen", side_effect=exc):
            return scheduler_client.request_remote_manual_run(5, confirm_drop=True)

    def test_connection_refused_is_unavailable_nothing_executed(self):
        import urllib.error
        import scheduler_client
        for exc in (ConnectionRefusedError("refused"),
                    urllib.error.URLError(ConnectionRefusedError(10061, "refused"))):
            res = self._call(exc)
            self.assertEqual(res["status"], scheduler_client.REMOTE_RUN_UNAVAILABLE, exc)
            self.assertIn("Nothing was executed", res["message"])

    def test_disconnect_reset_or_timeout_after_send_is_ambiguous(self):
        import http.client
        import socket
        import scheduler_client
        for exc in (http.client.RemoteDisconnected("closed"), ConnectionResetError("reset"),
                    socket.timeout("timed out"), TimeoutError("timed out")):
            res = self._call(exc)
            self.assertEqual(res["status"], scheduler_client.REMOTE_RUN_TIMEOUT, exc)
            self.assertNotIn("nothing was executed", res["message"].lower())
            self.assertIn("Do not re-run", res["message"])

    def test_http_5xx_is_ambiguous_4xx_is_refusal(self):
        import io
        import urllib.error
        import scheduler_client

        def http_error(code):
            return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"not json"))

        self.assertEqual(self._call(http_error(500))["status"], scheduler_client.REMOTE_RUN_TIMEOUT)
        res = self._call(http_error(404))
        self.assertEqual(res["status"], scheduler_client.REMOTE_RUN_UNAVAILABLE)
        self.assertIn("nothing was executed", res["message"])
