"""Phase 3 — Datadog / DogStatsD observability tests."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import observability
from observability import (
    datadog_enabled,
    emit_api_request,
    emit_db_connection_failure,
    emit_job_execution,
    emit_jobs_failed_count,
    emit_scheduler_concurrency,
    emit_scheduler_heartbeat,
    emit_secrets_refresh,
    emit_secrets_retrieval_failure,
    format_counter_for_tests,
    format_gauge_for_tests,
    format_timing_for_tests,
    normalize_job_status,
    normalize_operation,
    observability_status,
)
from scheduler_backend import scheduler_database
from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.scheduler import PartitionScheduler


class DatadogConfigTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PARTOPS_DATADOG_ENABLED", None)
            self.assertFalse(datadog_enabled())

    def test_disabled_mode_sends_no_udp(self) -> None:
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "false"}, clear=False):
            with patch("observability.socket.socket") as sock_cls:
                emit_scheduler_heartbeat()
                emit_job_execution(status="EXECUTED", is_create=True)
                sock_cls.assert_not_called()


class DogStatsDFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "PARTOPS_DATADOG_ENABLED": "true",
                "PARTOPS_ENVIRONMENT": "development",
                "PARTOPS_METRICS_NAMESPACE": "partops",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def test_counter_format(self) -> None:
        text = format_counter_for_tests("job.execution", 1, {"status": "executed"})
        self.assertTrue(text.startswith("partops.job.execution:1|c|#"))
        self.assertIn("status:executed", text)
        self.assertIn("env:development", text)

    def test_gauge_format(self) -> None:
        text = format_gauge_for_tests("scheduler.active_jobs", 2)
        self.assertTrue(text.startswith("partops.scheduler.active_jobs:2|g|#"))

    def test_timing_format(self) -> None:
        text = format_timing_for_tests("job.duration_ms", 12.5, {"status": "executed"})
        self.assertTrue(text.startswith("partops.job.duration_ms:12.5|ms|#"))

    def test_multiple_safe_tags(self) -> None:
        text = format_counter_for_tests(
            "job.execution",
            1,
            {"status": "executed", "operation": "create"},
        )
        self.assertIn("status:executed", text)
        self.assertIn("operation:create", text)

    def test_forbidden_tags_not_added(self) -> None:
        text = format_counter_for_tests(
            "job.execution",
            1,
            {
                "status": "executed",
                "job_id": "33",
                "job_name": "orders_drop",
                "table_name": "orders",
                "error": "boom",
                "secret_id": "arn:aws:secretsmanager:x",
            },
        )
        self.assertNotIn("job_id", text)
        self.assertNotIn("job_name", text)
        self.assertNotIn("table_name", text)
        self.assertNotIn("error:", text)
        self.assertNotIn("secret_id", text)
        self.assertIn("status:executed", text)

    def test_send_exception_never_propagates(self) -> None:
        with patch(
            "observability.socket.socket",
            side_effect=OSError("agent down"),
        ):
            emit_scheduler_heartbeat()
            emit_job_execution(status="FAILED")
            emit_db_connection_failure(component="api")


class StatusNormalizationTests(unittest.TestCase):
    def test_status_normalized(self) -> None:
        self.assertEqual(normalize_job_status("EXECUTED"), "executed")
        self.assertEqual(normalize_job_status("SKIPPED_TARGET_LOCKED"), "skipped_target_locked")
        self.assertEqual(normalize_job_status("FAILED_DATABASE"), "failed_database")
        self.assertEqual(normalize_job_status("weird"), "other")

    def test_operation_tags(self) -> None:
        self.assertEqual(normalize_operation(True), "create")
        self.assertEqual(normalize_operation(False), "drop")
        self.assertIsNone(normalize_operation(None))


class SchedulerMetricsIntegrationTests(unittest.TestCase):
    def test_heartbeat_and_concurrency_on_refresh(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=2, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 15, 0, 0)

        def fake_fetch(cfg):
            return [
                ScheduledJob(1, when, -5.0, "a", True),
                ScheduledJob(2, when, 10.0, "b", False),
            ]

        sent = []

        def capture_send(payload: bytes) -> None:
            sent.append(payload.decode("utf-8"))

        with patch.dict(
            os.environ,
            {
                "PARTOPS_DATADOG_ENABLED": "true",
                "PARTOPS_ENVIRONMENT": "development",
            },
            clear=False,
        ), patch(
            "scheduler_backend.scheduler.fetch_upcoming_jobs",
            side_effect=fake_fetch,
        ), patch(
            "scheduler_backend.scheduler.count_failed_jobs",
            return_value=0,
        ), patch("observability._send", side_effect=capture_send):
            ok = asyncio.run(scheduler.refresh_queue("startup"))

        self.assertTrue(ok)
        joined = "\n".join(sent)
        self.assertIn("partops.scheduler.heartbeat:1|g", joined)
        self.assertIn("partops.scheduler.active_jobs:", joined)
        self.assertIn("partops.scheduler.max_concurrent_jobs:2|g", joined)
        self.assertIn("partops.scheduler.queue_depth:", joined)
        self.assertIn("partops.scheduler.available_slots:", joined)
        self.assertIn("partops.scheduler.overdue_jobs:1|g", joined)
        self.assertIn("partops.jobs.failed_count:0|g", joined)

    def test_job_execution_counter_and_no_high_cardinality_tags(self) -> None:
        config = SchedulerConfig(max_concurrent_jobs=1, reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 15, 5, 0)
        sent = []

        async def fake_refresh(reason: str) -> bool:
            if reason == "startup":
                scheduler.queue.replace_from_jobs(
                    [
                        ScheduledJob(
                            33, when, 0.0, "DROP_ORDERS", False
                        )
                    ]
                )
                scheduler._update_status_from_queue()
            return True

        scheduler.refresh_queue = fake_refresh  # type: ignore[method-assign]

        with patch.dict(
            os.environ,
            {"PARTOPS_DATADOG_ENABLED": "true", "PARTOPS_ENVIRONMENT": "development"},
            clear=False,
        ), patch(
            "scheduler_backend.scheduler.execute_scheduled_job",
            return_value={
                "status": "EXECUTED",
                "job_id": 33,
                "message": "ok",
                "next_run_time": when,
                "is_create": False,
                "execution_duration_ms": 42,
            },
        ), patch(
            "observability._send", side_effect=lambda p: sent.append(p.decode("utf-8"))
        ):

            async def scenario() -> None:
                task = asyncio.create_task(scheduler.run())
                await asyncio.sleep(0.25)
                scheduler.request_shutdown()
                await asyncio.wait_for(task, timeout=3)

            asyncio.run(scenario())

        joined = "\n".join(sent)
        self.assertIn("partops.job.execution:1|c", joined)
        self.assertIn("status:executed", joined)
        self.assertIn("operation:drop", joined)
        self.assertIn("partops.job.duration_ms:42|ms", joined)
        self.assertNotIn("job_id:", joined)
        self.assertNotIn("job_name:", joined)
        self.assertNotIn("table_name:", joined)
        self.assertNotIn("DROP_ORDERS", joined)

    def test_duration_not_emitted_without_db_duration(self) -> None:
        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False):
            with patch(
                "observability._send",
                side_effect=lambda p: sent.append(p.decode("utf-8")),
            ):
                emit_job_execution(status="EXECUTED", is_create=True, duration_ms=None)
        joined = "\n".join(sent)
        self.assertIn("partops.job.execution", joined)
        self.assertNotIn("duration_ms", joined)

    def test_monitoring_failure_does_not_change_job_result(self) -> None:
        config = SchedulerConfig()
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 15, 10, 0)

        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False):
            with patch(
                "scheduler_backend.scheduler.execute_scheduled_job",
                return_value={
                    "status": "SKIPPED_TARGET_LOCKED",
                    "job_id": 9,
                    "message": "locked",
                    "next_run_time": when,
                    "is_create": True,
                },
            ), patch(
                "observability._send", side_effect=RuntimeError("metrics boom")
            ):
                asyncio.run(scheduler._execute_one(9, when))
        self.assertEqual(scheduler.status.last_execution_result, "SKIPPED_TARGET_LOCKED")


class FailedJobsGaugeTests(unittest.TestCase):
    def test_gauge_format_not_counter(self) -> None:
        with patch.dict(
            os.environ,
            {"PARTOPS_DATADOG_ENABLED": "true", "PARTOPS_ENVIRONMENT": "development"},
            clear=False,
        ):
            text = format_gauge_for_tests("jobs.failed_count", 2)
        self.assertEqual(
            text.split("|#")[0], "partops.jobs.failed_count:2|g"
        )
        self.assertNotIn("|c", text.split("|#")[0])

    def test_count_failed_jobs_sql_filters(self) -> None:
        source = inspect.getsource(scheduler_database.count_failed_jobs)
        self.assertIn("is_enabled IS TRUE", source)
        self.assertIn("last_run_status = 'FAIL'", source)
        self.assertIn("COUNT(*)", source)
        self.assertNotIn("partitioning_job_table_log", source)
        self.assertIn("gauge", inspect.getsource(emit_jobs_failed_count))

    def _refresh_with_failed_count(self, failed_count: int) -> list[str]:
        config = SchedulerConfig(reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 16, 0, 0)
        sent: list[str] = []

        def fake_fetch(cfg):
            return [ScheduledJob(1, when, 0.0, "a", True)]

        with patch.dict(
            os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False
        ), patch(
            "scheduler_backend.scheduler.fetch_upcoming_jobs",
            side_effect=fake_fetch,
        ), patch(
            "scheduler_backend.scheduler.count_failed_jobs",
            return_value=failed_count,
        ), patch(
            "observability._send",
            side_effect=lambda p: sent.append(p.decode("utf-8")),
        ):
            ok = asyncio.run(scheduler.refresh_queue("startup"))
        self.assertTrue(ok)
        return sent

    def test_zero_enabled_failed_jobs(self) -> None:
        sent = self._refresh_with_failed_count(0)
        self.assertTrue(any("partops.jobs.failed_count:0|g" in s for s in sent))

    def test_one_enabled_failed_job(self) -> None:
        sent = self._refresh_with_failed_count(1)
        self.assertTrue(any("partops.jobs.failed_count:1|g" in s for s in sent))

    def test_two_enabled_failed_jobs(self) -> None:
        sent = self._refresh_with_failed_count(2)
        self.assertTrue(any("partops.jobs.failed_count:2|g" in s for s in sent))

    def test_count_helper_excludes_disabled_success_null(self) -> None:
        """SQL contract: only enabled FAIL rows; not disabled/SUCCESS/NULL."""
        source = inspect.getsource(scheduler_database.count_failed_jobs)
        sql_start = source.index('sql = (')
        sql_block = source[sql_start:]
        self.assertIn("j.is_enabled IS TRUE", sql_block)
        self.assertIn("j.last_run_status = 'FAIL'", sql_block)
        # Equality to FAIL excludes NULL and SUCCESS without needing OR clauses.
        self.assertNotIn("SUCCESS", sql_block)
        self.assertNotIn("is_enabled IS FALSE", sql_block)
        self.assertNotIn("IS NULL", sql_block)

    def test_count_query_failure_does_not_crash_or_emit_zero(self) -> None:
        config = SchedulerConfig(reconcile_seconds=60)
        scheduler = PartitionScheduler(config)
        when = datetime(2026, 10, 13, 16, 5, 0)
        sent: list[str] = []

        def fake_fetch(cfg):
            return [ScheduledJob(1, when, 0.0, "a", True)]

        with patch.dict(
            os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False
        ), patch(
            "scheduler_backend.scheduler.fetch_upcoming_jobs",
            side_effect=fake_fetch,
        ), patch(
            "scheduler_backend.scheduler.count_failed_jobs",
            side_effect=RuntimeError("count unavailable"),
        ), patch(
            "observability._send",
            side_effect=lambda p: sent.append(p.decode("utf-8")),
        ):
            ok = asyncio.run(scheduler.refresh_queue("startup"))

        self.assertTrue(ok)
        self.assertNotIn("failed_jobs_count", scheduler.status.extra)
        self.assertFalse(any("jobs.failed_count" in s for s in sent))
        # Refresh still emitted heartbeat (scheduling path intact).
        self.assertTrue(any("scheduler.heartbeat" in s for s in sent))

    def test_job_execution_metric_unchanged(self) -> None:
        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False):
            with patch(
                "observability._send",
                side_effect=lambda p: sent.append(p.decode("utf-8")),
            ):
                emit_job_execution(status="FAILED", is_create=False)
        joined = "\n".join(sent)
        self.assertIn("partops.job.execution:1|c", joined)
        self.assertIn("status:failed", joined)
        self.assertNotIn("jobs.failed_count", joined)


class DbAndSecretsMetricsTests(unittest.TestCase):
    def test_db_connection_failure_metric(self) -> None:
        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False):
            with patch(
                "observability._send",
                side_effect=lambda p: sent.append(p.decode("utf-8")),
            ):
                emit_db_connection_failure(component="scheduler")
        self.assertTrue(any("partops.db.connection_failure" in s for s in sent))
        self.assertTrue(any("component:scheduler" in s for s in sent))

    def test_secrets_failure_and_refresh(self) -> None:
        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False):
            with patch(
                "observability._send",
                side_effect=lambda p: sent.append(p.decode("utf-8")),
            ):
                emit_secrets_retrieval_failure(reason="AccessDeniedException")
                emit_secrets_refresh()
        joined = "\n".join(sent)
        self.assertIn("partops.secrets_manager.retrieval_failure", joined)
        self.assertIn("reason:access_denied", joined)
        self.assertIn("partops.secrets_manager.refresh", joined)

    def test_cached_secret_read_does_not_increment_refresh(self) -> None:
        from db_credentials import (
            _reset_for_tests,
            get_database_secret,
            resolve_database_credentials,
        )

        _reset_for_tests()
        client = MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": '{"username":"u","password":"p"}'
        }
        sent = []
        with patch.dict(
            os.environ,
            {
                "PARTITION_DB_SECRET_PROVIDER": "aws",
                "PARTITION_DB_SECRET_ID": "partops/test",
                "PARTOPS_DATADOG_ENABLED": "true",
            },
            clear=False,
        ), patch("db_credentials._secrets_manager_client", return_value=client), patch(
            "observability._send",
            side_effect=lambda p: sent.append(p.decode("utf-8")),
        ):
            resolve_database_credentials()
            resolve_database_credentials()
        refresh_lines = [s for s in sent if "secrets_manager.refresh" in s]
        self.assertEqual(len(refresh_lines), 1)
        self.assertEqual(client.get_secret_value.call_count, 1)
        _reset_for_tests()

    def test_monitoring_failure_does_not_change_credential_failure(self) -> None:
        from db_credentials import DatabaseCredentialError, _reset_for_tests, get_database_secret

        _reset_for_tests()

        class Boom(Exception):
            response = {"Error": {"Code": "AccessDeniedException"}}

        client = MagicMock()
        client.get_secret_value.side_effect = Boom("denied")
        with patch.dict(
            os.environ,
            {
                "PARTITION_DB_SECRET_PROVIDER": "aws",
                "PARTITION_DB_SECRET_ID": "partops/test",
                "PARTOPS_DATADOG_ENABLED": "true",
            },
            clear=False,
        ), patch("db_credentials._secrets_manager_client", return_value=client), patch(
            "observability._send", side_effect=RuntimeError("metrics boom")
        ):
            with self.assertRaises(DatabaseCredentialError) as ctx:
                get_database_secret()
        self.assertIn("AccessDenied", str(ctx.exception))
        _reset_for_tests()

    def test_monitoring_failure_does_not_change_db_error_behaviour(self) -> None:
        import database

        def boom_factory():
            raise OSError("down")

        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False):
            with patch("observability._send", side_effect=RuntimeError("metrics boom")):
                with self.assertRaises(database.DatabaseError):
                    with database._connection(boom_factory):
                        pass


class ApiMetricsTests(unittest.TestCase):
    def test_api_request_low_cardinality(self) -> None:
        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true"}, clear=False):
            with patch(
                "observability._send",
                side_effect=lambda p: sent.append(p.decode("utf-8")),
            ):
                emit_api_request(method="GET", status_code=200)
                emit_api_request(method="POST", status_code=500)
        joined = "\n".join(sent)
        self.assertIn("partops.api.request", joined)
        self.assertIn("method:GET", joined)
        self.assertIn("status_class:2xx", joined)
        self.assertIn("partops.api.error", joined)
        self.assertNotIn("/api/jobs/", joined)


class ReadinessAndSafetyTests(unittest.TestCase):
    def test_observability_status_safe(self) -> None:
        status = observability_status()
        self.assertIn("datadog_enabled", status)
        self.assertFalse(status["dogstatsd_required_for_readiness"])
        self.assertNotIn("api_key", str(status).lower())
        self.assertNotIn("DD_API_KEY", str(status))

    def test_no_datadog_api_key_or_sdk_dependency(self) -> None:
        root = os.path.dirname(__file__)
        source = open(os.path.join(root, "observability.py"), encoding="utf-8").read()
        # Must not read/configure an application Datadog API key env var.
        self.assertNotRegex(source, r'(?m)^\s*os\.environ\.get\(\s*["\']DD_')
        self.assertNotIn("datadog-api-client", source)
        self.assertNotIn("import datadog", source)
        self.assertNotIn("from datadog", source)
        req = open(os.path.join(root, "requirements.txt"), encoding="utf-8").read()
        self.assertNotIn("datadog", req.lower())

    def test_python39_compatible(self) -> None:
        path = os.path.join(os.path.dirname(__file__), "observability.py")
        source = open(path, encoding="utf-8").read()
        compile(source, path, "exec")
        self.assertNotRegex(source, r"(?m)^\s*match\s+")
        self.assertGreaterEqual(sys.version_info[:2], (3, 9))

    def test_env_examples_document_datadog_defaults(self) -> None:
        root = os.path.dirname(__file__)
        for name in (".env.example", ".env.realtime.example"):
            text = open(os.path.join(root, name), encoding="utf-8").read()
            self.assertIn("PARTOPS_DATADOG_ENABLED=false", text)
            self.assertIn("PARTOPS_DOGSTATSD_HOST=127.0.0.1", text)
            self.assertIn("PARTOPS_DOGSTATSD_PORT=8125", text)
            # No assignment of a Datadog API key in PartOps env templates.
            self.assertNotRegex(text, r"(?m)^\s*DD_API_KEY\s*=")


if __name__ == "__main__":
    unittest.main()
