"""Phase 5A — missed-run policy, bounded retry/backoff, manual target lock,
db_config_para allowlist, DBA review API.

No database is contacted: SQL behaviour is verified as source contracts on the
version-controlled migrations; scheduler behaviour runs the real asyncio loop
with mocked DB helpers.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from scheduler_backend import scheduler_database
from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.queue_manager import ScheduleQueue
from scheduler_backend.retry_policy import (
    ACTION_CLEARED,
    ACTION_COOLDOWN,
    ACTION_EXHAUSTED,
    ACTION_RETRY,
    HISTORICAL,
    OWNED,
    OccurrenceOwnership,
    OccurrenceRetryTracker,
    parse_bounded_int,
    parse_bounded_seconds,
    parse_delay_list,
)
from scheduler_backend.scheduler import PartitionScheduler, _HANDLED_OCCURRENCE_STATUSES

ROOT = os.path.dirname(os.path.abspath(__file__))
MIG_01 = os.path.join(ROOT, "sql", "migrations", "20261110_01_partition_job_attention.sql")
MIG_02 = os.path.join(
    ROOT, "sql", "migrations", "20261110_02_missed_run_reconcile_retry_and_manual_lock.sql"
)
CANONICAL = os.path.join(ROOT, "sql", "realtime_scheduler_v1.sql")

T_MISSED = datetime(2026, 10, 20, 2, 0, 0)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _function(sql: str, name: str) -> str:
    """Return one CREATE OR REPLACE FUNCTION ... $function$; block."""
    start = sql.index("CREATE OR REPLACE FUNCTION mubasher_oms.{}(".format(name))
    body_end = sql.index("$function$;", start) + len("$function$;")
    return sql[start:body_end]


def _body(sql: str, name: str) -> str:
    """Executable body only (between the dollar quotes, comments removed)."""
    fn = _function(sql, name)
    inner = fn.split("$function$", 2)[1]
    return re.sub(r"--[^\n]*", "", inner)


def _job(job_id: int, when: datetime, delay: float) -> ScheduledJob:
    return ScheduledJob(job_id, when, delay, "JOB_TEST", False)


def _cfg(**kw) -> SchedulerConfig:
    base = dict(reconcile_seconds=60.0, lookahead_seconds=120.0)
    base.update(kw)
    return SchedulerConfig(**base)


# ---------------------------------------------------------------------------
# 1–4. Overdue occurrences are reconciled, never executed (SQL contracts)
# ---------------------------------------------------------------------------
class MissedRunSqlContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mig = _read(MIG_02)
        cls.sched = _body(cls.mig, "run_partition_job_scheduled")
        cls.rec = _body(cls.mig, "reconcile_missed_partition_job")

    def test_db_ceiling_never_executes_beyond_max_lateness(self) -> None:
        guard = (
            "IF v_now > p_expected_run_time + "
            "mubasher_oms.partition_job_max_execution_lateness() THEN"
        )
        self.assertIn(guard, self.sched)
        at = self.sched.index(guard)
        block = self.sched[at:].split("END IF;", 1)[0]
        self.assertIn("reconcile_missed_partition_job", block)
        self.assertIn("'MISSED_SCHEDULED_RUN'", block)
        self.assertIn("RETURN;", block)
        # Missed check precedes target lock, schedule calc, db_config and workers.
        for later in (
            "87421032",
            "cron_to_interval_or_next_run",
            "set_config",
            "create_any_table_partition",
            "drop_any_table_partition",
        ):
            self.assertGreater(self.sched.index(later), at, later)
        # And follows revalidation (row lock, disabled, stale, not-due).
        for earlier in (
            "FOR UPDATE",
            "SKIPPED_DISABLED",
            "SKIPPED_RESCHEDULED",
            "SKIPPED_NOT_DUE",
        ):
            self.assertLess(self.sched.index(earlier), at, earlier)

    def test_reconcile_never_runs_ddl_or_config(self) -> None:
        for forbidden in (
            "create_any_table_partition",
            "drop_any_table_partition",
            "set_config",
            "run_partition_job_manual",
        ):
            self.assertNotIn(forbidden, self.rec, forbidden)

    def test_next_run_advances_to_first_future_occurrence(self) -> None:
        self.assertIn(
            "v_schedule := mubasher_oms.cron_to_interval_or_next_run(v_job.job_schedule)",
            self.rec,
        )
        self.assertIn("v_next <= v_now OR v_next <= p_expected_run_time", self.rec)
        self.assertIn("SET next_run_time = v_next", self.rec)
        self.assertNotIn("interval '1 day'", self.rec)
        self.assertNotIn("v_job.frequency", self.rec)
        self.assertIn("'MISSED_RESCHEDULED'", self.rec)
        # Fail closed when no valid future schedule.
        self.assertIn("'MISSED_INVALID_SCHEDULE'", self.rec)
        self.assertIn("v_next := NULL", self.rec)

    def test_attention_row_inserted_idempotently(self) -> None:
        self.assertIn("INSERT INTO mubasher_oms.partition_job_attention", self.rec)
        self.assertIn(
            "ON CONFLICT ON CONSTRAINT partition_job_attention_job_missed_uq DO NOTHING",
            self.rec,
        )
        insert = self.rec.split("INSERT INTO mubasher_oms.partition_job_attention", 1)[1]
        insert = insert.split(") VALUES (", 1)[0]
        for col in ("missed_run_time", "detected_at", "rescheduled_to", "reason"):
            self.assertIn(col, insert)
        # Evidence inserted before the schedule is advanced.
        self.assertLess(
            self.rec.index("INSERT INTO mubasher_oms.partition_job_attention"),
            self.rec.index("SET next_run_time = v_next"),
        )

    def test_last_run_status_never_set_to_missed(self) -> None:
        self.assertNotIn("last_run_status", self.rec)
        self.assertNotIn("last_run_time", self.rec)
        self.assertNotIn("partitioning_job_table_log", self.rec)
        for text in (self.mig, _read(MIG_01)):
            self.assertNotRegex(text, r"last_run_status\s*=\s*'MISSED")

    def test_reconcile_revalidates_under_locks(self) -> None:
        self.assertIn("87421031", self.rec)
        self.assertIn("FOR UPDATE", self.rec)
        self.assertLess(self.rec.index("FOR UPDATE"), self.rec.index("SKIPPED_RESCHEDULED"))
        self.assertIn("IF v_job.next_run_time > v_now THEN", self.rec)

    def test_reconcile_reasons_restricted(self) -> None:
        self.assertIn("'MISSED_SCHEDULED_RUN', 'RETRY_EXHAUSTED'", self.rec)

    def test_canonical_files_match_latest_migration(self) -> None:
        # Canonical v1 bodies track the LATEST migration that redefines them
        # (Phase 5C: 20261117_02, which keeps every Phase 5A rule and adds the
        # remote-target guard).
        canonical = _read(CANONICAL)
        mig_dir = os.path.join(ROOT, "sql", "migrations")
        for name in ("run_partition_job_scheduled", "reconcile_missed_partition_job"):
            marker = "CREATE OR REPLACE FUNCTION mubasher_oms.{}(".format(name)
            latest = [f for f in sorted(os.listdir(mig_dir))
                      if f.endswith(".sql") and marker in _read(os.path.join(mig_dir, f))][-1]
            self.assertEqual(_function(canonical, name),
                             _function(_read(os.path.join(mig_dir, latest)), name), name)
        # The Phase 5A rules are still present in the canonical bodies.
        sched = _body(canonical, "run_partition_job_scheduled")
        self.assertIn("partition_job_max_execution_lateness()", sched)
        self.assertIn("partition_job_config_param_allowed", sched)

    def test_existing_functions_replaced_without_drop(self) -> None:
        executable = re.sub(r"--[^\n]*", "", self.mig)
        self.assertNotIn("DROP FUNCTION", executable)
        self.assertNotIn("CASCADE", executable)
        self.assertIn("BEGIN;", self.mig)
        self.assertIn("COMMIT;", self.mig)

    def test_advisory_key_overflow_safe_and_identical(self) -> None:
        # bigint before abs(): abs(hashtext()) cannot overflow int4.
        self.assertNotIn("abs(hashtext('partition_job_scheduled:' || p_job_id::text)) %", self.mig)
        self.assertIn("::bigint) % 2147483647)::integer", self.sched)


class AttentionTableMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = _read(MIG_01)

    def test_table_shape(self) -> None:
        for column in (
            "attention_id",
            "job_id",
            "reason",
            "missed_run_time",
            "detected_at",
            "rescheduled_to",
            "status",
            "reviewed_at",
            "resolution",
            "manual_execution_result",
        ):
            self.assertIn(column, self.sql)
        self.assertIn("UNIQUE (job_id, missed_run_time)", self.sql)
        self.assertIn("'PENDING_REVIEW', 'MANUALLY_RERUN', 'ACKNOWLEDGED'", self.sql)
        self.assertIn("WHERE status = 'PENDING_REVIEW'", self.sql)

    def test_security(self) -> None:
        self.assertIn("OWNER TO mubasher_oms", self.sql)
        self.assertIn(
            "REVOKE ALL ON TABLE mubasher_oms.partition_job_attention FROM PUBLIC", self.sql
        )
        self.assertNotIn("GRANT ALL", self.sql)
        self.assertNotRegex(self.sql, r"GRANT[^;']*DELETE")
        # UI role gets column-level UPDATE of review columns only.
        columns = self.sql.split("GRANT UPDATE (", 1)[1].split(
            "ON mubasher_oms.partition_job_attention", 1
        )[0]
        self.assertIn("status", columns)
        self.assertIn("manual_execution_result", columns)
        for evidence in ("missed_run_time", "job_id", "detected_at", "rescheduled_to", "reason"):
            self.assertNotIn(evidence, columns)
        # No privilege change on the job configuration table.
        self.assertNotRegex(self.sql, r"GRANT[^;]*ON mubasher_oms\.partitioning_job_table ")

    def test_guard_trigger_protects_evidence(self) -> None:
        self.assertIn("BEFORE UPDATE ON mubasher_oms.partition_job_attention", self.sql)
        guard = _body(self.sql, "fn_partition_job_attention_guard")
        for col in ("missed_run_time", "detected_at", "rescheduled_to", "reason", "job_id"):
            self.assertIn("NEW.{}".format(col), guard)
        self.assertIn("OLD.status IS DISTINCT FROM 'PENDING_REVIEW'", guard)

    def test_db_ceiling_is_safety_net_not_ten_minutes(self) -> None:
        self.assertIn("partition_job_max_execution_lateness()", self.sql)
        self.assertIn("interval '6 hours'", self.sql)
        for path in (MIG_01, MIG_02, CANONICAL):
            text = _read(path)
            self.assertNotIn("partition_job_missed_run_grace", text, path)
            self.assertNotIn("10 minutes", text, path)


# ---------------------------------------------------------------------------
# 5–6. Manual recovery / acknowledge never move the normal schedule
# ---------------------------------------------------------------------------
class RecoverySqlContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mig = _read(MIG_02)
        cls.recover = _body(cls.mig, "run_partition_job_attention_recovery")
        cls.ack = _body(cls.mig, "acknowledge_partition_job_attention")
        cls.manual = _body(cls.mig, "run_partition_job_manual")

    def test_recovery_uses_manual_path_and_does_not_touch_schedule(self) -> None:
        self.assertIn("mubasher_oms.run_partition_job_manual(v_att.job_id)", self.recover)
        self.assertNotIn("next_run_time", self.recover)
        self.assertNotIn("UPDATE mubasher_oms.partitioning_job_table", self.recover)
        self.assertNotIn("next_run_time", self.manual)
        self.assertNotIn("UPDATE mubasher_oms.partitioning_job_table", self.manual)

    def test_recovery_requires_pending_and_unchanged_target(self) -> None:
        self.assertIn("FOR UPDATE NOWAIT", self.recover)
        self.assertIn("v_att.status IS DISTINCT FROM 'PENDING_REVIEW'", self.recover)
        for col in ("is_create", "table_schema", "table_name"):
            self.assertIn("v_job.{0} IS DISTINCT FROM v_att.{0}".format(col), self.recover)
        # Only success resolves the item; failures stay pending with evidence.
        self.assertIn("WHEN v_status = 'MANUAL_SUCCESS' THEN 'MANUALLY_RERUN'", self.recover)
        self.assertIn("manual_execution_result = v_status", self.recover)

    def test_recovery_and_ack_are_invoker(self) -> None:
        for name in (
            "run_partition_job_attention_recovery",
            "acknowledge_partition_job_attention",
            "reconcile_missed_partition_job",
        ):
            header = _function(self.mig, name).split("AS $function$", 1)[0]
            self.assertIn("SECURITY INVOKER", header, name)
            self.assertNotIn("SECURITY DEFINER", header, name)

    def test_acknowledge_executes_no_ddl(self) -> None:
        for forbidden in (
            "run_partition_job_manual",
            "create_any_table_partition",
            "drop_any_table_partition",
            "set_config",
            "next_run_time",
        ):
            self.assertNotIn(forbidden, self.ack, forbidden)
        self.assertIn("status = 'ACKNOWLEDGED'", self.ack)

    def test_public_revoked_and_narrow_grants(self) -> None:
        for sig in (
            "run_partition_job_attention_recovery(bigint) FROM PUBLIC",
            "acknowledge_partition_job_attention(bigint, text) FROM PUBLIC",
            "run_partition_job_manual(numeric) FROM PUBLIC",
        ):
            self.assertIn(sig, self.mig)
        self.assertNotIn("GRANT ALL", self.mig)


# ---------------------------------------------------------------------------
# 14. Manual run takes the shared target-table lock
# ---------------------------------------------------------------------------
class ManualTargetLockTests(unittest.TestCase):
    def test_manual_uses_identical_target_lock(self) -> None:
        mig = _read(MIG_02)
        manual = _body(mig, "run_partition_job_manual")
        sched = _body(mig, "run_partition_job_scheduled")

        def lock_expr(body: str) -> str:
            at = body.index("pg_try_advisory_xact_lock(\n        87421032")
            return re.sub(r"\s+", " ", body[at: body.index(";", at)])

        self.assertEqual(lock_expr(manual), lock_expr(sched))
        self.assertIn("'MANUAL_TARGET_LOCKED'", manual)
        lock_at = manual.index("87421032")
        self.assertLess(lock_at, manual.index("create_any_table_partition"))
        self.assertLess(lock_at, manual.index("set_config"))
        busy = manual[manual.index("IF NOT v_lock_ok THEN"):].split("END IF;", 1)[0]
        self.assertNotIn("INSERT", busy)

    def test_python_maps_target_locked_to_conflict(self) -> None:
        import database

        cur = MagicMock()
        cur.description = [("status",), ("message",), ("execution_duration_ms",)]
        cur.fetchone.return_value = ("MANUAL_TARGET_LOCKED", "busy target", None)
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur

        @contextmanager
        def fake_connection(_factory):
            yield conn

        with patch.object(database, "_connection", fake_connection):
            with self.assertRaises(database.DatabaseConflictError) as ctx:
                database.run_partition_job_manual(7)
        self.assertIn("busy target", ctx.exception.message)

    def test_api_maps_conflict_to_409(self) -> None:
        from api.deps import raise_for_domain
        import database

        with self.assertRaises(HTTPException) as ctx:
            raise_for_domain(database.DatabaseConflictError("busy"))
        self.assertEqual(ctx.exception.status_code, 409)


# ---------------------------------------------------------------------------
# 15–16. db_config_para allowlist
# ---------------------------------------------------------------------------
class DbConfigAllowlistTests(unittest.TestCase):
    def test_dangerous_keys_rejected(self) -> None:
        from validators import ValidationError, validate_db_config_json

        for key, value in (
            ("role", "postgres"),
            ("session_authorization", "postgres"),
            ("search_path", "evil, public"),
            ("SEARCH_PATH", "public"),
            ("default_transaction_read_only", "off"),
            ("log_statement", "all"),
            ("password", "x"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError) as ctx:
                    validate_db_config_json('{"%s": "%s"}' % (key, value))
                self.assertIn("not supported", ctx.exception.message)

    def test_datestyle_only_iso(self) -> None:
        from validators import ValidationError, validate_db_config_json

        self.assertEqual(
            validate_db_config_json('{"datestyle": "ISO, DMY"}'), {"datestyle": "ISO, DMY"}
        )
        self.assertEqual(
            validate_db_config_json('{"DateStyle": "iso, mdy"}'), {"datestyle": "iso, mdy"}
        )
        for bad in ("Redwood, SHOW_TIME", "SQL, DMY", "German", "Postgres"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    validate_db_config_json('{"datestyle": "%s"}' % bad)

    def test_allowed_tuning_keys_accepted(self) -> None:
        from validators import validate_db_config_json

        parsed = validate_db_config_json(
            '{"lock_timeout": "30s", "statement_timeout": "15min", "work_mem": "512MB",'
            ' "maintenance_work_mem": "1GB", "effective_io_concurrency": 200,'
            ' "random_page_cost": 1.1, "idle_in_transaction_session_timeout": "5min",'
            ' "max_parallel_maintenance_workers": 2}'
        )
        self.assertEqual(parsed["work_mem"], "512MB")
        self.assertEqual(parsed["random_page_cost"], 1.1)
        self.assertEqual(validate_db_config_json("{}"), {})

    def test_bad_values_rejected(self) -> None:
        from validators import ValidationError, validate_db_config_json

        for payload in (
            '{"work_mem": "1GB; RESET ROLE"}',
            '{"work_mem": "x\'y"}',
            '{"work_mem": true}',
            '{"work_mem": {"a": 1}}',
            '{"work_mem": ""}',
            '{"work_mem": "%s"}' % ("9" * 65),
            '{"work_mem": "1MB", "WORK_MEM": "2MB"}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_db_config_json(payload)

    def test_python_and_sql_allowlists_match(self) -> None:
        from validators import ALLOWED_DB_CONFIG_PARAMETERS

        sql = _read(MIG_01)
        fn = _function(sql, "partition_job_config_param_allowed")
        listed = set(re.findall(r"'([a-z_]+)'", fn.split("WHEN lower(p_key) IN (", 1)[1].split(")", 1)[0]))
        self.assertEqual(listed | {"datestyle"}, set(ALLOWED_DB_CONFIG_PARAMETERS))
        self.assertIn("WHEN lower(p_key) = 'datestyle'", fn)
        for forbidden in ("search_path", "role", "session_authorization"):
            self.assertNotIn("'{}'".format(forbidden), fn)

    def test_sql_enforces_allowlist_before_every_set_config(self) -> None:
        mig = _read(MIG_02)
        for name in ("run_partition_job_scheduled", "run_partition_job_manual"):
            body = _body(mig, name)
            self.assertLess(
                body.index("partition_job_config_param_allowed"),
                body.index("set_config"),
                name,
            )
        update = _body(mig, "update_data_to_partition_job_table")
        self.assertIn("partition_job_config_param_allowed", update)
        header = _function(mig, "update_data_to_partition_job_table").split("AS $function$")[0]
        self.assertIn("SECURITY DEFINER", header)
        self.assertIn("SET search_path = pg_catalog, mubasher_oms, pg_temp", header)


# ---------------------------------------------------------------------------
# 9–12. Retry policy unit behaviour
# ---------------------------------------------------------------------------
class RetryPolicyTests(unittest.TestCase):
    KEY = (33, T_MISSED)

    def test_failure_delays_increase_and_are_bounded(self) -> None:
        tracker = OccurrenceRetryTracker(failure_delays=(5.0, 15.0, 30.0))
        delays = []
        for i in range(3):
            d = tracker.record_outcome(self.KEY, "FAILED_DATABASE", now_mono=100.0)
            self.assertEqual(d.action, ACTION_RETRY)
            delays.append(d.delay_seconds)
        self.assertEqual(delays, [5.0, 15.0, 30.0])
        d = tracker.record_outcome(self.KEY, "FAILED_CONNECTION", now_mono=100.0)
        self.assertEqual(d.action, ACTION_EXHAUSTED)
        self.assertTrue(tracker.is_exhausted(self.KEY))
        self.assertEqual(tracker.max_failure_attempts, 4)

    def test_retry_delay_sets_not_before(self) -> None:
        tracker = OccurrenceRetryTracker()  # defaults: 15, 60, 180 s
        tracker.record_outcome(self.KEY, "FAILED_DATABASE", now_mono=1000.0)
        self.assertEqual(tracker.not_before(self.KEY), 1015.0)

    def test_success_clears_state(self) -> None:
        tracker = OccurrenceRetryTracker()
        tracker.record_outcome(self.KEY, "FAILED_DATABASE", now_mono=1.0)
        for handled in sorted(_HANDLED_OCCURRENCE_STATUSES):
            tracker.record_outcome(self.KEY, "FAILED_DATABASE", now_mono=1.0)
            d = tracker.record_outcome(self.KEY, handled, now_mono=2.0)
            self.assertEqual(d.action, ACTION_CLEARED)
            self.assertNotIn(self.KEY, tracker)

    def test_target_locked_is_contention_not_failure(self) -> None:
        tracker = OccurrenceRetryTracker()
        d1 = tracker.record_outcome(self.KEY, "SKIPPED_TARGET_LOCKED", now_mono=0.0)
        d2 = tracker.record_outcome(self.KEY, "SKIPPED_TARGET_LOCKED", now_mono=0.0)
        self.assertEqual((d1.action, d1.delay_seconds), (ACTION_RETRY, 5.0))
        self.assertEqual(d2.delay_seconds, 10.0)
        self.assertEqual(tracker.get(self.KEY).failures, 0)
        # Cooldown caps at the last configured value; still never immediate.
        for _ in range(10):
            d = tracker.record_outcome(self.KEY, "SKIPPED_TARGET_LOCKED", now_mono=0.0)
        self.assertEqual(d.delay_seconds, 30.0)
        # Bounded by the total wait budget (default 1 h), not by wall clock vs
        # the scheduled time.
        d = tracker.record_outcome(self.KEY, "SKIPPED_TARGET_LOCKED", now_mono=3600.0)
        self.assertEqual(d.action, ACTION_EXHAUSTED)

    def test_every_non_handled_status_has_minimum_delay(self) -> None:
        tracker = OccurrenceRetryTracker()
        for status in ("SKIPPED_NOT_DUE", "NOT_FOUND", "SKIPPED_DISABLED", "WEIRD", ""):
            d = tracker.record_outcome(self.KEY, status, now_mono=0.0)
            self.assertGreaterEqual(d.delay_seconds, 1.0, status)
            self.assertEqual(d.action, ACTION_COOLDOWN)

    def test_reconcile_failure_backs_off_exponentially_with_cap(self) -> None:
        tracker = OccurrenceRetryTracker(exhausted_retry_seconds=60.0)
        for _ in range(4):
            tracker.record_outcome(self.KEY, "FAILED_DATABASE", now_mono=0.0)
        delays = [
            tracker.record_reconcile_outcome(self.KEY, "FAILED_DATABASE", now_mono=0.0).delay_seconds
            for _ in range(6)
        ]
        self.assertEqual(delays[:4], [60.0, 120.0, 240.0, 480.0])
        self.assertEqual(max(delays), 600.0)
        d = tracker.record_reconcile_outcome(self.KEY, "MISSED_RESCHEDULED", now_mono=0.0)
        self.assertEqual(d.action, ACTION_CLEARED)
        self.assertNotIn(self.KEY, tracker)

    def test_retain_drops_vanished_but_protects_active(self) -> None:
        tracker = OccurrenceRetryTracker()
        other = (34, T_MISSED)
        tracker.record_outcome(self.KEY, "FAILED_DATABASE", now_mono=0.0)
        tracker.record_outcome(other, "FAILED_DATABASE", now_mono=0.0)
        removed = tracker.retain([], protected_keys=[other], now_mono=1.0)
        self.assertEqual(removed, 1)
        self.assertNotIn(self.KEY, tracker)
        self.assertIn(other, tracker)

    def test_bounded_memory(self) -> None:
        tracker = OccurrenceRetryTracker(max_entries=10)
        for i in range(50):
            tracker.record_outcome((i, T_MISSED), "FAILED_DATABASE", now_mono=0.0)
        self.assertEqual(len(tracker), 10)

    def test_delay_env_parsing(self) -> None:
        self.assertEqual(parse_delay_list(None, (5.0,), "X"), (5.0,))
        self.assertEqual(parse_delay_list("2, 4,8", (5.0,), "X"), (2.0, 4.0, 8.0))
        for bad in ("0", "-1", "abc", "5,,6", "99999", ",".join(["5"] * 11)):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    parse_delay_list(bad, (5.0,), "X")

    def test_queue_respects_not_before(self) -> None:
        queue = ScheduleQueue()
        queue.replace_from_jobs(
            [_job(33, T_MISSED, -3600.0)], now_mono=1000.0, not_before={self.KEY: 1005.0}
        )
        self.assertIsNone(queue.pop_due(now_mono=1004.9))
        self.assertEqual(queue.pop_due(now_mono=1005.0), self.KEY)


# ---------------------------------------------------------------------------
# 7, 8, 11, 13, 19. Live scheduler loop regressions (no busy loop)
# ---------------------------------------------------------------------------
class SchedulerLoopRegressionTests(unittest.TestCase):
    """Run the real asyncio loop with an always-overdue occurrence."""

    def _run_loop(self, execute_side_effect, seconds: float = 0.8, config=None):
        config = config or _cfg()
        scheduler = PartitionScheduler(config)
        calls = {"execute": 0, "fetch": 0, "reconcile": 0}

        def fake_fetch(cfg):
            calls["fetch"] += 1
            # DB keeps reporting the same overdue occurrence (not advanced).
            return [_job(33, T_MISSED, -30.0)]

        def fake_execute(cfg, job_id, expected):
            calls["execute"] += 1
            return execute_side_effect(job_id, expected)

        def fake_reconcile(cfg, job_id, expected, reason, detail=None):
            calls["reconcile"] += 1
            calls.setdefault("reasons", []).append(reason)
            return {"status": "MISSED_RESCHEDULED", "job_id": job_id,
                    "message": "ok", "next_run_time": expected + timedelta(days=7),
                    "is_create": False}

        async def scenario():
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(seconds)
            started = time.monotonic()
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)
            return time.monotonic() - started

        with patch("scheduler_backend.scheduler.fetch_upcoming_jobs", side_effect=fake_fetch), \
                patch("scheduler_backend.scheduler.execute_scheduled_job", side_effect=fake_execute), \
                patch("scheduler_backend.scheduler.reconcile_occurrence", side_effect=fake_reconcile), \
                patch("scheduler_backend.scheduler.count_failed_jobs", return_value=0), \
                patch("scheduler_backend.scheduler.count_pending_attention", return_value=0):
            shutdown_seconds = asyncio.run(scenario())
        return scheduler, calls, shutdown_seconds

    def _status(self, status):
        def side(job_id, expected):
            return {"status": status, "job_id": job_id, "message": status,
                    "next_run_time": None, "is_create": False}
        return side

    def test_failed_database_cannot_immediate_loop(self) -> None:
        scheduler, calls, _ = self._run_loop(self._status("FAILED_DATABASE"))
        # Pre-5A this spun hundreds of times per second.
        self.assertEqual(calls["execute"], 1)
        self.assertLessEqual(calls["fetch"], 4)
        state = scheduler._retry.get((33, T_MISSED))
        self.assertEqual(state.failures, 1)
        self.assertGreater(state.next_eligible_mono, time.monotonic())

    def test_failed_connection_cannot_immediate_loop(self) -> None:
        def boom(job_id, expected):
            raise RuntimeError("db down")

        scheduler, calls, _ = self._run_loop(boom)
        self.assertEqual(calls["execute"], 1)
        self.assertEqual(scheduler.status.last_execution_result, "FAILED_CONNECTION")

    def test_skipped_target_locked_cannot_busy_loop(self) -> None:
        scheduler, calls, _ = self._run_loop(self._status("SKIPPED_TARGET_LOCKED"))
        self.assertEqual(calls["execute"], 1)
        self.assertEqual(scheduler._retry.get((33, T_MISSED)).contentions, 1)

    def test_bounded_retries_then_reconcile_not_execute(self) -> None:
        config = _cfg(retry_delays=(0.2, 0.2, 0.2))
        # min_redispatch floor is 1s → use a tracker with a tiny floor for speed.
        scheduler, calls, _ = self._run_loop(
            self._status("FAILED_DATABASE"), seconds=0.1, config=config
        )
        self.assertEqual(calls["execute"], 1)
        # Drive the remaining attempts directly (deterministic, no sleeps).
        tracker = scheduler._retry
        key = (33, T_MISSED)

        async def drive():
            with patch("scheduler_backend.scheduler.execute_scheduled_job",
                       side_effect=lambda c, j, e: self._status("FAILED_DATABASE")(j, e)) as ex, \
                    patch("scheduler_backend.scheduler.reconcile_occurrence",
                          return_value={"status": "MISSED_RESCHEDULED", "job_id": 33,
                                        "message": "ok", "next_run_time": None,
                                        "is_create": False}) as rec:
                for _ in range(10):
                    await scheduler._execute_one(33, T_MISSED)
                return ex.call_count, rec.call_count, rec.call_args

        executes, reconciles, rec_args = asyncio.run(drive())
        self.assertEqual(executes, 3)  # attempts 2..4; total 4 = 1 + len(delays)
        self.assertEqual(reconciles, 1)  # then handed to DB; never executed again
        self.assertEqual(rec_args[0][3], "RETRY_EXHAUSTED")
        self.assertIn("4 attempt", rec_args[0][4])
        self.assertNotIn(key, tracker)
        self.assertEqual(scheduler.status.last_execution_result, "SKIPPED_CIRCUIT_BREAKER")

    def test_retry_state_clears_after_success(self) -> None:
        scheduler = PartitionScheduler(_cfg())
        results = iter(["FAILED_DATABASE", "EXECUTED"])

        def side(cfg, job_id, expected):
            return {"status": next(results), "job_id": job_id, "message": "",
                    "next_run_time": expected + timedelta(days=1), "is_create": True}

        async def drive():
            with patch("scheduler_backend.scheduler.execute_scheduled_job", side_effect=side):
                await scheduler._execute_one(33, T_MISSED)
                self.assertIn((33, T_MISSED), scheduler._retry)
                await scheduler._execute_one(33, T_MISSED)

        asyncio.run(drive())
        self.assertNotIn((33, T_MISSED), scheduler._retry)
        self.assertEqual(scheduler.status.extra["retry_tracked"], 0)

    def test_shutdown_is_prompt_during_backoff(self) -> None:
        _, calls, shutdown_seconds = self._run_loop(
            self._status("FAILED_DATABASE"), seconds=0.4
        )
        self.assertEqual(calls["execute"], 1)
        self.assertLess(shutdown_seconds, 1.5)

    def test_refresh_signals_are_coalesced(self) -> None:
        scheduler = PartitionScheduler(_cfg())
        reasons = []

        async def fake_refresh(reason):
            reasons.append(reason)
            return True

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        async def scenario():
            task = asyncio.create_task(scheduler.run())
            for _ in range(40):  # 40 signals in ~0.4 s
                scheduler.request_refresh()
                await asyncio.sleep(0.01)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())
        self.assertLessEqual(reasons.count("http-refresh"), 2)

    def test_missed_result_is_handled_and_not_retried(self) -> None:
        scheduler = PartitionScheduler(_cfg())
        scheduler._pending_count_dirty = False
        sent = []

        def side(cfg, job_id, expected):
            return {"status": "MISSED_RESCHEDULED", "job_id": job_id,
                    "message": "not executed", "is_create": False,
                    "next_run_time": datetime(2026, 10, 27, 2, 0, 0)}

        async def drive():
            with patch("scheduler_backend.scheduler.execute_scheduled_job", side_effect=side) as ex:
                await scheduler._execute_one(33, T_MISSED)
                await scheduler._execute_one(33, T_MISSED)
                return ex.call_count

        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}), \
                patch("observability._send", side_effect=lambda p: sent.append(p.decode())):
            count = asyncio.run(drive())
        self.assertEqual(count, 1)
        self.assertNotIn((33, T_MISSED), scheduler._retry)
        self.assertTrue(scheduler._pending_count_dirty)
        joined = "\n".join(sent)
        self.assertIn("partops.missed_run.detected:1|c", joined)
        self.assertIn("reason:missed_scheduled_run", joined)
        self.assertIn("outcome:rescheduled", joined)
        self.assertNotIn("job_id", joined)

    def test_pending_review_gauge_and_retry_metrics(self) -> None:
        scheduler = PartitionScheduler(_cfg())
        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}), \
                patch("observability._send", side_effect=lambda p: sent.append(p.decode())), \
                patch("scheduler_backend.scheduler.fetch_upcoming_jobs", return_value=[]), \
                patch("scheduler_backend.scheduler.count_failed_jobs", return_value=0), \
                patch("scheduler_backend.scheduler.count_pending_attention", return_value=3), \
                patch("scheduler_backend.scheduler.execute_scheduled_job",
                      return_value={"status": "FAILED_DATABASE", "job_id": 1,
                                    "message": "", "next_run_time": None, "is_create": None}):
            asyncio.run(scheduler.refresh_queue("startup"))
            for _ in range(4):
                asyncio.run(scheduler._execute_one(1, T_MISSED))
        joined = "\n".join(sent)
        self.assertIn("partops.jobs.pending_review_count:3|g", joined)
        self.assertIn("partops.scheduler.retry_scheduled:1|c", joined)
        self.assertIn("status:failed_database", joined)
        self.assertIn("partops.scheduler.retry_exhausted:1|c", joined)
        for forbidden in ("job_id:", "table", "schema", "host:"):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(scheduler.status.extra["pending_review_count"], 3)

    def test_pending_count_absent_table_does_not_emit(self) -> None:
        scheduler = PartitionScheduler(_cfg())
        with patch("scheduler_backend.scheduler.fetch_upcoming_jobs", return_value=[]), \
                patch("scheduler_backend.scheduler.count_failed_jobs", return_value=0), \
                patch("scheduler_backend.scheduler.count_pending_attention", return_value=None):
            self.assertTrue(asyncio.run(scheduler.refresh_queue("startup")))
        self.assertNotIn("pending_review_count", scheduler.status.extra)


# ---------------------------------------------------------------------------
# Scheduler DB helpers
# ---------------------------------------------------------------------------
class SchedulerDatabaseHelperTests(unittest.TestCase):
    def _fake_open(self, row, captured):
        @contextmanager
        def fake_open(purpose):
            conn = MagicMock()
            cur = MagicMock()
            cur.fetchone.return_value = row
            cur.execute.side_effect = lambda sql, params=None: captured.append((sql, params))
            conn.cursor.return_value.__enter__.return_value = cur
            yield conn
        return fake_open

    def test_execute_result_is_datestyle_independent(self) -> None:
        captured = []
        row = {"status": "EXECUTED", "job_id": 1, "message": "ok",
               "next_run_time": "2026-10-27 02:00:00.000000", "is_create": True}
        with patch.object(scheduler_database, "open_connection", self._fake_open(row, captured)):
            result = scheduler_database.execute_scheduled_job(SchedulerConfig(), 1, T_MISSED)
        self.assertIn("to_char(r.next_run_time, 'YYYY-MM-DD HH24:MI:SS.US')", captured[0][0])
        self.assertEqual(result["next_run_time"], datetime(2026, 10, 27, 2, 0, 0))

    def test_reconcile_exhausted_calls_db_function(self) -> None:
        captured = []
        row = {"status": "MISSED_RESCHEDULED", "job_id": 1, "message": "ok",
               "next_run_time": None, "is_create": False}
        with patch.object(scheduler_database, "open_connection", self._fake_open(row, captured)):
            result = scheduler_database.reconcile_exhausted_occurrence(
                SchedulerConfig(), 1, T_MISSED, "detail"
            )
        sql, params = captured[0]
        self.assertIn("mubasher_oms.reconcile_missed_partition_job(", sql)
        self.assertEqual(params["reason"], "RETRY_EXHAUSTED")
        self.assertEqual(params["detail"], "detail")
        with self.assertRaises(ValueError):
            scheduler_database.reconcile_occurrence(
                SchedulerConfig(), 1, T_MISSED, "DROP TABLE x", None)
        self.assertEqual(result["status"], "MISSED_RESCHEDULED")

    def test_unparseable_timestamp_never_raises(self) -> None:
        self.assertIsNone(scheduler_database._parse_db_timestamp("23-OCT-26"))

    def test_config_loads_retry_settings(self) -> None:
        env = {
            "REALTIME_DB_EXPECTED_NAME": "x",
            "PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS": "3,6",
        }
        with patch.dict(os.environ, env), patch.object(scheduler_database, "_load_env"):
            cfg = scheduler_database.load_scheduler_config()
        self.assertEqual(cfg.retry_delays, (3.0, 6.0))
        self.assertEqual(cfg.reconcile_function, "reconcile_missed_partition_job")
        with patch.dict(os.environ, {"PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS": "0"}), \
                patch.object(scheduler_database, "_load_env"):
            with self.assertRaises(RuntimeError):
                scheduler_database.load_scheduler_config()


# ---------------------------------------------------------------------------
# 17–18. DBA review API (confirmation rules)
# ---------------------------------------------------------------------------
def _item(is_create, status="PENDING_REVIEW"):
    return {
        "attention_id": 9, "job_id": 33, "status": status, "is_create": is_create,
        "table_schema": "mubasher_oms", "table_name": "orders",
        "missed_run_time": T_MISSED, "current_next_run_time": datetime(2026, 10, 27, 2),
    }


class AttentionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from api.routers import attention as router
        self.router = router

    def test_create_recovery_requires_confirm(self) -> None:
        r = self.router
        with patch.object(r, "get_partition_job_attention_item", return_value=_item(True)), \
                patch.object(r, "recover_partition_job_attention") as rec:
            with self.assertRaises(HTTPException) as ctx:
                r.recover_attention(9, r.RecoverBody())
            self.assertEqual(ctx.exception.status_code, 400)
            rec.assert_not_called()
            rec.return_value = {"status": "MANUAL_SUCCESS", "job_id": 33}
            resp = r.recover_attention(9, r.RecoverBody(confirm=True))
        self.assertEqual(resp.status_code, 200)
        rec.assert_called_once_with(9)
        self.assertIn(b"normal scheduled next run is unchanged", resp.body)

    def test_drop_recovery_needs_stronger_confirmation(self) -> None:
        r = self.router
        with patch.object(r, "get_partition_job_attention_item", return_value=_item(False)), \
                patch.object(r, "recover_partition_job_attention",
                             return_value={"status": "MANUAL_SUCCESS", "job_id": 33}) as rec:
            for body in (
                r.RecoverBody(confirm=True),
                r.RecoverBody(confirm=True, confirm_drop=True),
                r.RecoverBody(confirm=True, confirm_drop=True, confirm_target="orders"),
                r.RecoverBody(confirm=True, confirm_drop=True, confirm_target="MUBASHER_OMS.ORDERS"),
            ):
                with self.subTest(body=body):
                    with self.assertRaises(HTTPException) as ctx:
                        r.recover_attention(9, body)
                    self.assertEqual(ctx.exception.status_code, 400)
            rec.assert_not_called()
            resp = r.recover_attention(
                9, r.RecoverBody(confirm=True, confirm_drop=True,
                                 confirm_target="mubasher_oms.orders"))
        self.assertEqual(resp.status_code, 200)
        rec.assert_called_once()

    def test_unknown_operation_treated_as_drop(self) -> None:
        self.assertTrue(self.router.requires_strong_confirmation(_item(None)))
        self.assertFalse(self.router.requires_strong_confirmation(_item(True)))

    def test_acknowledge_never_executes(self) -> None:
        r = self.router
        with patch.object(r, "get_partition_job_attention_item", return_value=_item(False)), \
                patch.object(r, "recover_partition_job_attention") as rec, \
                patch.object(r, "acknowledge_partition_job_attention",
                             return_value={"attention_status": "ACKNOWLEDGED"}) as ack:
            with self.assertRaises(HTTPException):
                r.acknowledge_attention(9, r.AcknowledgeBody())
            resp = r.acknowledge_attention(9, r.AcknowledgeBody(confirm=True, note="known outage"))
        self.assertEqual(resp.status_code, 200)
        ack.assert_called_once_with(9, "known outage")
        rec.assert_not_called()
        self.assertIn(b"Nothing was executed", resp.body)

    def test_already_reviewed_is_conflict(self) -> None:
        r = self.router
        with patch.object(r, "get_partition_job_attention_item",
                          return_value=_item(True, status="ACKNOWLEDGED")), \
                patch.object(r, "recover_partition_job_attention") as rec:
            with self.assertRaises(HTTPException) as ctx:
                r.recover_attention(9, r.RecoverBody(confirm=True))
        self.assertEqual(ctx.exception.status_code, 409)
        rec.assert_not_called()

    def test_list_filters_and_degrades_without_migration(self) -> None:
        r = self.router
        with patch.object(r, "get_partition_job_attention", return_value=([], False)) as get:
            resp = r.list_attention(status="pending", limit=200)
        get.assert_called_once_with("PENDING_REVIEW", 200)
        self.assertIn(b'"available":false', resp.body)
        with self.assertRaises(HTTPException):
            r.list_attention(status="bogus", limit=10)

    def test_recovery_failure_keeps_item_pending_and_reports_error(self) -> None:
        import database

        cur = MagicMock()
        cur.fetchone.return_value = {
            "result_status": "MANUAL_FAIL", "result_message": "worker boom",
            "result_duration_ms": 12, "attention_status": "PENDING_REVIEW",
            "attention_job_id": 33,
        }
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur

        @contextmanager
        def fake_connection(_factory):
            yield conn

        with patch.object(database, "_connection", fake_connection):
            with self.assertRaises(database.DatabaseError) as ctx:
                database.recover_partition_job_attention(9)
        self.assertIn("worker boom", ctx.exception.message)
        self.assertIn("remains pending review", ctx.exception.message)
        conn.transaction.assert_called_once()
        sql = cur.execute.call_args[0][0]
        self.assertIn("run_partition_job_attention_recovery", sql)
        self.assertNotIn("next_run_time", sql)

    def test_attention_lock_conflict_maps_to_409(self) -> None:
        import database

        exc = database.PsycopgError("locked")
        with patch.object(type(exc), "sqlstate", "55P03", create=True):
            mapped = database._attention_error(exc)
        self.assertIsInstance(mapped, database.DatabaseConflictError)


# ---------------------------------------------------------------------------
# 20. Compatibility
# ---------------------------------------------------------------------------
class CompatibilityTests(unittest.TestCase):
    def test_python39_syntax(self) -> None:
        for name in ("retry_policy.py", "scheduler.py", "queue_manager.py", "scheduler_database.py"):
            path = os.path.join(ROOT, "scheduler_backend", name)
            source = _read(path)
            compile(source, path, "exec")
            self.assertNotRegex(source, r"(?m)^\s*match\s+")
        compile(_read(os.path.join(ROOT, "api", "routers", "attention.py")), "attention.py", "exec")

    def test_execute_one_still_uses_to_thread(self) -> None:
        source = inspect.getsource(PartitionScheduler._execute_one)
        self.assertIn("asyncio.to_thread", source)
        self.assertNotIn("time.sleep", inspect.getsource(PartitionScheduler))

    def test_handled_statuses_superset_of_phase0(self) -> None:
        self.assertTrue(
            {"EXECUTED", "FAILED", "FAILED_INVALID_SCHEDULE"} <= set(_HANDLED_OCCURRENCE_STATUSES)
        )
        for status in ("FAILED_DATABASE", "FAILED_CONNECTION", "SKIPPED_TARGET_LOCKED"):
            self.assertNotIn(status, _HANDLED_OCCURRENCE_STATUSES)

    def test_api_routes_registered(self) -> None:
        from api.main import app

        paths = set(app.openapi()["paths"])
        for path in (
            "/api/attention",
            "/api/attention/{attention_id}/acknowledge",
            "/api/attention/{attention_id}/recover",
        ):
            self.assertIn(path, paths)


# ---------------------------------------------------------------------------
# Phase 5A hardening — ownership-based missed-run semantics
# ---------------------------------------------------------------------------
def _executed(job_id, expected):
    return {"status": "EXECUTED", "job_id": job_id, "message": "ok",
            "next_run_time": expected + timedelta(days=1), "is_create": True}


class OwnershipClassificationTests(unittest.TestCase):
    KEY = (33, T_MISSED)

    def test_seen_before_due_is_owned_and_sticky(self) -> None:
        own = OccurrenceOwnership(overdue_tolerance_seconds=60)
        self.assertEqual(own.observe(self.KEY, 90.0), OWNED)
        # Wall clock passes far beyond any old 10-minute threshold.
        self.assertEqual(own.observe(self.KEY, -3 * 3600.0), OWNED)

    def test_within_tolerance_is_owned(self) -> None:
        own = OccurrenceOwnership(overdue_tolerance_seconds=60)
        self.assertEqual(own.observe(self.KEY, -59.0), OWNED)

    def test_first_seen_overdue_is_historical_and_sticky(self) -> None:
        own = OccurrenceOwnership(overdue_tolerance_seconds=60)
        self.assertEqual(own.observe(self.KEY, -5400.0), HISTORICAL)
        self.assertEqual(own.observe(self.KEY, 10.0), HISTORICAL)

    def test_retain_forgets_vanished_but_keeps_active(self) -> None:
        own = OccurrenceOwnership()
        other = (34, T_MISSED)
        own.observe(self.KEY, 10.0)
        own.observe(other, 10.0)
        own.retain([], protected_keys=[other])
        self.assertIsNone(own.get(self.KEY))
        self.assertEqual(own.get(other), OWNED)


class HardeningLoopTests(unittest.TestCase):
    """Real asyncio loop; DB helpers mocked; DB clock expressed via delay."""

    def _patches(self, fetch, execute, reconcile):
        return (
            patch("scheduler_backend.scheduler.fetch_upcoming_jobs", side_effect=fetch),
            patch("scheduler_backend.scheduler.execute_scheduled_job", side_effect=execute),
            patch("scheduler_backend.scheduler.reconcile_occurrence", side_effect=reconcile),
            patch("scheduler_backend.scheduler.count_failed_jobs", return_value=0),
            patch("scheduler_backend.scheduler.count_pending_attention", return_value=0),
        )

    def _run(self, scheduler, fetch, execute, reconcile, seconds=0.8):
        p1, p2, p3, p4, p5 = self._patches(fetch, execute, reconcile)

        async def scenario():
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(seconds)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        with p1, p2, p3, p4, p5:
            asyncio.run(scenario())

    def test_historical_on_startup_is_reconciled_not_executed(self) -> None:
        scheduler = PartitionScheduler(_cfg())
        state = {"advanced": False}
        calls = {"execute": 0, "reconcile": []}

        def fetch(cfg):
            if state["advanced"]:
                # DB moved next_run_time to the first future occurrence.
                return [_job(33, datetime(2026, 10, 27, 2, 0), 86400.0)]
            # Scheduler starts at 10:30; DB says 09:00 (90 min overdue).
            return [_job(33, T_MISSED, -5400.0)]

        def execute(cfg, job_id, expected):
            calls["execute"] += 1
            return _executed(job_id, expected)

        def reconcile(cfg, job_id, expected, reason, detail=None):
            calls["reconcile"].append(reason)
            state["advanced"] = True
            return {"status": "MISSED_RESCHEDULED", "job_id": job_id,
                    "message": "recorded", "is_create": False,
                    "next_run_time": datetime(2026, 10, 27, 2, 0)}

        self._run(scheduler, fetch, execute, reconcile)
        self.assertEqual(calls["execute"], 0)  # no CREATE/DROP path at all
        self.assertEqual(calls["reconcile"], ["MISSED_SCHEDULED_RUN"])
        self.assertEqual(scheduler.status.last_execution_result, "MISSED_RESCHEDULED")
        self.assertTrue(scheduler._handled_occurrences.contains((33, T_MISSED)))

    def test_many_historical_rows_no_catch_up_storm(self) -> None:
        scheduler = PartitionScheduler(_cfg(max_concurrent_jobs=2))
        calls = {"execute": 0, "reconcile": 0}
        jobs = [_job(100 + i, T_MISSED - timedelta(days=i), -86400.0 * (i + 1))
                for i in range(25)]

        def fetch(cfg):
            return [j for j in jobs
                    if not scheduler._handled_occurrences.contains(j.key)]

        def execute(cfg, job_id, expected):
            calls["execute"] += 1
            return _executed(job_id, expected)

        def reconcile(cfg, job_id, expected, reason, detail=None):
            calls["reconcile"] += 1
            return {"status": "MISSED_RESCHEDULED", "job_id": job_id, "message": "",
                    "is_create": True, "next_run_time": None}

        self._run(scheduler, fetch, execute, reconcile, seconds=1.0)
        self.assertEqual(calls["execute"], 0)
        self.assertEqual(calls["reconcile"], 25)  # exactly once each

    def test_owned_occurrence_waiting_for_slot_still_executes(self) -> None:
        """Queued at 10:00, slot busy until >10 min later → still executed."""
        scheduler = PartitionScheduler(_cfg(max_concurrent_jobs=1))
        t_b = datetime(2026, 10, 20, 10, 0, 0)
        release = threading.Event()
        state = {"fetches": 0}
        calls = {"execute": [], "reconcile": []}

        def fetch(cfg):
            state["fetches"] += 1
            # First sighting of B: shortly BEFORE due (owned). Every later
            # refresh reports B >15 min overdue (DB clock keeps moving).
            delay_b = 0.2 if state["fetches"] == 1 else -(900.0 + state["fetches"])
            out = [_job(2, t_b, delay_b)]
            if state["fetches"] == 1:
                out.append(_job(1, t_b, -0.5))  # A: due now, long-running
            return out

        def execute(cfg, job_id, expected):
            calls["execute"].append(job_id)
            if job_id == 1:
                release.wait(timeout=5)
            return _executed(job_id, expected)

        def reconcile(cfg, job_id, expected, reason, detail=None):
            calls["reconcile"].append((job_id, reason))
            return {"status": "MISSED_RESCHEDULED", "job_id": job_id, "message": "",
                    "is_create": True, "next_run_time": None}

        p1, p2, p3, p4, p5 = self._patches(fetch, execute, reconcile)

        async def scenario():
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(0.3)
            # While A holds the only slot, B is reported >10 min overdue.
            await scheduler.refresh_queue("periodic-reconcile")
            await scheduler.refresh_queue("periodic-reconcile")
            self.assertEqual(scheduler._ownership.get((2, t_b)), OWNED)
            release.set()
            await asyncio.sleep(1.5)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        with p1, p2, p3, p4, p5:
            asyncio.run(scenario())
        self.assertEqual(calls["reconcile"], [])
        self.assertEqual(calls["execute"][:2], [1, 2])

    def test_owned_target_lock_contention_is_not_missed_and_not_busy(self) -> None:
        scheduler = PartitionScheduler(_cfg())
        calls = {"execute": 0, "reconcile": 0}

        def fetch(cfg):
            # DB clock says 30 min overdue — but this occurrence was owned.
            return [_job(7, T_MISSED, -1800.0)]

        def execute(cfg, job_id, expected):
            calls["execute"] += 1
            return {"status": "SKIPPED_TARGET_LOCKED", "job_id": job_id,
                    "message": "busy", "next_run_time": expected, "is_create": True}

        def reconcile(cfg, job_id, expected, reason, detail=None):
            calls["reconcile"] += 1
            return {"status": "MISSED_RESCHEDULED", "job_id": job_id, "message": "",
                    "is_create": True, "next_run_time": None}

        # First seen 5 s before due (earlier refresh) → owned.
        own_key = (7, T_MISSED)
        scheduler._ownership.observe(own_key, 5.0)
        self._run(scheduler, fetch, execute, reconcile, seconds=0.8)
        self.assertEqual(scheduler._ownership.get(own_key), OWNED)
        self.assertEqual(calls["reconcile"], 0)  # not turned into a missed run
        self.assertEqual(calls["execute"], 1)  # cooldown ≥ 5 s, no spin

    def test_restart_cannot_create_cpu_loop(self) -> None:
        """Fresh process: previously-owned occurrence is now historical; a failing
        reconcile backs off instead of looping."""
        scheduler = PartitionScheduler(_cfg())
        calls = {"execute": 0, "reconcile": 0}

        def fetch(cfg):
            return [_job(33, T_MISSED, -600.0)]

        def execute(cfg, job_id, expected):
            calls["execute"] += 1
            return _executed(job_id, expected)

        def reconcile(cfg, job_id, expected, reason, detail=None):
            calls["reconcile"] += 1
            return {"status": "FAILED_DATABASE", "job_id": job_id, "message": "",
                    "is_create": None, "next_run_time": None}

        self._run(scheduler, fetch, execute, reconcile, seconds=0.8)
        self.assertEqual(calls["execute"], 0)
        self.assertEqual(calls["reconcile"], 1)
        nb = scheduler._retry.not_before((33, T_MISSED))
        self.assertGreaterEqual(nb - time.monotonic(), 50.0)

    def test_retry_exhaustion_becomes_review_exactly_once(self) -> None:
        scheduler = PartitionScheduler(_cfg(retry_max_attempts=2, retry_delays=(1.0,)))
        recs = []

        def execute(cfg, job_id, expected):
            return {"status": "FAILED_DATABASE", "job_id": job_id, "message": "",
                    "next_run_time": None, "is_create": None}

        def reconcile(cfg, job_id, expected, reason, detail=None):
            recs.append(reason)
            return {"status": "MISSED_RESCHEDULED", "job_id": job_id, "message": "",
                    "is_create": True, "next_run_time": None}

        async def drive():
            with patch("scheduler_backend.scheduler.execute_scheduled_job",
                       side_effect=execute) as ex, \
                    patch("scheduler_backend.scheduler.reconcile_occurrence",
                          side_effect=reconcile):
                for _ in range(8):
                    await scheduler._execute_one(9, T_MISSED)
                return ex.call_count

        executes = asyncio.run(drive())
        self.assertEqual(executes, 2)  # configured attempt limit
        self.assertEqual(recs, ["RETRY_EXHAUSTED"])  # exactly once


class TargetLockBudgetTests(unittest.TestCase):
    def test_long_contention_bounded_and_never_tight(self) -> None:
        tracker = OccurrenceRetryTracker(target_lock_max_wait_seconds=3600.0)
        key = (1, T_MISSED)
        now, attempts, delays = 0.0, 0, []
        while True:
            d = tracker.record_outcome(key, "SKIPPED_TARGET_LOCKED", now_mono=now)
            attempts += 1
            if d.action == ACTION_EXHAUSTED:
                break
            delays.append(d.delay_seconds)
            now += d.delay_seconds
        self.assertGreaterEqual(min(delays), 5.0)
        self.assertGreaterEqual(now, 3600.0 - 30.0)
        self.assertLess(attempts, 130)  # ~1 h / 30 s cooldown
        self.assertEqual(tracker.get(key).failures, 0)


class RetryConfigTests(unittest.TestCase):
    def test_bounded_parsers(self) -> None:
        self.assertEqual(parse_bounded_int(None, 4, 1, 10, "X"), 4)
        self.assertEqual(parse_bounded_int("6", 4, 1, 10, "X"), 6)
        for bad in ("0", "11", "-1", "2.5", "abc"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                parse_bounded_int(bad, 4, 1, 10, "X")
        self.assertEqual(parse_bounded_seconds("", 60.0, 0.0, 600.0, "X"), 60.0)
        for bad in ("-1", "601", "x"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                parse_bounded_seconds(bad, 60.0, 0.0, 600.0, "X")

    def _load(self, env):
        with patch.dict(os.environ, env), patch.object(scheduler_database, "_load_env"):
            return scheduler_database.load_scheduler_config()

    def test_env_driven_retry_settings(self) -> None:
        cfg = self._load({
            "PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS": "3",
            "PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS": "10,20",
            "PARTITION_SCHEDULER_TARGET_LOCK_COOLDOWN_SECONDS": "5,15",
            "PARTITION_SCHEDULER_TARGET_LOCK_MAX_WAIT_SECONDS": "900",
            "PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS": "45",
        })
        self.assertEqual(cfg.retry_max_attempts, 3)
        self.assertEqual(cfg.retry_delays, (10.0, 20.0))
        self.assertEqual(cfg.target_lock_cooldowns, (5.0, 15.0))
        self.assertEqual(cfg.target_lock_max_wait_seconds, 900.0)
        self.assertEqual(cfg.overdue_tolerance_seconds, 45.0)

    def test_invalid_settings_fail_safely(self) -> None:
        for env in (
            {"PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS": "0"},
            {"PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS": "1000"},
            {"PARTITION_SCHEDULER_TARGET_LOCK_MAX_WAIT_SECONDS": "999999"},
            {"PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS": "-5"},
            {"PARTITION_SCHEDULER_LOOKAHEAD_SECONDS": "10",
             "PARTITION_SCHEDULER_RECONCILE_SECONDS": "30"},
        ):
            with self.subTest(env=env), self.assertRaises(RuntimeError):
                self._load(env)

    def test_defaults_ride_out_transient_outage(self) -> None:
        cfg = SchedulerConfig()
        tracker = OccurrenceRetryTracker(
            failure_delays=cfg.retry_delays, max_attempts=cfg.retry_max_attempts)
        window = 0.0
        for _ in range(cfg.retry_max_attempts - 1):
            window += tracker.record_outcome((1, T_MISSED), "FAILED_CONNECTION",
                                             now_mono=window).delay_seconds
        self.assertGreaterEqual(window, 240.0)  # ≥ 4 minutes of retrying
        d = tracker.record_outcome((1, T_MISSED), "FAILED_CONNECTION", now_mono=window)
        self.assertEqual(d.action, ACTION_EXHAUSTED)

    def test_env_example_documents_settings(self) -> None:
        text = _read(os.path.join(ROOT, ".env.realtime.example"))
        for name in (
            "PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS",
            "PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS",
            "PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS",
            "PARTITION_SCHEDULER_TARGET_LOCK_COOLDOWN_SECONDS",
            "PARTITION_SCHEDULER_TARGET_LOCK_MAX_WAIT_SECONDS",
        ):
            self.assertIn(name, text)
        self.assertNotIn("CONTENTION_DELAYS", text)


class DateStyleSafetyTests(unittest.TestCase):
    SAMPLES = [
        ("ISO, DMY", True), ("ISO, MDY", True), ("ISO, YMD", True), ("ISO", True),
        ("iso,dmy", True), (" ISO , DMY ", True),
        ("Redwood, SHOW_TIME", False), ("SQL, DMY", False), ("Postgres, MDY", False),
        ("German", False), ("DMY, ISO", False), ("ISO, DMY; RESET ROLE", False),
        ("ISO, DMY, extra", False), ("", False),
    ]

    def _sql_datestyle_regex(self):
        fn = _function(_read(MIG_01), "partition_job_config_param_allowed")
        pattern = re.search(r"p_value ~\* '([^']+)'", fn).group(1)
        return re.compile(pattern.replace("[[:space:]]", r"\s"), re.IGNORECASE)

    def test_python_and_sql_datestyle_rules_agree(self) -> None:
        from validators import is_allowed_db_config_parameter

        sql_re = self._sql_datestyle_regex()
        for value, expected in self.SAMPLES:
            with self.subTest(value=value):
                self.assertEqual(is_allowed_db_config_parameter("DateStyle", value), expected)
                self.assertEqual(bool(sql_re.match(value)) and 0 < len(value) <= 64, expected)

    def test_every_accepted_style_keeps_iso_output(self) -> None:
        from validators import is_allowed_db_config_parameter

        for value, _ in self.SAMPLES:
            if is_allowed_db_config_parameter("datestyle", value):
                # PostgreSQL DateStyle output format = first component; psycopg
                # decodes ISO output. Order component (DMY/MDY/YMD) only affects input.
                self.assertEqual(value.split(",")[0].strip().upper(), "ISO", value)


class FailedGaugeSemanticsTests(unittest.TestCase):
    def _fake_open(self, executed, fail_first=None):
        @contextmanager
        def fake_open(purpose):
            conn = MagicMock()
            cur = MagicMock()

            def execute(sql, params=None):
                executed.append(sql)
                if fail_first is not None and len(executed) == 1:
                    raise fail_first

            cur.execute.side_effect = execute
            cur.fetchone.return_value = {"failed_count": 2}
            conn.cursor.return_value.__enter__.return_value = cur
            yield conn
        return fake_open

    def test_later_manual_success_clears_failed_gauge(self) -> None:
        executed = []
        with patch.object(scheduler_database, "open_connection", self._fake_open(executed)):
            self.assertEqual(scheduler_database.count_failed_jobs(SchedulerConfig()), 2)
        sql = executed[0]
        self.assertIn("j.last_run_status = 'FAIL'", sql)
        self.assertIn("l.job_runtime > j.last_run_time", sql)
        self.assertIn("'MANUAL_SUCCESS', 'MANUAL_FAIL'", sql)
        self.assertIn("<> 'MANUAL_SUCCESS'", sql)
        self.assertIn("LIMIT 1", sql)  # latest manual outcome wins

    def test_missing_log_grant_falls_back_to_legacy_rule(self) -> None:
        from psycopg.errors import InsufficientPrivilege

        executed = []
        with patch.object(scheduler_database, "open_connection",
                          self._fake_open(executed, fail_first=InsufficientPrivilege("denied"))):
            self.assertEqual(scheduler_database.count_failed_jobs(SchedulerConfig()), 2)
        self.assertEqual(len(executed), 2)
        self.assertNotIn("job_runtime", executed[1])

    def test_last_run_status_semantics_unchanged(self) -> None:
        manual = _body(_read(MIG_02), "run_partition_job_manual")
        recover = _body(_read(MIG_02), "run_partition_job_attention_recovery")
        for body in (manual, recover):
            self.assertNotIn("last_run_status =", body)
            self.assertNotIn("UPDATE mubasher_oms.partitioning_job_table", body)
        self.assertIn(
            "GRANT SELECT ON mubasher_oms.partitioning_job_table_log TO partition_job_scheduler",
            _read(MIG_01),
        )


class AttentionStateMachineSqlTests(unittest.TestCase):
    def test_resolved_items_are_final_and_rerun_needs_success(self) -> None:
        guard = _body(_read(MIG_01), "fn_partition_job_attention_guard")
        self.assertIn("OLD.status IS DISTINCT FROM 'PENDING_REVIEW'", guard)
        self.assertIn("NEW.status = 'MANUALLY_RERUN'", guard)
        self.assertIn("IS DISTINCT FROM 'MANUAL_SUCCESS'", guard)
        for col in ("table_schema", "table_name", "is_create", "job_name",
                    "missed_run_time", "detected_at", "rescheduled_to"):
            self.assertIn("NEW.{}".format(col), guard)

    def test_acknowledge_has_fixed_search_path(self) -> None:
        header = _function(_read(MIG_02), "acknowledge_partition_job_attention").split(
            "AS $function$")[0]
        self.assertIn("SET search_path = pg_catalog, mubasher_oms, pg_temp", header)


if __name__ == "__main__":
    unittest.main()
