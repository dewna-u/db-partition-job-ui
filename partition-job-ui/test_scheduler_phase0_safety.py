"""Phase 0 — scheduler occurrence safety / tight-loop regression tests."""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.scheduler import PartitionScheduler


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


class OccurrenceAdvancementSqlContractTests(unittest.TestCase):
    @classmethod
    def _sql(cls) -> str:
        path = os.path.join(
            os.path.dirname(__file__), "sql", "realtime_scheduler_v1.sql"
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_rejects_same_or_non_future_next_run(self) -> None:
        text = self._sql()
        self.assertIn(
            "v_new_next_run IS NOT DISTINCT FROM p_expected_run_time", text
        )
        self.assertIn("v_new_next_run <= p_expected_run_time", text)
        self.assertIn("v_new_next_run <= v_now", text)
        self.assertIn("controlled fallback", text)
        self.assertIn("v_job.frequency", text)

    def test_stale_and_disabled_still_skip_workers(self) -> None:
        text = self._sql()
        self.assertIn("SKIPPED_RESCHEDULED", text)
        self.assertIn("SKIPPED_DISABLED", text)
        self.assertIn("IS DISTINCT FROM p_expected_run_time", text)
        # Stale/disabled paths return before create/drop workers.
        stale = text.split("IF v_job.next_run_time IS DISTINCT FROM p_expected_run_time")[
            1
        ].split("END IF;")[0]
        self.assertNotIn("create_any_table_partition", stale)
        disabled = text.split("IF v_job.is_enabled IS NOT TRUE THEN")[1].split("END IF;")[
            0
        ]
        self.assertNotIn("drop_any_table_partition", disabled)

    def test_migration_mirrors_safety_clause(self) -> None:
        path = os.path.join(
            os.path.dirname(__file__),
            "sql",
            "migrations",
            "20261007_01_run_partition_job_scheduled_occurrence_safety.sql",
        )
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("CREATE OR REPLACE FUNCTION", text)
        self.assertIn("v_new_next_run IS NOT DISTINCT FROM p_expected_run_time", text)
        self.assertNotIn("DROP FUNCTION", text)


class TightLoopRegressionTests(unittest.TestCase):
    def test_same_occurrence_cannot_execute_twice_after_executed(self) -> None:
        """Reproduce observed (J,T) rediscovery; circuit breaker must stop loop."""
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        async def fake_refresh(reason: str) -> bool:
            # Pathological DB: still returns the same occurrence after EXECUTED.
            jobs = [_job(33, t, -1.0)]
            filtered = [
                j
                for j in jobs
                if (j.job_id, j.expected_run_time) not in scheduler._handled_occurrences
            ]
            scheduler.queue.replace_from_jobs(filtered, now_mono=1000.0)
            scheduler._update_status_from_queue()
            scheduler.status.last_refresh_result = f"ok:{reason}:1"
            return True

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "EXECUTED",
                "job_id": job_id,
                "message": "DROP partition operation completed.",
                # Bug shape: next_run_time not advanced past T.
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
                # First due pop + execute + refresh
                due = scheduler.queue.pop_due(now_mono=1000.0)
                self.assertEqual(due, (33, t))
                await scheduler._execute_one(33, t)
                await scheduler.refresh_queue("post-execution")
                # Queue must not keep the same occurrence after breaker filter.
                self.assertIsNone(scheduler.queue.peek())
                # Direct re-fire must also be refused.
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
            # Apply same filter path as production refresh_queue.
            filtered = [
                j
                for j in jobs
                if (j.job_id, j.expected_run_time) not in scheduler._handled_occurrences
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
                self.assertEqual(peeked[1], 33)
                self.assertEqual(peeked[2], t2)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)

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
                self.assertNotIn((33, t), scheduler._handled_occurrences)
                await scheduler._execute_one(33, t)

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 2)
        self.assertEqual(scheduler.status.last_execution_result, "EXECUTED")

    def test_failed_worker_still_suppresses_same_occurrence(self) -> None:
        t = datetime(2026, 9, 18, 0, 15, 0)
        scheduler = PartitionScheduler(_config())
        calls = {"n": 0}

        def fake_execute(config, job_id, expected):
            calls["n"] += 1
            return {
                "status": "FAILED",
                "job_id": job_id,
                "message": "worker boom",
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
        self.assertEqual(
            scheduler.status.last_execution_result, "SKIPPED_CIRCUIT_BREAKER"
        )


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
