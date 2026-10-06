"""Tests for Edit Configured Job + Execution Duration features."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import unittest

from fastapi.testclient import TestClient

from api.main import app
from api.routers import jobs as jobs_router
from dashboard_metrics import average_execution_duration_ms, format_duration_ms
from validators import ValidationError, validate_form_data


VALID_UPDATE = {
    "job_name": "JOB_EDIT_TEST",
    "is_enabled": True,
    "table_schema": "mubasher_oms",
    "table_name": "r19_customer_summary",
    "db_config": "{}",
    "job_schedule": "0 0 2 * * *",
    "frequency_amount": 1,
    "frequency_unit": "day",
    "next_run_time": "2026-09-30T02:00:00",
    "partition_unit": "day",
    "partition_period": 1,
    "is_create": True,
    "create_drop_amount": 2,
    "create_drop_unit": "month",
}


class FormatDurationTests(unittest.TestCase):
    def test_null_shows_em_dash(self) -> None:
        self.assertEqual(format_duration_ms(None), "—")

    def test_milliseconds(self) -> None:
        self.assertEqual(format_duration_ms(342), "342 ms")

    def test_seconds(self) -> None:
        self.assertEqual(format_duration_ms(1820), "1.82 s")

    def test_longer_seconds(self) -> None:
        self.assertEqual(format_duration_ms(48300), "48.3 s")

    def test_minutes(self) -> None:
        self.assertEqual(format_duration_ms(74000), "1m 14s")

    def test_minutes_padded(self) -> None:
        self.assertEqual(format_duration_ms(123000), "2m 03s")


class AverageDurationTests(unittest.TestCase):
    def test_ignores_null(self) -> None:
        logs = [
            {"execution_duration_ms": None},
            {"execution_duration_ms": 1000},
            {"execution_duration_ms": 3000},
            {"execution_duration_ms": None},
        ]
        result = average_execution_duration_ms(logs)
        self.assertTrue(result["available"])
        self.assertEqual(result["average_ms"], 2000)
        self.assertEqual(result["sample_count"], 2)

    def test_no_data(self) -> None:
        result = average_execution_duration_ms(
            [{"execution_duration_ms": None}, {"job_runtime": "2026-01-01"}]
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["detail"], "No runtime data yet")
        self.assertEqual(result["label"], "—")


class PrepareJobUpdateTests(unittest.TestCase):
    def _existing(self, **overrides):
        base = {
            "job_id": 34,
            "job_name": "JOB_EDIT_TEST",
            "is_enabled": True,
            "table_schema": "mubasher_oms",
            "table_name": "r19_customer_summary",
            "db_config_para": {},
            "job_schedule": "0 0 2 * * *",
            "frequency": "1 day",
            "next_run_time": datetime(2026, 9, 30, 2, 0, 0),
            "partition_unit": "day",
            "partition_period": 1,
            "is_create": True,
            "create_drop_interval": "2 months",
            "last_run_time": datetime(2026, 9, 29, 2, 0, 0),
            "last_run_status": "SUCCESS",
        }
        base.update(overrides)
        return base

    def test_update_job_name(self) -> None:
        body = jobs_router.JobUpdateBody(**{**VALID_UPDATE, "job_name": "JOB_RENAMED"})
        validated = jobs_router._prepare_job_update(self._existing(), body)
        self.assertEqual(validated["job_name"], "JOB_RENAMED")
        self.assertNotIn("last_run_time", validated)
        self.assertNotIn("last_run_status", validated)
        self.assertNotIn("job_id", validated)

    def test_disable_enabled_job(self) -> None:
        body = jobs_router.JobUpdateBody(**{**VALID_UPDATE, "is_enabled": False})
        validated = jobs_router._prepare_job_update(self._existing(), body)
        self.assertFalse(validated["is_enabled"])

    def test_enable_disabled_recalculates_past_next_run(self) -> None:
        past = datetime.now() - timedelta(hours=2)
        existing = self._existing(is_enabled=False, next_run_time=past)
        body = jobs_router.JobUpdateBody(
            **{
                **VALID_UPDATE,
                "is_enabled": True,
                "next_run_time": past.isoformat(sep="T"),
            }
        )
        validated = jobs_router._prepare_job_update(existing, body)
        self.assertTrue(validated["is_enabled"])
        self.assertGreater(validated["next_run_time"], datetime.now() - timedelta(seconds=5))

    def test_schedule_change_recalculates_next_run(self) -> None:
        body = jobs_router.JobUpdateBody(
            **{**VALID_UPDATE, "job_schedule": "0 30 3 * * *"}
        )
        validated = jobs_router._prepare_job_update(self._existing(), body)
        self.assertEqual(validated["job_schedule"], "0 30 3 * * *")
        self.assertEqual(validated["next_run_time"].hour, 3)
        self.assertEqual(validated["next_run_time"].minute, 30)

    def test_invalid_cron_rejected(self) -> None:
        body = jobs_router.JobUpdateBody(
            **{**VALID_UPDATE, "job_schedule": "not a cron"}
        )
        with self.assertRaises(ValidationError):
            jobs_router._prepare_job_update(self._existing(), body)

    def test_invalid_db_config_rejected(self) -> None:
        body = jobs_router.JobUpdateBody(**{**VALID_UPDATE, "db_config": "[1,2]"})
        with self.assertRaises(ValidationError):
            jobs_router._prepare_job_update(self._existing(), body)

    def test_create_to_drop_requires_confirmation(self) -> None:
        body = jobs_router.JobUpdateBody(
            **{**VALID_UPDATE, "is_create": False, "confirm_dangerous": False}
        )
        with self.assertRaises(ValidationError) as ctx:
            jobs_router._prepare_job_update(self._existing(), body)
        self.assertIn("confirm_dangerous", ctx.exception.message)

    def test_create_to_drop_with_confirmation(self) -> None:
        body = jobs_router.JobUpdateBody(
            **{**VALID_UPDATE, "is_create": False, "confirm_dangerous": True}
        )
        validated = jobs_router._prepare_job_update(self._existing(), body)
        self.assertFalse(validated["is_create"])

    def test_target_change_requires_confirmation(self) -> None:
        body = jobs_router.JobUpdateBody(
            **{
                **VALID_UPDATE,
                "table_name": "other_table",
                "confirm_dangerous": False,
            }
        )
        with self.assertRaises(ValidationError):
            jobs_router._prepare_job_update(self._existing(), body)


class PatchJobsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_nonexistent_job_404(self) -> None:
        with patch("api.routers.jobs.get_partition_job", return_value=None):
            res = self.client.patch("/api/jobs/99999", json=VALID_UPDATE)
        self.assertEqual(res.status_code, 404)

    def test_scheduler_refresh_called_after_successful_edit(self) -> None:
        existing = {
            "job_id": 34,
            "job_name": "JOB_EDIT_TEST",
            "is_enabled": True,
            "table_schema": "mubasher_oms",
            "table_name": "r19_customer_summary",
            "is_create": True,
            "job_schedule": "0 0 2 * * *",
            "next_run_time": datetime(2026, 9, 30, 2, 0, 0),
            "last_run_time": datetime(2026, 9, 29, 2, 0, 0),
            "last_run_status": "SUCCESS",
        }
        with patch("api.routers.jobs.get_partition_job", return_value=existing), patch(
            "api.routers.jobs.update_partition_job", return_value=None
        ) as update_mock, patch(
            "api.routers.jobs.notify_scheduler_refresh",
            return_value=(True, "ok"),
        ) as refresh_mock:
            res = self.client.patch(
                "/api/jobs/34",
                json={**VALID_UPDATE, "job_name": "JOB_RENAMED"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["message"], "Job updated successfully.")
        update_mock.assert_called_once()
        refresh_mock.assert_called_once()
        # Protected fields must not be passed to the update function.
        sent = update_mock.call_args[0][1]
        self.assertNotIn("last_run_time", sent)
        self.assertNotIn("last_run_status", sent)
        self.assertNotIn("job_id", sent)

    def test_scheduler_refresh_failure_does_not_undo_db_update(self) -> None:
        existing = {
            "job_id": 34,
            "job_name": "JOB_EDIT_TEST",
            "is_enabled": True,
            "table_schema": "mubasher_oms",
            "table_name": "r19_customer_summary",
            "is_create": True,
            "job_schedule": "0 0 2 * * *",
            "next_run_time": datetime(2026, 9, 30, 2, 0, 0),
        }
        with patch("api.routers.jobs.get_partition_job", return_value=existing), patch(
            "api.routers.jobs.update_partition_job", return_value=None
        ) as update_mock, patch(
            "api.routers.jobs.notify_scheduler_refresh",
            return_value=(False, "scheduler unreachable"),
        ):
            res = self.client.patch(
                "/api/jobs/34",
                json={**VALID_UPDATE, "job_name": "JOB_RENAMED"},
            )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["refresh_ok"])
        update_mock.assert_called_once()

    def test_disable_via_api(self) -> None:
        existing = {
            "job_id": 34,
            "job_name": "JOB_EDIT_TEST",
            "is_enabled": True,
            "table_schema": "mubasher_oms",
            "table_name": "r19_customer_summary",
            "is_create": True,
            "job_schedule": "0 0 2 * * *",
            "next_run_time": datetime(2026, 9, 30, 2, 0, 0),
        }
        with patch("api.routers.jobs.get_partition_job", return_value=existing), patch(
            "api.routers.jobs.update_partition_job", return_value=None
        ) as update_mock, patch(
            "api.routers.jobs.notify_scheduler_refresh",
            return_value=(True, "ok"),
        ):
            res = self.client.patch(
                "/api/jobs/34",
                json={**VALID_UPDATE, "is_enabled": False},
            )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(update_mock.call_args[0][1]["is_enabled"])

    def test_job_id_cannot_be_changed_via_body(self) -> None:
        """Extra job_id in JSON is ignored by JobUpdateBody; path id is authoritative."""
        existing = {
            "job_id": 34,
            "job_name": "JOB_EDIT_TEST",
            "is_enabled": True,
            "table_schema": "mubasher_oms",
            "table_name": "r19_customer_summary",
            "is_create": True,
            "job_schedule": "0 0 2 * * *",
            "next_run_time": datetime(2026, 9, 30, 2, 0, 0),
        }
        with patch("api.routers.jobs.get_partition_job", return_value=existing), patch(
            "api.routers.jobs.update_partition_job", return_value=None
        ) as update_mock, patch(
            "api.routers.jobs.notify_scheduler_refresh",
            return_value=(True, "ok"),
        ):
            res = self.client.patch(
                "/api/jobs/34",
                json={**VALID_UPDATE, "job_id": 999},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(update_mock.call_args[0][0], 34)
        self.assertNotIn("job_id", update_mock.call_args[0][1])


class DurationApiAndManualPathTests(unittest.TestCase):
    def test_history_api_includes_duration_field(self) -> None:
        logs = [
            {
                "job_log_id": 1,
                "job_id": 34,
                "job_name": "JOB_A",
                "last_run_status": "SUCCESS",
                "job_runtime": datetime(2026, 9, 29, 9, 42, 10),
                "job_error": None,
                "execution_duration_ms": 1820,
            },
            {
                "job_log_id": 2,
                "job_id": 33,
                "job_name": "JOB_B",
                "last_run_status": "FAIL",
                "job_runtime": datetime(2026, 9, 29, 9, 30, 0),
                "job_error": "boom",
                "execution_duration_ms": None,
            },
        ]
        client = TestClient(app)
        with patch("api.routers.logs.get_partition_job_logs", return_value=logs):
            res = client.get("/api/logs?limit=10")
        self.assertEqual(res.status_code, 200)
        body = res.json()["logs"]
        self.assertEqual(body[0]["execution_duration_ms"], 1820)
        self.assertIsNone(body[1]["execution_duration_ms"])

    def test_job_details_include_last_and_avg_duration(self) -> None:
        job = {
            "job_id": 34,
            "job_name": "JOB_A",
            "is_enabled": True,
            "last_execution_duration_ms": 1820,
            "avg_execution_duration_ms": 2140,
        }
        client = TestClient(app)
        with patch("api.routers.jobs.get_partition_job", return_value=job):
            res = client.get("/api/jobs/34")
        self.assertEqual(res.status_code, 200)
        payload = res.json()["job"]
        self.assertEqual(payload["last_execution_duration_ms"], 1820)
        self.assertEqual(payload["avg_execution_duration_ms"], 2140)

    def test_manual_run_calls_db_function_once_without_stamp(self) -> None:
        import database

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.description = [("result",)]
        mock_cur.fetchone.return_value = (
            "MANUAL_SUCCESS",
            "CREATE partition operation completed.",
            12,
        )
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
        mock_conn.transaction.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(database, "_connection", return_value=mock_conn), patch.object(
            database, "_main_db_kwargs", return_value={}
        ), patch.object(
            database,
            "build_run_partition_job_manual_sql",
            return_value="SELECT mubasher_oms.run_partition_job_manual(%(job_id)s)",
        ):
            result = database.run_partition_job_manual(34)

        self.assertEqual(result["status"], "MANUAL_SUCCESS")
        self.assertEqual(result["execution_duration_ms"], 12)
        self.assertEqual(mock_cur.execute.call_count, 1)
        sql, params = mock_cur.execute.call_args[0]
        self.assertIn("run_partition_job_manual", sql)
        self.assertEqual(params, {"job_id": 34})
        self.assertNotIn("duration_ms", params)
        self.assertFalse(hasattr(database, "build_stamp_log_duration_sql"))
        self.assertFalse(hasattr(database, "stamp_duration_function_identity"))

    def test_python_raises_failure_after_connection_block(self) -> None:
        import inspect
        import database

        source = inspect.getsource(database.run_partition_job_manual)
        conn_at = source.find("with _connection(")
        raise_at = source.rfind("raise DatabaseError(failure_message)")
        self.assertGreater(conn_at, 0)
        self.assertGreater(raise_at, conn_at)
        after_conn = source[source.find("with _connection(") :]
        # The application-level failure raise must not sit inside the transaction.
        self.assertIn("if failure_message:", source[source.rfind("with _connection(") :])
        nested = after_conn.split("if failure_message:", 1)
        self.assertEqual(len(nested), 2)
        self.assertNotIn("with conn.transaction()", nested[1])

    def test_run_now_api_still_accepts_manual_run(self) -> None:
        client = TestClient(app)
        job = {"job_id": 34, "job_name": "JOB_A", "is_create": True}
        with patch(
            "api.routers.jobs.get_database_readiness",
            return_value={"config_table_select": True},
        ), patch("api.routers.jobs.get_partition_job", return_value=job), patch(
            "api.routers.jobs.run_partition_job_manual", return_value=None
        ) as run_mock:
            res = client.post("/api/jobs/34/run")
        self.assertEqual(res.status_code, 200)
        run_mock.assert_called_once_with(34)
        self.assertIn("completed", res.json()["message"])

    def test_run_now_api_surfaces_manual_failure(self) -> None:
        from database import DatabaseError

        client = TestClient(app)
        job = {"job_id": 34, "job_name": "JOB_A", "is_create": True}
        with patch(
            "api.routers.jobs.get_database_readiness",
            return_value={"config_table_select": True},
        ), patch("api.routers.jobs.get_partition_job", return_value=job), patch(
            "api.routers.jobs.run_partition_job_manual",
            side_effect=DatabaseError("worker boom"),
        ):
            res = client.post("/api/jobs/34/run")
        self.assertEqual(res.status_code, 502)
        self.assertIn("worker boom", res.json()["detail"])

    def test_update_sql_does_not_touch_last_run_columns(self) -> None:
        import database

        sql = database.build_update_partition_job_sql()
        self.assertIn("update_data_to_partition_job_table", sql)
        self.assertNotIn("last_run_time", sql)
        self.assertNotIn("last_run_status", sql)

    def test_validate_form_still_used_for_edit_payload(self) -> None:
        validated = validate_form_data(VALID_UPDATE)
        self.assertEqual(validated["job_name"], "JOB_EDIT_TEST")
        self.assertTrue(validated["is_enabled"])


class ScheduledDurationSqlContractTests(unittest.TestCase):
    def test_realtime_scheduler_sql_writes_execution_duration_ms(self) -> None:
        with open("sql/realtime_scheduler_v1.sql", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("execution_duration_ms", source)
        self.assertIn("v_duration_ms", source)
        self.assertIn("v_exec_start", source)
        # next_run_time semantics must remain present.
        self.assertIn("next_run_time", source)
        self.assertIn("last_run_status", source)


class ManualDurationMigrationContractTests(unittest.TestCase):
    @classmethod
    def _manual_sql(cls) -> str:
        with open(
            "sql/migrations/20260929_03_run_partition_job_manual_duration.sql",
            encoding="utf-8",
        ) as handle:
            return handle.read()

    def test_stamp_helper_migration_is_gone(self) -> None:
        self.assertFalse(
            os.path.exists(
                "sql/migrations/20260929_03_stamp_latest_job_log_duration.sql"
            )
        )

    def test_manual_success_insert_includes_duration(self) -> None:
        text = self._manual_sql()
        self.assertIn("'MANUAL_SUCCESS'", text)
        success = text.split("'MANUAL_SUCCESS'")[0]
        # Column list of the success INSERT precedes the status literal.
        self.assertIn("execution_duration_ms", success[-400:])
        self.assertIn("v_duration_ms", text.split("'MANUAL_SUCCESS'")[1][:800])

    def test_manual_fail_insert_includes_duration(self) -> None:
        text = self._manual_sql()
        self.assertIn("'MANUAL_FAIL'", text)
        fail_tail = text.split("'MANUAL_FAIL'")[1]
        self.assertIn("v_duration_ms", fail_tail[:800])
        fail_head = text.split("'MANUAL_FAIL'")[0]
        self.assertIn("execution_duration_ms", fail_head[-400:])

    def test_manual_fail_returns_without_reraise(self) -> None:
        text = self._manual_sql()
        handler = text.split("WHEN OTHERS THEN", 1)[1].split("END;", 1)[0]
        self.assertIn("status := 'MANUAL_FAIL'", handler)
        self.assertIn("RETURN NEXT", handler)
        self.assertNotIn("RAISE;", handler)
        self.assertIn("RETURNS TABLE", text)
        self.assertIn(
            "DROP FUNCTION IF EXISTS mubasher_oms.run_partition_job_manual(numeric);",
            text,
        )
        self.assertNotIn(
            "DROP FUNCTION IF EXISTS mubasher_oms.run_partition_job_manual(numeric) CASCADE",
            text,
        )
        self.assertIn("v_dependents > 0", text)
        self.assertIn("SECURITY INVOKER", text)
        create_at = text.find("CREATE FUNCTION mubasher_oms.run_partition_job_manual")
        owner_at = text.find(
            "ALTER FUNCTION mubasher_oms.run_partition_job_manual(numeric)"
        )
        grant_at = text.find(
            "GRANT EXECUTE ON FUNCTION\n    mubasher_oms.run_partition_job_manual(numeric)"
        )
        self.assertGreater(create_at, 0)
        self.assertGreater(owner_at, create_at)
        self.assertGreater(grant_at, owner_at)
        self.assertNotIn("GRANT ALL ON", text)
        self.assertNotIn("-- GRANT EXECUTE ON FUNCTION", text)
        grant_stmt = text[grant_at : text.find(";", grant_at) + 1]
        self.assertIn("partition_job_ui", grant_stmt)
        self.assertIn(
            "ALTER FUNCTION mubasher_oms.run_partition_job_manual(numeric)",
            text,
        )

    def test_manual_does_not_modify_schedule_columns(self) -> None:
        text = self._manual_sql()
        body = text.split("$function$")[1]
        self.assertNotIn("next_run_time", body)
        self.assertNotIn("last_run_time", body)
        self.assertNotIn("UPDATE mubasher_oms.partitioning_job_table", body)

    def test_manual_preserves_worker_signatures(self) -> None:
        text = self._manual_sql()
        self.assertIn("mubasher_oms.create_any_table_partition(", text)
        create_args = text.split("mubasher_oms.create_any_table_partition(")[1].split(
            ");"
        )[0]
        self.assertIn("v_job.partition_period::integer", create_args)
        self.assertNotIn("db_config", create_args)
        drop_args = text.split("mubasher_oms.drop_any_table_partition(")[1].split(");")[
            0
        ]
        self.assertIn("v_job.create_drop_interval", drop_args)
        self.assertNotIn("partition_unit", drop_args)

    def test_duration_column_migration_is_nullable(self) -> None:
        with open(
            "sql/migrations/20260929_01_add_execution_duration_ms.sql",
            encoding="utf-8",
        ) as handle:
            text = handle.read()
        self.assertIn("execution_duration_ms BIGINT", text)
        self.assertNotIn("NOT NULL", text.split("ADD COLUMN")[1].split(";")[0])
        self.assertNotIn("UPDATE ", text.split("BEGIN;")[1])

    def test_no_runtime_reference_to_stamp_helper(self) -> None:
        root = os.path.abspath(".")
        hits = []
        skip_dirs = {".git", "node_modules", "__pycache__", ".next", "archives"}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for name in filenames:
                if not name.endswith((".py", ".sql")):
                    continue
                if name.startswith("test_"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as handle:
                    contents = handle.read()
                if "stamp_latest_job_log_duration" in contents:
                    hits.append(os.path.relpath(path, root))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
