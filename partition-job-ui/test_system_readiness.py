"""Unit tests for PartOps system readiness (mocked; no real DB/systemd)."""

from __future__ import annotations

import inspect
import os
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import system_readiness as sr
from api.main import app
from system_readiness import (
    ALLOWLISTED_SYSTEMD_UNITS,
    LOCAL_TCP_PORTS,
    STATUS_FAILED,
    STATUS_NOT_REQUIRED,
    STATUS_READY,
    STATUS_UNKNOWN,
    STATUS_WARNING,
    _check,
    _overall,
    _rollup,
    _scrub_secrets,
    build_system_readiness,
    check_database_connection,
    check_legacy_conflicts,
    check_local_ports,
    check_pgagent_access,
    check_required_functions,
    check_required_tables,
    check_runtime_configuration,
    check_scheduler,
    check_systemd_services,
)

_ROOT = Path(__file__).resolve().parent
_FRONTEND = _ROOT / "frontend"


def _reset_readiness_cache() -> None:
    sr._cache_payload = None
    sr._cache_monotonic_at = 0.0


def _sample_check(
    key: str = "sample",
    category: str = "database",
    status: str = STATUS_READY,
) -> dict:
    return _check(key, key, category, status, "summary")


class HelperFunctionTests(unittest.TestCase):
    def test_rollup_ignores_not_required(self) -> None:
        self.assertEqual(_rollup([STATUS_NOT_REQUIRED, STATUS_READY]), STATUS_READY)

    def test_overall_ready_when_all_ready(self) -> None:
        self.assertEqual(_overall([STATUS_READY, STATUS_READY]), STATUS_READY)

    def test_overall_warning_when_warnings_present_not_failed(self) -> None:
        self.assertEqual(
            _overall([STATUS_READY, STATUS_WARNING, STATUS_NOT_REQUIRED]),
            STATUS_WARNING,
        )

    def test_overall_failed_when_any_failed(self) -> None:
        self.assertEqual(
            _overall([STATUS_READY, STATUS_WARNING, STATUS_FAILED]),
            STATUS_FAILED,
        )

    def test_scrub_secrets_nested_dict(self) -> None:
        raw = {
            "host": "db.example",
            "db": {"DB_PASSWORD": "secret", "user": "app"},
            "items": [{"api_key": "k1", "name": "x"}],
        }
        scrubbed = _scrub_secrets(raw)
        self.assertEqual(scrubbed["host"], "db.example")
        self.assertEqual(scrubbed["db"]["DB_PASSWORD"], "[redacted]")
        self.assertEqual(scrubbed["db"]["user"], "app")
        self.assertEqual(scrubbed["items"][0]["api_key"], "[redacted]")
        self.assertEqual(scrubbed["items"][0]["name"], "x")

    def test_check_scrubs_details(self) -> None:
        item = _check(
            "k",
            "L",
            "configuration",
            STATUS_READY,
            "ok",
            details={"password": "x"},
        )
        self.assertEqual(item["details"]["password"], "[redacted]")


class BuildSystemReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_readiness_cache()

    def tearDown(self) -> None:
        _reset_readiness_cache()

    def test_build_overall_ready_via_collect_patch(self) -> None:
        checks = [
            _sample_check("a", "database", STATUS_READY),
            _sample_check("b", "scheduler", STATUS_READY),
        ]
        with patch.object(sr, "_collect_all_checks", return_value=checks):
            payload = build_system_readiness(force_refresh=True)
        self.assertEqual(payload["overall_status"], STATUS_READY)
        self.assertEqual(payload["overall_label"], "Ready")
        self.assertFalse(payload["from_cache"])

    def test_build_overall_warning_via_collect_patch(self) -> None:
        checks = [
            _sample_check("a", "database", STATUS_READY),
            _sample_check("b", "legacy", STATUS_WARNING),
        ]
        with patch.object(sr, "_collect_all_checks", return_value=checks):
            payload = build_system_readiness(force_refresh=True)
        self.assertEqual(payload["overall_status"], STATUS_WARNING)

    def test_build_overall_failed_via_collect_patch(self) -> None:
        checks = [
            _sample_check("a", "database", STATUS_FAILED),
            _sample_check("b", "scheduler", STATUS_READY),
        ]
        with patch.object(sr, "_collect_all_checks", return_value=checks):
            payload = build_system_readiness(force_refresh=True)
        self.assertEqual(payload["overall_status"], STATUS_FAILED)

    def test_cache_second_call_from_cache_within_ttl(self) -> None:
        checks = [_sample_check()]
        with patch.object(sr, "_collect_all_checks", return_value=checks) as collect:
            first = build_system_readiness(force_refresh=True)
            second = build_system_readiness()
        self.assertFalse(first["from_cache"])
        self.assertTrue(second["from_cache"])
        self.assertEqual(collect.call_count, 1)

    def test_force_refresh_bypasses_cache(self) -> None:
        checks = [_sample_check()]
        with patch.object(sr, "_collect_all_checks", return_value=checks) as collect:
            build_system_readiness(force_refresh=True)
            build_system_readiness(force_refresh=True)
        self.assertEqual(collect.call_count, 2)


class DatabaseConnectionTests(unittest.TestCase):
    def test_connected_path(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (1,)
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        @contextmanager
        def fake_connection(_kwargs):
            yield conn

        kwargs = {
            "host": "h",
            "port": 5432,
            "dbname": "d",
            "user": "u",
            "password": "p",
            "sslmode": "prefer",
        }
        with patch.object(sr, "_main_db_kwargs", return_value=kwargs), patch.object(
            sr, "_connection", side_effect=fake_connection
        ):
            result = check_database_connection()
        self.assertEqual(result["status"], STATUS_READY)
        self.assertNotIn("password", result.get("details", {}))


class RequiredTablesTests(unittest.TestCase):
    def _mock_tables_cursor(self, has_duration: bool) -> MagicMock:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = [
            {"config_exists": True, "log_exists": True},
            {"has_duration": has_duration},
        ]
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        @contextmanager
        def fake_connection(_kwargs):
            yield conn

        return fake_connection

    def test_duration_column_missing_warning(self) -> None:
        with patch.object(sr, "_main_db_kwargs", return_value={}), patch.object(
            sr, "_connection", side_effect=self._mock_tables_cursor(False)
        ), patch.object(sr, "partition_job_schema", return_value="mubasher_oms"), patch.object(
            sr, "partition_job_table_name", return_value="partitioning_job_table"
        ), patch.object(
            sr, "partition_job_log_table_name", return_value="partitioning_job_table_log"
        ):
            checks = check_required_tables()
        duration = next(c for c in checks if c["key"] == "log_duration_column")
        self.assertEqual(duration["status"], STATUS_WARNING)


class RequiredFunctionsTests(unittest.TestCase):
    def test_optional_update_function_missing_warning_not_failed(self) -> None:
        conn = MagicMock()
        cur = MagicMock()

        def exists(_cur, _schema, fn, _argtypes):
            return fn != "update_data_to_partition_job_table"

        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False

        @contextmanager
        def fake_connection(_kwargs):
            yield conn

        with patch.object(sr, "_main_db_kwargs", return_value={}), patch.object(
            sr, "_connection", side_effect=fake_connection
        ), patch.object(sr, "_function_exists", side_effect=exists), patch.object(
            sr, "partition_job_schema", return_value="mubasher_oms"
        ), patch.object(
            sr,
            "partition_job_function_identity",
            return_value=("mubasher_oms", "insert_data_to_partition_job_table"),
        ), patch.object(
            sr,
            "partition_job_update_function_identity",
            return_value=("mubasher_oms", "update_data_to_partition_job_table"),
        ), patch.object(
            sr,
            "stamp_duration_function_identity",
            return_value=("mubasher_oms", "stamp_latest_job_log_duration"),
        ), patch.object(
            sr, "manual_run_function_name", return_value="run_partition_job_manual"
        ):
            checks = check_required_functions()

        update = next(c for c in checks if c["key"] == "update_function")
        self.assertEqual(update["status"], STATUS_WARNING)
        required_failed = [
            c
            for c in checks
            if c["key"] in ("insert_function", "manual_run_function")
            and c["status"] == STATUS_FAILED
        ]
        self.assertEqual(required_failed, [])


class RuntimeConfigurationTests(unittest.TestCase):
    def test_db_password_reported_without_value(self) -> None:
        env = {
            "DB_NAME": "partdb",
            "DB_USER": "partuser",
            "DB_PASSWORD": "super-secret-value",
        }
        with patch.dict(os.environ, env, clear=False):
            checks = check_runtime_configuration()
        pwd = next(c for c in checks if c["key"] == "env_db_password")
        self.assertEqual(pwd["status"], STATUS_READY)
        self.assertEqual(pwd["summary"], "Configured")
        self.assertNotIn("details", pwd)
        blob = str(checks)
        self.assertNotIn("super-secret-value", blob)

    def test_missing_required_env_failed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            checks = check_runtime_configuration()
        for key in ("env_db_name", "env_db_user", "env_db_password"):
            item = next(c for c in checks if c["key"] == key)
            self.assertEqual(item["status"], STATUS_FAILED)


class SchedulerTests(unittest.TestCase):
    def test_scheduler_offline(self) -> None:
        with patch.object(
            sr, "_http_get_json", return_value=(False, None, "connection refused")
        ), patch.object(
            sr, "fetch_scheduler_status", return_value=(False, None, "offline")
        ), patch.object(sr, "scheduler_status_url", return_value="http://127.0.0.1:8765/internal/scheduler/status"):
            checks = check_scheduler()
        health = next(c for c in checks if c["key"] == "scheduler_health")
        status = next(c for c in checks if c["key"] == "scheduler_status")
        self.assertEqual(health["status"], STATUS_FAILED)
        self.assertEqual(status["status"], STATUS_FAILED)

    def test_scheduler_malformed_status(self) -> None:
        with patch.object(
            sr, "_http_get_json", return_value=(True, {"ok": True}, "ok")
        ), patch.object(
            sr, "fetch_scheduler_status", return_value=(False, "not-json", "bad payload")
        ), patch.object(
            sr, "scheduler_status_url", return_value="http://127.0.0.1:8765/internal/scheduler/status"
        ):
            checks = check_scheduler()
        status = next(c for c in checks if c["key"] == "scheduler_status")
        self.assertEqual(status["status"], STATUS_WARNING)


class SystemdTests(unittest.TestCase):
    def test_allowlist_only_three_units(self) -> None:
        self.assertEqual(
            ALLOWLISTED_SYSTEMD_UNITS,
            (
                "partition-job-api.service",
                "partition-job-ui.service",
                "partition-job-scheduler.service",
            ),
        )

    def test_shell_false_and_allowlist_in_source(self) -> None:
        source = inspect.getsource(check_systemd_services)
        self.assertIn("shell=False", source)
        self.assertNotIn("shell=True", source)

    @patch.object(sr.subprocess, "run")
    def test_systemctl_unavailable_unknown(self, run_mock: MagicMock) -> None:
        run_mock.side_effect = FileNotFoundError("systemctl")
        checks = check_systemd_services()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], STATUS_UNKNOWN)
        self.assertIn("systemctl", checks[0]["summary"])

    @patch.object(sr.subprocess, "run")
    def test_subprocess_timeout_unknown(self, run_mock: MagicMock) -> None:
        run_mock.side_effect = subprocess.TimeoutExpired(cmd="systemctl", timeout=2)
        checks = check_systemd_services()
        self.assertTrue(checks)
        self.assertEqual(checks[0]["status"], STATUS_UNKNOWN)
        self.assertIn("timed out", checks[0]["summary"])


class LocalPortsTests(unittest.TestCase):
    def test_ports_contract_no_8000(self) -> None:
        ports = [port for port, _label in LOCAL_TCP_PORTS]
        self.assertEqual(ports, [8501, 8001, 8765])
        source = inspect.getsource(check_local_ports)
        self.assertNotIn("8000", source)

    @patch.object(sr.socket, "create_connection")
    def test_check_local_ports_only_expected_ports(self, create_conn: MagicMock) -> None:
        create_conn.side_effect = OSError("refused")
        checks = check_local_ports()
        checked_ports = sorted(
            int(c["details"]["port"]) for c in checks if "details" in c
        )
        self.assertEqual(checked_ports, [8001, 8501, 8765])
        for args, _kwargs in create_conn.call_args_list:
            _host, port = args[0]
            self.assertNotIn(port, (8000,))


class LegacyPgAgentTests(unittest.TestCase):
    def _legacy_conn(self, pgagent_installed: bool, conflicts=None):
        conn = MagicMock()
        cur_exists = MagicMock()
        cur_exists.fetchone.return_value = (pgagent_installed,)

        cur_steps = MagicMock()
        cur_steps.fetchall.return_value = conflicts or []

        def cursor_factory(*_args, **kwargs):
            cur = MagicMock()
            if kwargs.get("row_factory"):
                cur.fetchall.return_value = conflicts or []
                cur.__enter__ = MagicMock(return_value=cur)
                cur.__exit__ = MagicMock(return_value=False)
                return cur
            cur.fetchone.return_value = (pgagent_installed,)
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            return cur

        conn.cursor.side_effect = cursor_factory

        @contextmanager
        def fake_connection(_kwargs):
            yield conn

        return fake_connection

    def test_legacy_conflict_warning(self) -> None:
        conflicts = [
            {
                "job_id": 1,
                "job_name": "legacy",
                "job_enabled": True,
                "step_enabled": True,
                "code": "SELECT run_partition_create_jobs();",
            }
        ]
        with patch.object(sr, "_pgagent_db_kwargs", return_value={}), patch.object(
            sr, "_connection", side_effect=self._legacy_conn(True, conflicts)
        ):
            checks = check_legacy_conflicts()
        self.assertEqual(checks[0]["status"], STATUS_WARNING)

    def test_no_legacy_ready(self) -> None:
        with patch.object(sr, "_pgagent_db_kwargs", return_value={}), patch.object(
            sr, "_connection", side_effect=self._legacy_conn(True, [])
        ):
            checks = check_legacy_conflicts()
        self.assertEqual(checks[0]["status"], STATUS_READY)

    def test_pgagent_absent_not_required(self) -> None:
        with patch.object(sr, "_pgagent_db_kwargs", return_value={}), patch.object(
            sr, "_connection", side_effect=self._legacy_conn(False)
        ):
            legacy = check_legacy_conflicts()
            access = check_pgagent_access()
        self.assertEqual(legacy[0]["status"], STATUS_NOT_REQUIRED)
        self.assertEqual(access[0]["status"], STATUS_NOT_REQUIRED)


class SystemReadinessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_readiness_cache()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        _reset_readiness_cache()

    def test_get_readiness_schema(self) -> None:
        sample = {
            "overall_status": STATUS_READY,
            "overall_label": "Ready",
            "checked_at": "2026-09-29T12:00:00+00:00",
            "from_cache": False,
            "counts": {
                STATUS_READY: 1,
                STATUS_WARNING: 0,
                STATUS_FAILED: 0,
                STATUS_NOT_REQUIRED: 0,
                STATUS_UNKNOWN: 0,
            },
            "categories": [],
            "product": {"name": "PartOps", "subtitle": "Partition Manager read-only diagnostics"},
        }
        with patch("api.routers.system.build_system_readiness", return_value=sample):
            res = self.client.get("/api/system/readiness")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        for key in (
            "overall_status",
            "overall_label",
            "checked_at",
            "from_cache",
            "counts",
            "categories",
            "product",
        ):
            self.assertIn(key, body)

    def test_refresh_query_forces_rebuild(self) -> None:
        with patch(
            "api.routers.system.build_system_readiness",
            return_value={"overall_status": STATUS_READY, "from_cache": False},
        ) as mock_build:
            self.client.get("/api/system/readiness?refresh=true")
        mock_build.assert_called_once_with(force_refresh=True)


class BrandingSourceContractTests(unittest.TestCase):
    def test_sidebar_no_workspace_box(self) -> None:
        text = (_FRONTEND / "src/components/shell/sidebar.tsx").read_text(
            encoding="utf-8"
        )
        lowered = text.lower()
        self.assertNotIn("workspace", lowered)
        self.assertNotIn("mubasher_oms", text)

    def test_sidebar_and_layout_contain_partops(self) -> None:
        sidebar = (_FRONTEND / "src/components/shell/sidebar.tsx").read_text(
            encoding="utf-8"
        )
        layout = (_FRONTEND / "src/app/layout.tsx").read_text(encoding="utf-8")
        self.assertIn("PartOps", sidebar.replace("Part<span", "PartOps"))
        self.assertIn("PartOps", layout)

    def test_next_config_defaults_api_to_8001_not_8000(self) -> None:
        config = (_FRONTEND / "next.config.ts").read_text(encoding="utf-8")
        self.assertIn("8001", config)
        self.assertNotIn("8000", config)

    def test_theme_provider_storage_key(self) -> None:
        theme = (
            _FRONTEND / "src/components/theme/theme-provider.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('STORAGE_KEY = "partops-theme"', theme)
        self.assertIn("themeStorageKey", theme)

    def test_system_page_route_exists(self) -> None:
        path = _FRONTEND / "src/app/system/page.tsx"
        self.assertTrue(path.is_file(), msg=f"missing {path}")


if __name__ == "__main__":
    unittest.main()
