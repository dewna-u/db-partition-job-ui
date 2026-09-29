"""Tests for Edit Configured Job + Execution Duration features."""

from __future__ import annotations

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

    def test_manual_run_stamps_duration_after_success(self) -> None:
        import database

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.description = [("result",)]
        mock_cur.fetchone.return_value = ("ok",)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
        mock_conn.transaction.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Two connections: run then stamp.
        with patch.object(database, "_connection", return_value=mock_conn), patch.object(
            database, "_main_db_kwargs", return_value={}
        ), patch.object(
            database, "build_run_partition_job_manual_sql", return_value="SELECT 1"
        ), patch.object(
            database,
            "build_stamp_log_duration_sql",
            return_value="SELECT stamp(%(job_id)s, %(duration_ms)s)",
        ):
            result = database.run_partition_job_manual(34)

        self.assertEqual(result, "ok")
        # First execute = manual; second = stamp with duration_ms bound.
        self.assertGreaterEqual(mock_cur.execute.call_count, 2)
        stamp_call = mock_cur.execute.call_args_list[-1]
        self.assertIn("duration_ms", stamp_call[0][1])
        self.assertGreaterEqual(stamp_call[0][1]["duration_ms"], 0)

    def test_manual_run_succeeds_even_if_stamp_fails(self) -> None:
        import database

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.description = [("result",)]
        mock_cur.fetchone.return_value = ("ok",)

        call_state = {"n": 0}

        def execute_side_effect(sql, params=None):
            call_state["n"] += 1
            if call_state["n"] > 1:
                raise RuntimeError("stamp failed")

        mock_cur.execute.side_effect = execute_side_effect
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
        mock_conn.transaction.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(database, "_connection", return_value=mock_conn), patch.object(
            database, "_main_db_kwargs", return_value={}
        ), patch.object(
            database, "build_run_partition_job_manual_sql", return_value="SELECT 1"
        ), patch.object(
            database, "build_stamp_log_duration_sql", return_value="SELECT stamp"
        ):
            result = database.run_partition_job_manual(34)

        self.assertEqual(result, "ok")

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


if __name__ == "__main__":
    unittest.main()
