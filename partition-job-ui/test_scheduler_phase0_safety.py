"""Phase 0 — scheduler occurrence safety / fail-closed / circuit-breaker tests."""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.scheduler import (
    PartitionScheduler,
    RecentOccurrenceCache,
    _CIRCUIT_BREAKER_MAX_ENTRIES,
    _CIRCUIT_BREAKER_TTL_SECONDS,
    _HANDLED_OCCURRENCE_STATUSES,
)


def _config() -> SchedulerConfig:
    return SchedulerConfig(lookahead_seconds=120.0, reconcile_seconds=30.0)


def _job(job_id: int, when: datetime, delay: float = 0.0) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        expected_run_time=when,
        delay_seconds=delay,
        job_name="JOB_TEST",
        is_create=False,
    )


def _extract_function(sql_text: str) -> str:
    start = sql_text.index(
        "CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_scheduled("
    )
    cstart = sql_text.index(
        "COMMENT ON FUNCTION mubasher_oms.run_partition_job_scheduled"
        "(numeric, timestamp without time zone) IS"
    )
    cend = sql_text.index(";", sql_text.index("Does not replace run_partition_job_manual", cstart)) + 1
    return sql_text[start:cend]


class OccurrenceAdvancementSqlContractTests(unittest.TestCase):
    @classmethod
    def _sql(cls) -> str:
        path = os.path.join(
            os.path.dirname(__file__), "sql", "realtime_scheduler_v1.sql"
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_fail_closed_no_arbitrary_day_or_frequency_fallback(self) -> None:
        text = self._sql()
        fn = _extract_function(text)
        self.assertIn("FAILED_INVALID_SCHEDULE", fn)
        self.assertIn("Fail closed", fn)
        self.assertIn("v_new_next_run IS NOT DISTINCT FROM p_expected_run_time", fn)
        self.assertIn("v_new_next_run <= p_expected_run_time", fn)
        self.assertIn("v_new_next_run <= v_now", fn)
        self.assertNotIn("v_now + interval '1 day'", fn)
        self.assertNotIn("v_job.frequency", fn)
        self.assertNotIn("COALESCE(v_new_next_run, v_now + interval '1 day')", fn)

    def test_next_run_validated_before_workers(self) -> None:
        fn = _extract_function(self._sql())
        calc_at = fn.find("cron_to_interval_or_next_run")
        # Helper-invalid path (not the earlier is_create IS NULL fail-closed).
        helper_invalid_at = fn.find("Invalid next_run_time from schedule helper")
        create_at = fn.find("create_any_table_partition")
        self.assertGreater(calc_at, 0)
        self.assertGreater(helper_invalid_at, calc_at)
        self.assertGreater(create_at, helper_invalid_at)

    def test_stale_and_disabled_still_skip_workers(self) -> None:
        text = self._sql()
        self.assertIn("SKIPPED_RESCHEDULED", text)
        self.assertIn("SKIPPED_DISABLED", text)
        stale = text.split("IF v_job.next_run_time IS DISTINCT FROM p_expected_run_time")[
            1
        ].split("END IF;")[0]
        self.assertNotIn("create_any_table_partition", stale)
        disabled = text.split("IF v_job.is_enabled IS NOT TRUE THEN")[1].split("END IF;")[
            0
        ]
        self.assertNotIn("drop_any_table_partition", disabled)

    def test_null_is_create_fail_closed_clears_next_run(self) -> None:
        text = self._sql()
        null_block = text.split("IF v_job.is_create IS NULL THEN", 1)[1].split(
            "END IF;", 1
        )[0]
        self.assertIn("FAILED_INVALID_SCHEDULE", null_block)
        self.assertIn("next_run_time   = NULL", null_block)
        self.assertNotIn("interval '1 day'", null_block)

    def test_migration_matches_canonical_function_body(self) -> None:
        root = os.path.dirname(__file__)
        with open(
            os.path.join(root, "sql", "realtime_scheduler_v1.sql"), encoding="utf-8"
        ) as handle:
            canonical = _extract_function(handle.read())
        mig_path = os.path.join(
            root,
            "sql",
            "migrations",
            "20261007_01_run_partition_job_scheduled_occurrence_safety.sql",
        )
        with open(mig_path, encoding="utf-8") as handle:
            migration = handle.read()
        migrated = _extract_function(migration)
        # Normalize whitespace for BOM / newline differences.
        self.assertEqual(
            re.sub(r"\s+", " ", canonical.strip()),
            re.sub(r"\s+", " ", migrated.strip()),
        )


class TightLoopRegressionTests(unittest.TestCase):
    def test_same_occurrence_cannot_execute_twice_after_executed(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        async def fake_refresh(reason: str) -> bool:
            jobs = [_job(33, t, -1.0)]
            filtered = [
                j
                for j in jobs
                if not scheduler._handled_occurrences.contains(
                    (j.job_id, j.expected_run_time)
                )
            ]
            scheduler.queue.replace_from_jobs(filtered, now_mono=1000.0)
            scheduler._update_status_from_queue()
            return True

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "EXECUTED",
                "job_id": job_id,
                "message": "DROP partition operation completed.",
                "next_run_time": expected,
                "is_create": False,
            }

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler.refresh_queue("startup")
                due = scheduler.queue.pop_due(now_mono=1000.0)
                self.assertEqual(due, (33, t))
                await scheduler._execute_one(33, t)
                await scheduler.refresh_queue("post-execution")
                self.assertIsNone(scheduler.queue.peek())
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            scheduler.status.last_execution_result, "SKIPPED_CIRCUIT_BREAKER"
        )

    def test_successful_advance_allows_different_occurrence(self) -> None:
        t1 = datetime(2026, 9, 18, 0, 15, 0)
        t2 = datetime(2026, 10, 7, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        async def fake_refresh(reason: str) -> bool:
            if calls["n"] == 0:
                jobs = [_job(33, t1, -1.0)]
            else:
                jobs = [_job(33, t2, 3600.0)]
            filtered = [
                j
                for j in jobs
                if not scheduler._handled_occurrences.contains(
                    (j.job_id, j.expected_run_time)
                )
            ]
            scheduler.queue.replace_from_jobs(filtered, now_mono=1000.0)
            scheduler._update_status_from_queue()
            return True

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "EXECUTED",
                "job_id": job_id,
                "message": "ok",
                "next_run_time": t2,
                "is_create": True,
            }

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler.refresh_queue("startup")
                due = scheduler.queue.pop_due(now_mono=1000.0)
                self.assertEqual(due[0], 33)
                await scheduler._execute_one(33, t1)
                await scheduler.refresh_queue("post-execution")
                peeked = scheduler.queue.peek()
                self.assertIsNotNone(peeked)
                self.assertEqual(peeked[2], t2)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)

    def test_failed_database_is_not_cached(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "FAILED_DATABASE",
                "job_id": job_id,
                "message": "Database error; transaction did not complete.",
                "next_run_time": None,
                "is_create": None,
            }

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler._execute_one(33, t)
                self.assertEqual(
                    scheduler.status.last_execution_result, "FAILED_DATABASE"
                )
                self.assertFalse(scheduler._handled_occurrences.contains((33, t)))
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 2)

    def test_skipped_not_due_and_locked_are_not_cached(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        for status in ("SKIPPED_NOT_DUE", "SKIPPED_LOCKED"):
            scheduler = PartitionScheduler(_config())
            calls = {"n": 0}

            def fake_execute(config, job_id, expected, _status=status):
                calls["n"] += 1
                return {
                    "status": _status,
                    "job_id": job_id,
                    "message": _status,
                    "next_run_time": expected,
                    "is_create": False,
                }

            async def scenario() -> None:
                with patch(
                    "scheduler_backend.scheduler.execute_scheduled_job",
                    side_effect=fake_execute,
                ):
                    await scheduler._execute_one(33, t)
                    self.assertFalse(
                        scheduler._handled_occurrences.contains((33, t))
                    )
                    await scheduler._execute_one(33, t)

            asyncio.run(scenario())
            self.assertEqual(calls["n"], 2, msg=status)

    def test_connection_failure_does_not_suppress_occurrence(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db down")
            return {
                "status": "EXECUTED",
                "job_id": job_id,
                "message": "ok",
                "next_run_time": expected + timedelta(days=1),
                "is_create": False,
            }

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler._execute_one(33, t)
                self.assertEqual(
                    scheduler.status.last_execution_result, "FAILED_CONNECTION"
                )
                self.assertFalse(scheduler._handled_occurrences.contains((33, t)))
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 2)

    def test_invalid_schedule_result_is_cached_as_handled(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "FAILED_INVALID_SCHEDULE",
                "job_id": job_id,
                "message": "invalid next",
                "next_run_time": None,
                "is_create": False,
            }

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler._execute_one(33, t)
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            scheduler.status.last_execution_result, "SKIPPED_CIRCUIT_BREAKER"
        )

    def test_failed_worker_result_is_cached(self) -> None:
        """FAILED means DB committed log + advanced next_run_time — cache it."""
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "FAILED",
                "job_id": job_id,
                "message": "Partition worker raised an error.",
                "next_run_time": expected + timedelta(days=1),
                "is_create": False,
            }

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler._execute_one(33, t)
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)
        self.assertTrue(scheduler._handled_occurrences.contains((33, t)))

    def test_skipped_disabled_is_not_cached(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "SKIPPED_DISABLED",
                "job_id": job_id,
                "message": "Job is disabled.",
                "next_run_time": expected,
                "is_create": False,
            }

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler._execute_one(33, t)
                self.assertFalse(scheduler._handled_occurrences.contains((33, t)))
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 2)

    def test_skipped_rescheduled_is_not_cached(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "SKIPPED_RESCHEDULED",
                "job_id": job_id,
                "message": "stale timer",
                "next_run_time": expected + timedelta(hours=1),
                "is_create": False,
            }

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler._execute_one(33, t)
                self.assertFalse(scheduler._handled_occurrences.contains((33, t)))
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 2)

    def test_not_found_is_not_cached(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "NOT_FOUND",
                "job_id": None,
                "message": "Job not found.",
                "next_run_time": None,
                "is_create": None,
            }

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=fake_execute,
            ):
                await scheduler._execute_one(33, t)
                self.assertFalse(scheduler._handled_occurrences.contains((33, t)))
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 2)

    def test_skipped_results_leave_db_authoritative_on_refresh(self) -> None:
        """After SKIPPED_DISABLED, refresh still admits the same occurrence."""
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                return_value={
                    "status": "SKIPPED_DISABLED",
                    "job_id": 33,
                    "message": "disabled",
                    "next_run_time": t,
                    "is_create": False,
                },
            ):
                await scheduler._execute_one(33, t)

            with patch(
                "scheduler_backend.scheduler.fetch_upcoming_jobs",
                return_value=[_job(33, t, -1.0)],
            ):
                ok = await scheduler.refresh_queue("post-skip")
            self.assertTrue(ok)
            peeked = scheduler.queue.peek()
            self.assertIsNotNone(peeked)
            self.assertEqual(peeked[1], 33)
            self.assertEqual(peeked[2], t)

        asyncio.run(scenario())


class CircuitBreakerBoundTests(unittest.TestCase):
    def test_handled_status_policy(self) -> None:
        self.assertEqual(
            _HANDLED_OCCURRENCE_STATUSES,
            frozenset({"EXECUTED", "FAILED", "FAILED_INVALID_SCHEDULE"}),
        )
        for status in (
            "FAILED_DATABASE",
            "FAILED_CONNECTION",
            "SKIPPED_DISABLED",
            "SKIPPED_RESCHEDULED",
            "NOT_FOUND",
            "SKIPPED_NOT_DUE",
            "SKIPPED_LOCKED",
        ):
            self.assertNotIn(status, _HANDLED_OCCURRENCE_STATUSES)

    def test_ttl_expires_entries(self) -> None:
        cache = RecentOccurrenceCache(max_entries=10, ttl_seconds=10.0)
        key = (1, datetime(2026, 1, 1, 0, 0, 0))
        cache.add(key, "EXECUTED", now_mono=100.0)
        self.assertTrue(cache.contains(key, now_mono=105.0))
        self.assertFalse(cache.contains(key, now_mono=111.0))

    def test_max_entries_prunes_oldest(self) -> None:
        cache = RecentOccurrenceCache(max_entries=3, ttl_seconds=1000.0)
        for i in range(5):
            cache.add((i, datetime(2026, 1, 1, 0, 0, 0)), "EXECUTED", now_mono=float(i))
        self.assertEqual(len(cache), 3)
        self.assertFalse(cache.contains((0, datetime(2026, 1, 1, 0, 0, 0)), now_mono=10.0))
        self.assertTrue(cache.contains((4, datetime(2026, 1, 1, 0, 0, 0)), now_mono=10.0))

    def test_default_bounds_are_rapid_loop_window(self) -> None:
        self.assertEqual(_CIRCUIT_BREAKER_MAX_ENTRIES, 512)
        self.assertEqual(_CIRCUIT_BREAKER_TTL_SECONDS, 300.0)


class ScheduleQueueDuplicateTests(unittest.TestCase):
    def test_replace_dedupes_same_occurrence_key(self) -> None:
        from scheduler_backend.queue_manager import ScheduleQueue

        t = datetime(2026, 9, 18, 0, 15, 0)
        q = ScheduleQueue()
        q.replace_from_jobs([_job(33, t, -5.0), _job(33, t, -5.0)], now_mono=100.0)
        self.assertEqual(len(q), 1)
        first = q.pop_due(100.0)
        self.assertEqual(first, (33, t))
        self.assertIsNone(q.pop_due(100.0))


if __name__ == "__main__":
    unittest.main()
