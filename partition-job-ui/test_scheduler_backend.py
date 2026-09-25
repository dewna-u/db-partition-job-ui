"""Unit tests for the realtime scheduler backend (mocked DB — no live DDL)."""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from scheduler_backend.models import ScheduledJob, SchedulerConfig, SchedulerStatus
from scheduler_backend.queue_manager import ScheduleQueue
from scheduler_backend import scheduler_database
from scheduler_backend.scheduler import PartitionScheduler
from scheduler_backend.main import ProcessSingletonLock
import scheduler_client


def _job(
    job_id: int,
    when: datetime,
    delay: float,
    *,
    name: str = "j",
    is_create: bool = True,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        expected_run_time=when,
        delay_seconds=delay,
        job_name=name,
        is_create=is_create,
    )


class ConfigSafetyTests(unittest.TestCase):
    def test_realtime_example_exists_and_has_placeholders(self) -> None:
        path = os.path.join(os.path.dirname(__file__), ".env.realtime.example")
        self.assertTrue(os.path.isfile(path))
        text = open(path, encoding="utf-8").read()
        self.assertIn("your_new_realtime_database", text)
        self.assertIn("replace_with_strong_password", text)
        self.assertIn("PARTITION_SCHEDULER_LOOKAHEAD_SECONDS", text)
        self.assertIn("REALTIME_DB_EXPECTED_NAME", text)

    def test_gitignore_keeps_realtime_env_secret(self) -> None:
        text = open(
            os.path.join(os.path.dirname(__file__), ".gitignore"), encoding="utf-8"
        ).read()
        self.assertIn(".env.*", text)
        self.assertIn("!.env.realtime.example", text)

    def test_env_example_untouched_pattern(self) -> None:
        example = open(
            os.path.join(os.path.dirname(__file__), ".env.example"), encoding="utf-8"
        ).read()
        self.assertIn("DB_NAME=your_database", example)


class NoPoolNoPersistentConnectionTests(unittest.TestCase):
    def test_no_connection_pool_in_scheduler_modules(self) -> None:
        root = os.path.dirname(__file__)
        forbidden = (
            "ConnectionPool",
            "AsyncConnectionPool",
            "psycopg_pool",
            "create_pool",
        )
        for name in (
            "scheduler_backend/scheduler_database.py",
            "scheduler_backend/scheduler.py",
            "scheduler_backend/main.py",
            "database.py",
            "scheduler_client.py",
        ):
            path = os.path.join(root, name.replace("/", os.sep))
            source = open(path, encoding="utf-8").read()
            for token in forbidden:
                self.assertNotIn(token, source, msg=f"{name} contains {token}")
            # Operational LISTEN sessions are forbidden (comments may mention the word).
            self.assertNotRegex(
                source,
                r"(?im)^\s*[^#\"'].*\bLISTEN\b",
                msg=f"{name} appears to use LISTEN",
            )

    def test_no_module_level_connection_object(self) -> None:
        source = inspect.getsource(scheduler_database)
        self.assertNotIn("\n_conn ", source)
        self.assertNotIn("global_connection", source)
        self.assertIn("def open_connection", source)


class QueueManagerTests(unittest.TestCase):
    def test_orders_by_due_then_job_id(self) -> None:
        base = datetime(2026, 8, 20, 10, 0, 0)
        q = ScheduleQueue()
        now = 1000.0
        q.replace_from_jobs(
            [
                _job(30, base + timedelta(seconds=20), 20.0),
                _job(10, base + timedelta(seconds=5), 5.0),
                _job(20, base + timedelta(seconds=5), 5.0),
            ],
            now_mono=now,
        )
        first = q.pop_due(now + 5.0)
        second = q.pop_due(now + 5.0)
        self.assertEqual(first, (10, base + timedelta(seconds=5)))
        self.assertEqual(second, (20, base + timedelta(seconds=5)))

    def test_refresh_rebuild_removes_stale_and_disabled(self) -> None:
        base = datetime(2026, 8, 20, 10, 0, 0)
        q = ScheduleQueue()
        q.replace_from_jobs(
            [_job(1, base, 1.0), _job(2, base + timedelta(seconds=10), 10.0)],
            now_mono=0.0,
        )
        self.assertEqual(len(q), 2)
        q.replace_from_jobs([_job(2, base + timedelta(seconds=30), 30.0)], now_mono=0.0)
        self.assertEqual(len(q), 1)
        peeked = q.peek()
        assert peeked is not None
        self.assertEqual(peeked[1], 2)
        self.assertEqual(peeked[2], base + timedelta(seconds=30))

    def test_same_time_jobs_deterministic(self) -> None:
        when = datetime(2026, 8, 20, 10, 5, 0)
        q = ScheduleQueue()
        q.replace_from_jobs(
            [_job(12, when, 0.0), _job(10, when, 0.0), _job(11, when, 0.0)],
            now_mono=50.0,
        )
        order = []
        while True:
            item = q.pop_due(50.0)
            if item is None:
                break
            order.append(item[0])
        self.assertEqual(order, [10, 11, 12])

    def test_overdue_negative_delay_is_immediately_due(self) -> None:
        when = datetime(2026, 8, 20, 10, 0, 0)
        q = ScheduleQueue()
        q.replace_from_jobs([_job(7, when, -12.5)], now_mono=100.0)
        self.assertEqual(q.pop_due(100.0), (7, when))


class SchedulerDatabaseLifecycleTests(unittest.TestCase):
    def test_fetch_upcoming_opens_and_closes(self) -> None:
        config = SchedulerConfig()
        conn = MagicMock()
        conn.closed = False
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        cursor.fetchall.return_value = [
            {
                "job_id": 31,
                "expected_run_time": datetime(2026, 8, 20, 10, 1, 20),
                "delay_seconds": 43.25,
                "job_name": "demo",
                "is_create": True,
            }
        ]
        conn.cursor.return_value = cursor

        @contextmanager
        def fake_open(_purpose):
            yield conn
            conn.close()

        with patch.object(scheduler_database, "open_connection", fake_open):
            rows = scheduler_database.fetch_upcoming_jobs(config)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].job_id, 31)
        self.assertEqual(rows[0].delay_seconds, 43.25)
        conn.close.assert_called()

    def test_execute_scheduled_opens_and_closes(self) -> None:
        config = SchedulerConfig()
        conn = MagicMock()
        conn.closed = False
        txn = MagicMock()
        txn.__enter__.return_value = txn
        txn.__exit__.return_value = False
        conn.transaction.return_value = txn
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        cursor.fetchone.return_value = {
            "status": "EXECUTED",
            "job_id": 31,
            "message": "ok",
            "next_run_time": datetime(2026, 8, 20, 11, 0, 0),
            "is_create": True,
        }
        conn.cursor.return_value = cursor

        @contextmanager
        def fake_open(_purpose):
            yield conn
            conn.close()

        with patch.object(scheduler_database, "open_connection", fake_open):
            result = scheduler_database.execute_scheduled_job(
                config, 31, datetime(2026, 8, 20, 10, 1, 20)
            )

        self.assertEqual(result["status"], "EXECUTED")
        conn.close.assert_called()
        conn.transaction.assert_called()


class SchedulerLoopTests(unittest.TestCase):
    def test_refresh_event_wakes_and_rereads_db(self) -> None:
        config = SchedulerConfig(reconcile_seconds=60, lookahead_seconds=120)
        scheduler = PartitionScheduler(config)
        calls = {"n": 0}

        async def fake_refresh(reason: str) -> bool:
            calls["n"] += 1
            calls["last"] = reason
            return True

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(0.05)
            scheduler.request_refresh()
            await asyncio.sleep(0.1)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())
        self.assertGreaterEqual(calls["n"], 2)
        self.assertIn(calls["last"], {"http-refresh", "post-execution", "periodic-reconcile", "startup"})

    def test_timer_passes_expected_run_time(self) -> None:
        config = SchedulerConfig(reconcile_seconds=30)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 8, 20, 10, 1, 20)
        seen: list[tuple[int, datetime]] = []

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs(
                    [_job(31, when, 0.0)], now_mono=time.monotonic()
                )
                scheduler._update_status_from_queue()
            return True

        async def fake_execute(job_id: int, expected: datetime) -> None:
            seen.append((job_id, expected))

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]
        scheduler._execute_one = fake_execute  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(0.15)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())
        self.assertEqual(seen, [(31, when)])

    def test_db_outage_on_execute_does_not_kill_loop(self) -> None:
        config = SchedulerConfig()
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 8, 20, 10, 0, 0)

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs(
                    [_job(9, when, 0.0)], now_mono=time.monotonic()
                )
            return True

        async def boom(job_id: int, expected: datetime) -> None:
            raise RuntimeError("db down")

        # Use real _execute_one but patch execute_scheduled_job
        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        async def scenario() -> None:
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                side_effect=RuntimeError("db down"),
            ):
                task = asyncio.create_task(scheduler.run())
                await asyncio.sleep(0.2)
                scheduler.request_shutdown()
                await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())
        self.assertEqual(scheduler.status.last_execution_result, "FAILED_CONNECTION")


class SingletonLockTests(unittest.TestCase):
    def test_second_lock_fails(self) -> None:
        path = os.path.join(tempfile.gettempdir(), f"pj-sched-lock-{os.getpid()}.lock")
        a = ProcessSingletonLock(path)
        b = ProcessSingletonLock(path)
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
        finally:
            a.release()
            try:
                os.remove(path)
            except OSError:
                pass


class SchedulerClientTests(unittest.TestCase):
    def test_refresh_success(self) -> None:
        class Resp:
            status = 202

            def read(self) -> bytes:
                return b'{"accepted": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("urllib.request.urlopen", return_value=Resp()):
            ok, message = scheduler_client.notify_scheduler_refresh()
        self.assertTrue(ok)
        self.assertIn("accepted", message.lower())

    def test_refresh_unavailable_does_not_raise(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            ok, message = scheduler_client.notify_scheduler_refresh()
        self.assertFalse(ok)
        self.assertIn("unavailable", message.lower())


class ApiRefreshOrderingTests(unittest.TestCase):
    def test_submit_calls_refresh_after_create(self) -> None:
        with open("api/routers/jobs.py", encoding="utf-8") as handle:
            source = handle.read()
        create_at = source.index("create_partition_job(validated)")
        refresh_at = source.index("notify_scheduler_refresh()")
        self.assertLess(create_at, refresh_at)

    def test_create_partition_job_closes_before_return(self) -> None:
        import database

        source = inspect.getsource(database.create_partition_job)
        self.assertIn("with _connection(", source)
        self.assertIn("Always closes the connection", database.create_partition_job.__doc__)


class SqlMigrationPresenceTests(unittest.TestCase):
    @classmethod
    def _read_sql(cls, name: str) -> str:
        path = os.path.join(os.path.dirname(__file__), "sql", name)
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_realtime_defines_required_functions_without_listen(self) -> None:
        text = self._read_sql("realtime_scheduler_v1.sql")
        self.assertIn("get_upcoming_partition_jobs", text)
        self.assertIn("run_partition_job_scheduled", text)
        self.assertIn("FOR UPDATE", text)
        self.assertIn("pg_try_advisory_xact_lock", text)
        self.assertIn("SKIPPED_RESCHEDULED", text)
        self.assertIn("SKIPPED_DISABLED", text)
        self.assertIn("SKIPPED_NOT_DUE", text)
        self.assertNotIn("pg_notify", text.lower())
        self.assertNotIn("CREATE TRIGGER", text)
        self.assertIn("create_any_table_partition", text)
        self.assertIn("drop_any_table_partition", text)

    def test_exact_create_five_argument_call(self) -> None:
        text = self._read_sql("realtime_scheduler_v1.sql")
        self.assertIn("v_job.partition_period::integer", text)
        self.assertIn("PERFORM mubasher_oms.create_any_table_partition(", text)
        # CREATE call must not pass db_config_para as a 6th worker argument.
        create_section = text.split("PERFORM mubasher_oms.create_any_table_partition(")[1]
        create_args = create_section.split(");")[0]
        self.assertNotIn("db_config", create_args)

    def test_exact_drop_three_argument_call(self) -> None:
        text = self._read_sql("realtime_scheduler_v1.sql")
        self.assertIn("PERFORM mubasher_oms.drop_any_table_partition(", text)
        drop_section = text.split("PERFORM mubasher_oms.drop_any_table_partition(")[1]
        drop_args = drop_section.split(");")[0]
        self.assertIn("v_job.create_drop_interval", drop_args)
        self.assertNotIn("partition_unit", drop_args)
        self.assertNotIn("partition_period", drop_args)

    def test_exact_cron_helper_and_legacy_case(self) -> None:
        text = self._read_sql("realtime_scheduler_v1.sql")
        self.assertIn(
            "v_schedule := mubasher_oms.cron_to_interval_or_next_run(v_job.job_schedule)",
            text,
        )
        self.assertIn("v_schedule.schedule_interval IS NOT NULL", text)
        self.assertIn("v_now + interval '1 day'", text)

    def test_history_insert_omits_job_log_id_and_uses_success_fail(self) -> None:
        text = self._read_sql("realtime_scheduler_v1.sql")
        self.assertIn("INSERT INTO mubasher_oms.partitioning_job_table_log (", text)
        insert_block = text.split("INSERT INTO mubasher_oms.partitioning_job_table_log (")[1]
        insert_block = insert_block.split(") VALUES (")[0]
        self.assertNotIn("job_log_id", insert_block)
        self.assertIn("'SUCCESS'", text)
        self.assertIn("'FAIL'", text)

    def test_failure_advances_next_run_and_cron_fallback(self) -> None:
        text = self._read_sql("realtime_scheduler_v1.sql")
        self.assertIn("fallback next_run_time = now() + 1 day", text)
        self.assertIn(
            "next_run_time   = COALESCE(v_new_next_run, v_now + interval '1 day')",
            text,
        )

    def test_null_is_create_fails_safely(self) -> None:
        text = self._read_sql("realtime_scheduler_v1.sql")
        self.assertIn("IF v_job.is_create IS NULL THEN", text)
        self.assertIn("refusing to decide CREATE vs DROP", text)

    def test_preflight_is_read_only(self) -> None:
        text = self._read_sql("preflight_realtime_database.sql")
        # Strip SQL comments so documentary mentions of DDL verbs do not fail the check.
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            lines.append(line.split("--", 1)[0])
        active = "\n".join(lines)
        for forbidden in (
            "CREATE TABLE",
            "CREATE OR REPLACE",
            "DROP ",
            "TRUNCATE",
            "DELETE FROM",
            "UPDATE ",
            "INSERT INTO",
            "ALTER TABLE",
            "GRANT ",
            "REVOKE ",
        ):
            self.assertNotIn(forbidden, active)

    def test_bootstrap_does_not_drop_application_schema(self) -> None:
        text = self._read_sql("bootstrap_partition_job_framework.sql")
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            lines.append(line.split("--", 1)[0])
        active = "\n".join(lines)
        self.assertNotIn("DROP SCHEMA", active)
        self.assertNotIn("DROP OWNED", active)
        self.assertNotIn("TRUNCATE", active)
        self.assertIn("frequency", active)
        self.assertNotIn("job_frequency", active)
        self.assertIn("must import from REFERENCE", text)
        self.assertNotIn(
            "CREATE OR REPLACE FUNCTION mubasher_oms.create_any_table_partition",
            active,
        )
        self.assertNotIn(
            "CREATE OR REPLACE FUNCTION mubasher_oms.drop_any_table_partition",
            active,
        )
        self.assertNotIn(
            "CREATE OR REPLACE FUNCTION mubasher_oms.cron_to_interval_or_next_run",
            active,
        )

    def test_disposable_test_targets_only_synthetic_schema(self) -> None:
        text = self._read_sql("integration_test_disposable.sql")
        self.assertIn("partition_scheduler_test", text)
        self.assertIn("synth_events", text)
        self.assertNotIn("DROP SCHEMA mubasher_oms", text)
        self.assertIn("_partition_scheduler_destructive_test_approved", text)


class DbSafetyTests(unittest.TestCase):
    def test_expected_name_required_and_mismatch_fails(self) -> None:
        from scheduler_backend.db_safety import (
            DatabaseSafetyError,
            assert_expected_database,
            assert_migration_gate,
        )

        with patch.dict(os.environ, {"REALTIME_DB_EXPECTED_NAME": ""}, clear=False):
            with self.assertRaises(DatabaseSafetyError):
                assert_expected_database("anything")

        with patch.dict(
            os.environ, {"REALTIME_DB_EXPECTED_NAME": "new_db_copy"}, clear=False
        ):
            assert_expected_database("new_db_copy")
            with self.assertRaises(DatabaseSafetyError):
                assert_expected_database("wrong_db")

        with patch.dict(
            os.environ, {"PARTITION_REALTIME_ALLOW_MIGRATION": ""}, clear=False
        ):
            with self.assertRaises(DatabaseSafetyError):
                assert_migration_gate()

        with patch.dict(
            os.environ,
            {
                "PARTITION_REALTIME_ALLOW_MIGRATION": "true",
                "REALTIME_DB_EXPECTED_NAME": "new_db_copy",
            },
            clear=False,
        ):
            assert_migration_gate(actual_database="new_db_copy")


class BackendDoesNotCallLegacyRunnersTests(unittest.TestCase):
    def test_scheduler_modules_never_call_legacy_or_manual_runners(self) -> None:
        root = os.path.dirname(__file__)
        forbidden = (
            "run_partition_create_jobs",
            "run_partition_drop_jobs",
            "run_partition_job_manual",
        )
        for name in (
            "scheduler_backend/scheduler.py",
            "scheduler_backend/scheduler_database.py",
            "scheduler_backend/main.py",
            "scheduler_backend/queue_manager.py",
        ):
            with open(
                os.path.join(root, name.replace("/", os.sep)), encoding="utf-8"
            ) as handle:
                source = handle.read()
            for token in forbidden:
                self.assertNotIn(token, source, msg=f"{name} references {token}")
        with open(
            os.path.join(root, "scheduler_backend", "scheduler_database.py"),
            encoding="utf-8",
        ) as handle:
            source = handle.read()
        self.assertIn("run_partition_job_scheduled", source)
        self.assertIn("get_upcoming_partition_jobs", source)
        self.assertIn("assert_expected_database", source)


class StatusModelTests(unittest.TestCase):
    def test_status_as_dict_needs_no_db(self) -> None:
        status = SchedulerStatus(scheduler_active=True, upcoming_job_count=2)
        data = status.as_dict()
        self.assertTrue(data["scheduler_active"])
        self.assertEqual(data["upcoming_job_count"], 2)


if __name__ == "__main__":
    unittest.main()
