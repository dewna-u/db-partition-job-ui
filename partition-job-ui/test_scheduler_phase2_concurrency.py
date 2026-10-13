"""Phase 2 — controlled parallel scheduled job execution tests."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

from scheduler_backend.models import ScheduledJob, SchedulerConfig, SchedulerStatus
from scheduler_backend.scheduler import (
    PartitionScheduler,
    RecentOccurrenceCache,
    _HANDLED_OCCURRENCE_STATUSES,
)
from scheduler_backend.scheduler_database import parse_max_concurrent_jobs


def _job(job_id: int, when: datetime, delay: float = 0.0) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        expected_run_time=when,
        delay_seconds=delay,
        job_name="JOB_TEST",
        is_create=True,
    )


def _extract_scheduled_fn(sql_text: str) -> str:
    start = sql_text.index(
        "CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_scheduled("
    )
    cstart = sql_text.index(
        "COMMENT ON FUNCTION mubasher_oms.run_partition_job_scheduled"
        "(numeric, timestamp without time zone) IS"
    )
    cend = sql_text.index(
        ";", sql_text.index("Does not replace run_partition_job_manual", cstart)
    ) + 1
    return sql_text[start:cend]


class MaxConcurrentConfigTests(unittest.TestCase):
    def test_default_concurrency_is_one(self) -> None:
        self.assertEqual(parse_max_concurrent_jobs(None), 1)
        self.assertEqual(SchedulerConfig().max_concurrent_jobs, 1)

    def test_configured_concurrency_two(self) -> None:
        self.assertEqual(parse_max_concurrent_jobs("2"), 2)

    def test_configured_concurrency_three(self) -> None:
        self.assertEqual(parse_max_concurrent_jobs("3"), 3)

    def test_zero_rejected(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            parse_max_concurrent_jobs("0")
        self.assertIn(">= 1", str(ctx.exception))

    def test_negative_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            parse_max_concurrent_jobs("-1")
        with self.assertRaises(RuntimeError):
            parse_max_concurrent_jobs("-3")

    def test_non_integer_rejected(self) -> None:
        for raw in ("1.5", "2.0", "abc", "1e2", "+2", "  "):
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    parse_max_concurrent_jobs(raw)

    def test_empty_string_rejected(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            parse_max_concurrent_jobs("")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_example_env_defaults_to_one(self) -> None:
        path = os.path.join(os.path.dirname(__file__), ".env.realtime.example")
        text = open(path, encoding="utf-8").read()
        self.assertIn("PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS=1", text)


class SemaphoreConcurrencyTests(unittest.TestCase):
    def test_no_more_than_n_workers_run_simultaneously(self) -> None:
        for n in (1, 2, 3):
            with self.subTest(n=n):
                self._assert_max_active(n)

    def _assert_max_active(self, n: int) -> None:
        config = SchedulerConfig(max_concurrent_jobs=n, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 0, 0)
        gate = threading.Event()
        active = {"n": 0, "peak": 0}
        lock = threading.Lock()
        started = {"n": 0}

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                jobs = [_job(100 + i, when, 0.0) for i in range(n + 2)]
                scheduler.queue.replace_from_jobs(jobs)
                scheduler._update_status_from_queue()
            return True

        async def slow_execute(job_id: int, expected: datetime) -> None:
            with lock:
                active["n"] += 1
                active["peak"] = max(active["peak"], active["n"])
                started["n"] += 1
            try:
                while not gate.is_set():
                    await asyncio.sleep(0.01)
            finally:
                with lock:
                    active["n"] -= 1

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = slow_execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            deadline = time.monotonic() + 2.0
            while started["n"] < n and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertEqual(started["n"], n)
            self.assertLessEqual(active["peak"], n)
            self.assertLessEqual(scheduler.status.active_job_count, n)
            gate.set()
            deadline = time.monotonic() + 2.0
            while started["n"] < n + 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertEqual(started["n"], n + 2)
            self.assertLessEqual(active["peak"], n)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())
        self.assertLessEqual(active["peak"], n)

    def test_n_plus_one_waits_for_slot(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=2, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 5, 0)
        release_first = asyncio.Event()
        order: list[str] = []
        entered = {"n": 0}

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs(
                    [_job(1, when, 0.0), _job(2, when, 0.0), _job(3, when, 0.0)]
                )
                scheduler._update_status_from_queue()
            return True

        async def execute(job_id: int, expected: datetime) -> None:
            entered["n"] += 1
            order.append("start:{}".format(job_id))
            if job_id in (1, 2):
                await release_first.wait()
            order.append("end:{}".format(job_id))

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            deadline = time.monotonic() + 2.0
            while entered["n"] < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertEqual(entered["n"], 2)
            self.assertNotIn("start:3", order)
            release_first.set()
            deadline = time.monotonic() + 2.0
            while "start:3" not in order and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertIn("start:3", order)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())

    def test_blocking_db_uses_to_thread(self) -> None:
        source = inspect.getsource(PartitionScheduler._execute_one)
        self.assertIn("asyncio.to_thread", source)
        self.assertIn("execute_scheduled_job", source)

    def test_event_loop_responsive_while_db_blocked(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=1, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 10, 0)
        entered = threading.Event()
        release = threading.Event()

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs([_job(41, when, 0.0)])
                scheduler._update_status_from_queue()
            return True

        def blocking_execute(cfg, job_id, expected):
            entered.set()
            release.wait(timeout=5)
            return {
                "status": "EXECUTED",
                "job_id": job_id,
                "message": "ok",
                "next_run_time": when,
                "is_create": True,
            }

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=blocking_execute,
            ):
                task = asyncio.create_task(scheduler.run())
                deadline = time.monotonic() + 2.0
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                # Event loop must still schedule while DB thread blocks.
                ping = asyncio.get_running_loop().create_future()

                def _mark() -> None:
                    if not ping.done():
                        ping.set_result(True)

                asyncio.get_running_loop().call_soon(_mark)
                await asyncio.wait_for(ping, timeout=1.0)
                self.assertTrue(scheduler.status.scheduler_active)
                self.assertEqual(scheduler.status.active_job_count, 1)
                release.set()
                scheduler.request_shutdown()
                await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())

    def test_health_logic_remains_responsive(self) -> None:
        status = SchedulerStatus(
            scheduler_active=True, active_job_count=2, max_concurrent_jobs=3
        )
        # /health only checks scheduler_active — active jobs are healthy.
        self.assertTrue(status.scheduler_active)
        self.assertGreater(status.active_job_count, 0)
        payload = status.as_dict()
        self.assertEqual(payload["active_job_count"], 2)
        self.assertEqual(payload["max_concurrent_jobs"], 3)

    def test_status_reports_concurrency_fields(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=3)
        scheduler = PartitionScheduler(config)
        scheduler._active_count = 2
        scheduler._update_concurrency_status()
        payload = scheduler.status.as_dict()
        self.assertEqual(payload["max_concurrent_jobs"], 3)
        self.assertEqual(payload["active_job_count"], 2)
        self.assertEqual(payload["available_slots"], 1)
        self.assertLessEqual(
            payload["active_job_count"], payload["max_concurrent_jobs"]
        )

    def test_active_count_never_exceeds_maximum(self) -> None:
        self._assert_max_active(2)

    def test_completed_tasks_cleaned_up(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=2, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 15, 0)

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs(
                    [_job(51, when, 0.0), _job(52, when, 0.0)]
                )
                scheduler._update_status_from_queue()
            return True

        async def execute(job_id: int, expected: datetime) -> None:
            await asyncio.sleep(0.05)

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(0.35)
            self.assertEqual(len(scheduler._active_tasks), 0)
            self.assertEqual(scheduler.status.active_job_count, 0)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())

    def test_task_exceptions_retrieved(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=1, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 20, 0)

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs([_job(61, when, 0.0)])
                scheduler._update_status_from_queue()
            return True

        async def boom(job_id: int, expected: datetime) -> None:
            raise RuntimeError("worker boom")

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = boom  # type: ignore[method-assign]

        async def scenario() -> None:
            with self.assertLogs("scheduler_backend.scheduler", level="ERROR") as cm:
                task = asyncio.create_task(scheduler.run())
                await asyncio.sleep(0.25)
                scheduler.request_shutdown()
                await asyncio.wait_for(task, timeout=3)
            self.assertTrue(
                any("exception" in line.lower() for line in cm.output),
                msg=cm.output,
            )

        asyncio.run(scenario())

    def test_one_failed_job_does_not_cancel_others(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=2, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 25, 0)
        finished: list[int] = []

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs(
                    [_job(71, when, 0.0), _job(72, when, 0.0)]
                )
                scheduler._update_status_from_queue()
            return True

        async def execute(job_id: int, expected: datetime) -> None:
            if job_id == 71:
                raise RuntimeError("fail A")
            await asyncio.sleep(0.05)
            finished.append(job_id)

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(0.3)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())
        self.assertIn(72, finished)

    def test_db_failure_releases_semaphore_slot(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=1, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 30, 0)
        second_started = asyncio.Event()

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs(
                    [_job(81, when, 0.0), _job(82, when, 0.0)]
                )
                scheduler._update_status_from_queue()
            return True

        async def execute(job_id: int, expected: datetime) -> None:
            if job_id == 81:
                raise RuntimeError("db fail")
            second_started.set()

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            await asyncio.wait_for(second_started.wait(), timeout=2)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())
        self.assertTrue(second_started.is_set())

    def test_shutdown_does_not_leak_tasks(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=2, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 35, 0)
        started = asyncio.Event()

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs([_job(91, when, 0.0)])
                scheduler._update_status_from_queue()
            return True

        async def execute(job_id: int, expected: datetime) -> None:
            started.set()
            await asyncio.sleep(0.2)

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            await asyncio.wait_for(started.wait(), timeout=2)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)
            self.assertEqual(len(scheduler._active_tasks), 0)
            self.assertFalse(scheduler.status.scheduler_active)

        asyncio.run(scenario())

    def test_refresh_while_worker_runs(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=1, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 40, 0)
        entered = asyncio.Event()
        release = asyncio.Event()
        refresh_reasons: list[str] = []

        async def fake_refresh(reason: str) -> bool:
            refresh_reasons.append(reason)
            if reason == "startup":
                scheduler.queue.replace_from_jobs([_job(101, when, 0.0)])
                scheduler._update_status_from_queue()
            return True

        async def execute(job_id: int, expected: datetime) -> None:
            entered.set()
            await release.wait()

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            await asyncio.wait_for(entered.wait(), timeout=2)
            before = len(refresh_reasons)
            scheduler.request_refresh()
            deadline = time.monotonic() + 2.0
            while len(refresh_reasons) <= before and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            self.assertGreater(len(refresh_reasons), before)
            self.assertIn("http-refresh", refresh_reasons)
            release.set()
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())

    def test_duplicate_occurrence_not_spawned_twice(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=2, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 12, 45, 0)
        calls: list[int] = []
        hold = asyncio.Event()

        async def fake_refresh(reason: str) -> bool:
            # Keep rediscovering the same occurrence while first is active.
            scheduler.queue.replace_from_jobs([_job(111, when, 0.0)])
            scheduler._update_status_from_queue()
            return True

        async def execute(job_id: int, expected: datetime) -> None:
            calls.append(job_id)
            await hold.wait()

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            deadline = time.monotonic() + 2.0
            while not calls and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            scheduler.request_refresh()
            await asyncio.sleep(0.15)
            self.assertEqual(calls, [111])
            hold.set()
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())


class TargetAdvisoryLockSqlTests(unittest.TestCase):
    @classmethod
    def _canonical(cls) -> str:
        path = os.path.join(
            os.path.dirname(__file__), "sql", "realtime_scheduler_v1.sql"
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    @classmethod
    def _migration(cls) -> str:
        path = os.path.join(
            os.path.dirname(__file__),
            "sql",
            "migrations",
            "20261013_01_scheduled_target_advisory_lock.sql",
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_migration_contains_target_lock_logic(self) -> None:
        text = self._migration()
        self.assertIn("BEGIN;", text)
        self.assertIn("COMMIT;", text)
        self.assertIn("87421032", text)
        self.assertIn("87421031", text)
        self.assertIn("SKIPPED_TARGET_LOCKED", text)
        self.assertIn("partition_job_target:", text)
        self.assertIn("pg_try_advisory_xact_lock", text)
        self.assertNotIn("DROP FUNCTION", text)
        self.assertNotIn("CASCADE", text)

    def test_canonical_matches_migration_behaviour(self) -> None:
        canon_fn = _extract_scheduled_fn(self._canonical())
        mig_fn = _extract_scheduled_fn(self._migration())
        self.assertEqual(canon_fn, mig_fn)

    def test_lock_order_job_then_row_then_target(self) -> None:
        fn = _extract_scheduled_fn(self._canonical())
        job_lock = fn.find("87421031")
        for_update = fn.find("FOR UPDATE")
        target_lock = fn.find("87421032")
        cron = fn.find("cron_to_interval_or_next_run")
        create = fn.find("create_any_table_partition")
        self.assertGreater(for_update, job_lock)
        self.assertGreater(target_lock, for_update)
        self.assertGreater(cron, target_lock)
        self.assertGreater(create, cron)

    def test_same_target_serialized_create_and_drop(self) -> None:
        fn = _extract_scheduled_fn(self._canonical())
        # Single target key from schema+name — CREATE and DROP share it.
        self.assertIn("v_job.table_schema", fn)
        self.assertIn("v_job.table_name", fn)
        self.assertIn("SKIPPED_TARGET_LOCKED", fn)
        # Worker paths remain after the target lock.
        target_at = fn.find("SKIPPED_TARGET_LOCKED")
        self.assertGreater(fn.find("create_any_table_partition", target_at), target_at)
        self.assertGreater(fn.find("drop_any_table_partition", target_at), target_at)

    def test_job_lock_namespace_preserved(self) -> None:
        fn = _extract_scheduled_fn(self._canonical())
        self.assertIn("partition_job_scheduled:", fn)
        self.assertIn("SKIPPED_LOCKED", fn)

    def test_skipped_target_locked_not_cached(self) -> None:
        self.assertNotIn("SKIPPED_TARGET_LOCKED", _HANDLED_OCCURRENCE_STATUSES)
        config = SchedulerConfig(max_concurrent_jobs=1, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 14, 0, 0)

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs([_job(201, when, 0.0)])
                scheduler._update_status_from_queue()
            return True

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                return_value={
                    "status": "SKIPPED_TARGET_LOCKED",
                    "job_id": 201,
                    "message": "target locked",
                    "next_run_time": when,
                    "is_create": True,
                },
            ):
                task = asyncio.create_task(scheduler.run())
                await asyncio.sleep(0.25)
                scheduler.request_shutdown()
                await asyncio.wait_for(task, timeout=3)

        asyncio.run(scenario())
        self.assertEqual(
            scheduler.status.last_execution_result, "SKIPPED_TARGET_LOCKED"
        )
        self.assertFalse(
            scheduler._handled_occurrences.contains((201, when))
        )

    def test_phase0_protections_still_present(self) -> None:
        fn = _extract_scheduled_fn(self._canonical())
        self.assertIn("FAILED_INVALID_SCHEDULE", fn)
        self.assertIn("Fail closed", fn)
        self.assertIn("SKIPPED_RESCHEDULED", fn)
        self.assertIn("SKIPPED_DISABLED", fn)
        self.assertNotIn("v_now + interval '1 day'", fn)


class ConcurrentConnectionIsolationTests(unittest.TestCase):
    def test_concurrent_jobs_use_independent_mocked_connections(self) -> None:
        from scheduler_backend import scheduler_database

        connections = []

        @contextmanager
        def fake_open(purpose: str):
            conn = MagicMock(name="conn-{}".format(len(connections)))
            connections.append(conn)
            txn = MagicMock()
            txn.__enter__ = MagicMock(return_value=None)
            txn.__exit__ = MagicMock(return_value=False)
            conn.transaction.return_value = txn
            cur = MagicMock()
            cur.fetchone.return_value = {
                "status": "EXECUTED",
                "job_id": 1,
                "message": "ok",
                "next_run_time": datetime(2026, 10, 14, 0, 0, 0),
                "is_create": True,
            }
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cur)
            cm.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cm
            yield conn

        when = datetime(2026, 10, 13, 13, 0, 0)
        config = SchedulerConfig()
        with patch.object(scheduler_database, "open_connection", fake_open):
            r1 = scheduler_database.execute_scheduled_job(config, 31, when)
            r2 = scheduler_database.execute_scheduled_job(config, 32, when)
        self.assertEqual(r1["status"], "EXECUTED")
        self.assertEqual(r2["status"], "EXECUTED")
        self.assertEqual(len(connections), 2)
        self.assertIsNot(connections[0], connections[1])


class Python39CompatibilityTests(unittest.TestCase):
    def test_scheduler_source_is_python39_compatible_syntax(self) -> None:
        root = os.path.join(os.path.dirname(__file__), "scheduler_backend")
        for name in ("scheduler.py", "scheduler_database.py", "models.py", "main.py"):
            path = os.path.join(root, name)
            source = open(path, encoding="utf-8").read()
            # No match/case, no type X =, no walrus-only requirements beyond 3.8.
            self.assertNotRegex(source, r"(?m)^\s*match\s+")
            self.assertNotRegex(source, r"(?m)^\s*case\s+")
            compile(source, path, "exec")
        self.assertGreaterEqual(sys.version_info[:2], (3, 9))


class SecretCacheConcurrencySmokeTests(unittest.TestCase):
    def test_secret_cache_still_imported_by_scheduler_db(self) -> None:
        from scheduler_backend import scheduler_database

        source = inspect.getsource(scheduler_database)
        self.assertIn("resolve_database_credentials", source)
        self.assertIn("invalidate_database_secret_cache", source)
        self.assertIn("is_authentication_error", source)


if __name__ == "__main__":
    unittest.main()
