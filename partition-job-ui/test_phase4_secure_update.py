"""Phase 4 — secure Edit / Enable / Disable update function contracts."""

from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from api.routers import jobs as jobs_router


MIGRATION = os.path.join(
    "sql",
    "migrations",
    "20261013_02_secure_update_partition_job.sql",
)

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


def _read_migration() -> str:
    with open(MIGRATION, encoding="utf-8") as handle:
        return handle.read()


def _function_body(text: str) -> str:
    start = text.index(
        "CREATE OR REPLACE FUNCTION mubasher_oms.update_data_to_partition_job_table("
    )
    end = text.index("$function$;", start) + len("$function$;")
    return text[start:end]


class SecureUpdateMigrationContractTests(unittest.TestCase):
    def test_security_definer_and_search_path(self) -> None:
        text = _read_migration()
        body = _function_body(text)
        self.assertIn("SECURITY DEFINER", body)
        self.assertIn(
            "SET search_path = pg_catalog, mubasher_oms, pg_temp",
            body,
        )
        self.assertNotIn("SECURITY INVOKER", body)

    def test_owner_and_grants(self) -> None:
        text = _read_migration()
        # Strip SQL comments before privilege assertions.
        code_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            code_lines.append(line.split("--", 1)[0])
        code = "\n".join(code_lines)
        self.assertIn("OWNER TO mubasher_oms", code)
        self.assertIn("REVOKE ALL ON FUNCTION", code)
        self.assertIn("FROM PUBLIC", code)
        self.assertIn("GRANT EXECUTE ON FUNCTION", code)
        self.assertIn("TO partition_job_ui", code)
        self.assertNotIn("GRANT UPDATE ON", code)
        self.assertNotIn(
            "GRANT ALL ON TABLE mubasher_oms.partitioning_job_table",
            code,
        )
        self.assertNotIn("CASCADE", code)

    def test_for_update_lock(self) -> None:
        body = _function_body(_read_migration())
        self.assertIn("FOR UPDATE", body)
        lock_at = body.find("FOR UPDATE")
        update_at = body.rfind("UPDATE mubasher_oms.partitioning_job_table")
        self.assertGreater(update_at, lock_at)

    def test_never_touches_immutable_or_history(self) -> None:
        body = _function_body(_read_migration())
        # SET list must not assign job_id / last_run_* .
        set_block = body.split("UPDATE mubasher_oms.partitioning_job_table AS j")[1]
        set_block = set_block.split("WHERE j.job_id = p_job_id;")[0]
        self.assertNotIn("job_id", set_block.replace("j.job_id", ""))
        self.assertNotIn("last_run_time", set_block)
        self.assertNotIn("last_run_status", set_block)
        self.assertNotIn("partitioning_job_table_log", body)

    def test_disable_preserves_next_run_path(self) -> None:
        body = _function_body(_read_migration())
        self.assertIn("v_effective_next := v_job.next_run_time", body)
        self.assertIn("p_is_enabled IS NOT TRUE", body)

    def test_reenable_recalculates_via_cron_helper(self) -> None:
        body = _function_body(_read_migration())
        self.assertIn("cron_to_interval_or_next_run", body)
        self.assertIn("v_now + v_schedule.schedule_interval", body)
        self.assertNotIn("interval '1 day'", body)
        self.assertNotIn("v_job.frequency", body)
        self.assertIn("Fail closed", body)

    def test_signature_preserved(self) -> None:
        text = _read_migration()
        self.assertIn("RETURNS void", text)
        self.assertIn("p_is_create_drop_interval  interval", text)
        # Same 13-arg signature as 20260929_02.
        legacy = open(
            os.path.join(
                "sql",
                "migrations",
                "20260929_02_update_data_to_partition_job_table.sql",
            ),
            encoding="utf-8",
        ).read()
        for arg in (
            "p_job_id",
            "p_job_name",
            "p_is_enabled",
            "p_table_schema",
            "p_table_name",
            "p_db_config_para",
            "p_job_schedule",
            "p_frequency",
            "p_next_run_time",
            "p_partition_unit",
            "p_partition_period",
            "p_is_create",
            "p_is_create_drop_interval",
        ):
            self.assertIn(arg, text)
            self.assertIn(arg, legacy)


class PrepareAndApiPhase4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

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

    def test_disable_preserves_last_run_fields_in_payload(self) -> None:
        body = jobs_router.JobUpdateBody(**{**VALID_UPDATE, "is_enabled": False})
        validated = jobs_router._prepare_job_update(self._existing(), body)
        self.assertFalse(validated["is_enabled"])
        self.assertNotIn("last_run_time", validated)
        self.assertNotIn("last_run_status", validated)

    def test_enable_null_next_run_calculates_future(self) -> None:
        existing = self._existing(is_enabled=False, next_run_time=None)
        body = jobs_router.JobUpdateBody(
            **{**VALID_UPDATE, "is_enabled": True, "next_run_time": None}
        )
        validated = jobs_router._prepare_job_update(existing, body)
        self.assertTrue(validated["is_enabled"])
        self.assertGreater(
            validated["next_run_time"], datetime.now() - timedelta(seconds=5)
        )

    def test_api_returns_job_after_update(self) -> None:
        existing = self._existing()
        refreshed = {**existing, "job_name": "JOB_RENAMED", "is_enabled": False}
        with patch("api.routers.jobs.get_partition_job", side_effect=[existing, refreshed]), patch(
            "api.routers.jobs.update_partition_job", return_value=None
        ), patch(
            "api.routers.jobs.notify_scheduler_refresh",
            return_value=(True, "ok"),
        ):
            res = self.client.patch(
                "/api/jobs/34",
                json={**VALID_UPDATE, "job_name": "JOB_RENAMED", "is_enabled": False},
            )
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual(payload["job"]["job_name"], "JOB_RENAMED")
        self.assertFalse(payload["job"]["is_enabled"])

    def test_api_db_failure_no_fake_success(self) -> None:
        from database import DatabaseError

        existing = self._existing()
        with patch("api.routers.jobs.get_partition_job", return_value=existing), patch(
            "api.routers.jobs.update_partition_job",
            side_effect=DatabaseError("permission denied"),
        ), patch("api.routers.jobs.notify_scheduler_refresh") as refresh_mock:
            res = self.client.patch("/api/jobs/34", json=VALID_UPDATE)
        self.assertGreaterEqual(res.status_code, 400)
        self.assertNotIn("Job updated successfully", res.text)
        refresh_mock.assert_not_called()

    def test_api_does_not_echo_secrets(self) -> None:
        from database import DatabaseError

        existing = self._existing()
        with patch("api.routers.jobs.get_partition_job", return_value=existing), patch(
            "api.routers.jobs.update_partition_job",
            side_effect=DatabaseError(
                "The application database user does not have the required permission."
            ),
        ):
            res = self.client.patch("/api/jobs/34", json=VALID_UPDATE)
        text = res.text.lower()
        self.assertNotIn("password", text)
        self.assertNotIn("secretstring", text)
        self.assertNotIn("db_password", text)

    def test_create_path_untouched_by_update_migration(self) -> None:
        import database

        create_sql = database.build_create_partition_job_sql()
        self.assertIn("insert_data_to_partition_job_table", create_sql)
        self.assertNotIn("update_data_to_partition_job_table", create_sql)

    def test_manual_run_sql_untouched(self) -> None:
        import database

        sql = database.build_run_partition_job_manual_sql()
        self.assertIn("run_partition_job_manual", sql)

    def test_frontend_sends_boolean_enabled(self) -> None:
        path = os.path.join(
            "frontend", "src", "components", "jobs", "job-form.tsx"
        )
        text = open(path, encoding="utf-8").read()
        self.assertIn("is_enabled: values.is_enabled", text)
        self.assertIn('onChange={(e) => set("is_enabled", e.target.checked)}', text)

    def test_jobs_page_refreshes_selected_after_update(self) -> None:
        path = os.path.join("frontend", "src", "app", "jobs", "page.tsx")
        text = open(path, encoding="utf-8").read()
        self.assertIn("setSelected((prev) =>", text)
        self.assertIn("onUpdated", text)
        self.assertIn("void load()", text)

    def test_python39_compatible_jobs_router(self) -> None:
        path = os.path.join("api", "routers", "jobs.py")
        source = open(path, encoding="utf-8").read()
        compile(source, path, "exec")
        self.assertNotRegex(source, r"(?m)^\s*match\s+")
        self.assertGreaterEqual(sys.version_info[:2], (3, 9))


class SchedulerSkipDisabledContractTests(unittest.TestCase):
    def test_scheduled_run_still_skips_disabled(self) -> None:
        with open(
            os.path.join("sql", "realtime_scheduler_v1.sql"), encoding="utf-8"
        ) as handle:
            text = handle.read()
        self.assertIn("SKIPPED_DISABLED", text)
        self.assertIn("IF v_job.is_enabled IS NOT TRUE THEN", text)


if __name__ == "__main__":
    unittest.main()
