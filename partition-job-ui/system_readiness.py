"""Read-only PartOps system diagnostics for Partition Manager deployments."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from psycopg.rows import dict_row

from database import (
    INSERT_FUNCTION_ARGUMENT_TYPES,
    DatabaseError,
    PgAgentNotInstalledError,
    _connection,
    _main_db_kwargs,
    _pgagent_db_kwargs,
    get_database_readiness,
    manual_run_function_name,
    partition_job_function_identity,
    partition_job_log_table_name,
    partition_job_schema,
    partition_job_table_name,
    partition_job_update_function_identity,
)
from scheduler_client import fetch_scheduler_status, scheduler_status_url

logger = logging.getLogger(__name__)

STATUS_READY = "ready"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"
STATUS_NOT_REQUIRED = "not_required"
STATUS_UNKNOWN = "unknown"

_STATUS_RANK = {
    STATUS_FAILED: 0,
    STATUS_WARNING: 1,
    STATUS_UNKNOWN: 2,
    STATUS_READY: 3,
    STATUS_NOT_REQUIRED: 4,
}

_OVERALL_LABELS = {
    STATUS_READY: "Ready",
    STATUS_WARNING: "Ready with warnings",
    STATUS_FAILED: "Not ready",
    STATUS_UNKNOWN: "Unknown",
    STATUS_NOT_REQUIRED: "Not required",
}

_CACHE_TTL_SECONDS = 10.0
_cache_payload: Optional[Dict[str, Any]] = None
_cache_monotonic_at: float = 0.0

ALLOWLISTED_SYSTEMD_UNITS = (
    "partition-job-api.service",
    "partition-job-ui.service",
    "partition-job-scheduler.service",
)

LOCAL_TCP_PORTS: Tuple[Tuple[int, str], ...] = (
    (8501, "Next.js UI"),
    (8001, "Partition API"),
    (8765, "Scheduler control API"),
)

_LEGACY_PGAGENT_SCANNERS = (
    "run_partition_create_jobs",
    "run_partition_drop_jobs",
)

# Matches update_data_to_partition_job_table in sql/migrations/20260929_02_*.sql
UPDATE_FUNCTION_ARGUMENT_TYPES = (
    "numeric,"
    "character varying,"
    "boolean,"
    "character varying,"
    "character varying,"
    "jsonb,"
    "character varying,"
    "interval,"
    "timestamp without time zone,"
    "character varying,"
    "numeric,"
    "boolean,"
    "interval"
)

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential)",
    re.IGNORECASE,
)

_PGAGENT_EXISTS_SQL = "SELECT to_regclass('pgagent.pga_job') IS NOT NULL;"

_PGAGENT_LEGACY_STEPS_SQL = """
SELECT
    j.jobid AS job_id,
    j.jobname AS job_name,
    j.jobenabled AS job_enabled,
    s.jstenabled AS step_enabled,
    s.jstcode AS code
FROM pgagent.pga_job j
JOIN pgagent.pga_jobstep s ON s.jstjobid = j.jobid
WHERE s.jstkind = 's';
"""

_CATEGORY_META: Tuple[Tuple[str, str], ...] = (
    ("database", "Database"),
    ("scheduler", "Realtime scheduler"),
    ("host", "Host services"),
    ("configuration", "Runtime configuration"),
    ("legacy", "Legacy pgAgent"),
    ("frontend", "Frontend"),
)

_PRODUCT = {
    "name": "PartOps",
    "subtitle": "Partition Manager read-only diagnostics",
}


def _check(
    key: str,
    label: str,
    category: str,
    status: str,
    summary: str,
    details: Optional[Any] = None,
    remediation_hint: Optional[str] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "key": key,
        "label": label,
        "category": category,
        "status": status,
        "summary": summary,
    }
    if details is not None:
        item["details"] = _scrub_secrets(details)
    if remediation_hint:
        item["remediation_hint"] = remediation_hint
    return item


def _scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        scrubbed: Dict[str, Any] = {}
        for key, inner in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                scrubbed[key] = "[redacted]"
            else:
                scrubbed[key] = _scrub_secrets(inner)
        return scrubbed
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_secrets(item) for item in value)
    return value


def _rollup(statuses: List[str]) -> str:
    actionable = [s for s in statuses if s != STATUS_NOT_REQUIRED]
    if not actionable:
        return STATUS_NOT_REQUIRED
    worst = min(actionable, key=lambda s: _STATUS_RANK.get(s, 2))
    return worst


def _overall(category_statuses: List[str]) -> str:
    return _rollup(category_statuses)


def _regprocedure(schema: str, function: str, argtypes: str) -> str:
    return f"{schema}.{function}({argtypes})"


def _env_configured(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def _scheduler_health_url() -> str:
    base = scheduler_status_url().rstrip("/")
    if base.endswith("/internal/scheduler/status"):
        return base[: -len("/internal/scheduler/status")] + "/health"
    return "http://127.0.0.1:8765/health"


def _http_get_json(url: str, timeout: float = 2.0) -> Tuple[bool, Optional[Any], str]:
    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200))
            if status >= 400:
                return False, None, f"HTTP {status}"
            if not raw.strip():
                return True, {}, "ok"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return True, raw[:500], "non-json body"
            return True, parsed, "ok"
    except urllib.error.HTTPError as exc:
        return False, None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        logger.debug("HTTP GET failed for %s: %s", url, exc)
        return False, None, str(exc)


def check_database_connection() -> Dict[str, Any]:
    category = "database"
    try:
        kwargs = _main_db_kwargs()
    except DatabaseError as exc:
        return _check(
            "database_connection",
            "Database connection",
            category,
            STATUS_FAILED,
            "Database environment is not configured.",
            details={"error": exc.message},
            remediation_hint="Set DB_HOST, DB_PORT, DB_NAME, DB_USER, and DB_PASSWORD in .env.",
        )

    safe_kwargs = {
        "host": kwargs.get("host"),
        "port": kwargs.get("port"),
        "dbname": kwargs.get("dbname"),
        "user": kwargs.get("user"),
        "sslmode": kwargs.get("sslmode"),
    }
    try:
        with _connection(kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok;")
                row = cur.fetchone()
                ok = bool(row and row[0] == 1)
        if not ok:
            return _check(
                "database_connection",
                "Database connection",
                category,
                STATUS_FAILED,
                "Connected but sanity query did not return expected result.",
                details=safe_kwargs,
            )
        return _check(
            "database_connection",
            "Database connection",
            category,
            STATUS_READY,
            "Connected to PostgreSQL.",
            details=safe_kwargs,
        )
    except DatabaseError as exc:
        return _check(
            "database_connection",
            "Database connection",
            category,
            STATUS_FAILED,
            exc.message,
            details=safe_kwargs,
            remediation_hint="Verify database reachability, credentials, and pg_hba.conf.",
        )


def check_required_tables() -> List[Dict[str, Any]]:
    category = "database"
    schema = partition_job_schema()
    config_table = partition_job_table_name()
    log_table = partition_job_log_table_name()
    checks: List[Dict[str, Any]] = []

    try:
        with _connection(_main_db_kwargs()) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT
                        to_regclass(%(config)s) IS NOT NULL AS config_exists,
                        to_regclass(%(log)s) IS NOT NULL AS log_exists
                    """,
                    {
                        "config": f"{schema}.{config_table}",
                        "log": f"{schema}.{log_table}",
                    },
                )
                row = dict(cur.fetchone() or {})

                if row.get("config_exists"):
                    checks.append(
                        _check(
                            "config_table",
                            "Configuration table",
                            category,
                            STATUS_READY,
                            f"{schema}.{config_table} exists.",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "config_table",
                            "Configuration table",
                            category,
                            STATUS_FAILED,
                            f"{schema}.{config_table} was not found.",
                            remediation_hint="Apply partition job framework DDL migrations.",
                        )
                    )

                if row.get("log_exists"):
                    checks.append(
                        _check(
                            "log_table",
                            "Job log table",
                            category,
                            STATUS_READY,
                            f"{schema}.{log_table} exists.",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "log_table",
                            "Job log table",
                            category,
                            STATUS_FAILED,
                            f"{schema}.{log_table} was not found.",
                            remediation_hint="Apply partition job framework DDL migrations.",
                        )
                    )

                if row.get("log_exists"):
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                              FROM information_schema.columns
                             WHERE table_schema = %(schema)s
                               AND table_name = %(table)s
                               AND column_name = 'execution_duration_ms'
                        ) AS has_duration;
                        """,
                        {"schema": schema, "table": log_table},
                    )
                    duration_row = dict(cur.fetchone() or {})
                    if duration_row.get("has_duration"):
                        checks.append(
                            _check(
                                "log_duration_column",
                                "execution_duration_ms column",
                                category,
                                STATUS_READY,
                                "Log table includes execution_duration_ms.",
                            )
                        )
                    else:
                        checks.append(
                            _check(
                                "log_duration_column",
                                "execution_duration_ms column",
                                category,
                                STATUS_WARNING,
                                "Log table is missing execution_duration_ms.",
                                remediation_hint="Apply migration 20260929_01_add_execution_duration_ms.sql.",
                            )
                        )
    except DatabaseError as exc:
        checks.append(
            _check(
                "required_tables",
                "Required tables",
                category,
                STATUS_FAILED,
                exc.message,
            )
        )
    return checks


def _function_exists(
    cur: Any, schema: str, function: str, argtypes: str
) -> bool:
    identity = _regprocedure(schema, function, argtypes)
    cur.execute("SELECT to_regprocedure(%(fn)s) IS NOT NULL AS ok;", {"fn": identity})
    row = cur.fetchone()
    return bool(row and row["ok"])


def check_required_functions() -> List[Dict[str, Any]]:
    category = "database"
    schema = partition_job_schema()
    _, insert_fn = partition_job_function_identity()
    _, update_fn = partition_job_update_function_identity()
    manual_fn = manual_run_function_name()

    required: List[Tuple[str, str, str, str]] = [
        (
            "insert_function",
            "Insert configuration function",
            insert_fn,
            INSERT_FUNCTION_ARGUMENT_TYPES,
        ),
        (
            "manual_run_function",
            "Manual run function",
            manual_fn,
            "numeric",
        ),
        (
            "upcoming_jobs_function",
            "get_upcoming_partition_jobs",
            "get_upcoming_partition_jobs",
            "interval",
        ),
        (
            "scheduled_run_function",
            "run_partition_job_scheduled",
            "run_partition_job_scheduled",
            "numeric, timestamp without time zone",
        ),
        (
            "create_any_function",
            "create_any_table_partition",
            "create_any_table_partition",
            "character varying, character varying, character varying, integer, interval",
        ),
        (
            "drop_any_function",
            "drop_any_table_partition",
            "drop_any_table_partition",
            "character varying, character varying, interval",
        ),
    ]

    optional: List[Tuple[str, str, str, str]] = [
        (
            "update_function",
            "Update configuration function",
            update_fn,
            UPDATE_FUNCTION_ARGUMENT_TYPES,
        ),
    ]

    checks: List[Dict[str, Any]] = []
    try:
        with _connection(_main_db_kwargs()) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                for key, label, fn, argtypes in required:
                    identity = _regprocedure(schema, fn, argtypes)
                    if _function_exists(cur, schema, fn, argtypes):
                        checks.append(
                            _check(
                                key,
                                label,
                                category,
                                STATUS_READY,
                                f"{identity} is deployed.",
                            )
                        )
                    else:
                        checks.append(
                            _check(
                                key,
                                label,
                                category,
                                STATUS_FAILED,
                                f"{identity} was not found.",
                                remediation_hint="Deploy realtime_scheduler_v1.sql and framework functions.",
                            )
                        )

                for key, label, fn, argtypes in optional:
                    identity = _regprocedure(schema, fn, argtypes)
                    if _function_exists(cur, schema, fn, argtypes):
                        checks.append(
                            _check(
                                key,
                                label,
                                category,
                                STATUS_READY,
                                f"{identity} is deployed.",
                            )
                        )
                    else:
                        checks.append(
                            _check(
                                key,
                                label,
                                category,
                                STATUS_WARNING,
                                f"{identity} is not deployed (optional for some flows).",
                                remediation_hint="Apply 20260929_02_update_data_to_partition_job_table.sql for Edit.",
                            )
                        )
    except DatabaseError as exc:
        checks.append(
            _check(
                "required_functions",
                "Database functions",
                category,
                STATUS_FAILED,
                exc.message,
            )
        )
    return checks


def check_database_privileges() -> List[Dict[str, Any]]:
    category = "database"
    try:
        readiness = get_database_readiness()
    except DatabaseError as exc:
        return [
            _check(
                "database_privileges",
                "Database privileges",
                category,
                STATUS_FAILED,
                exc.message,
            )
        ]

    mapping: List[Tuple[str, str, str]] = [
        ("schema_exists", "Schema exists", "schema_exists"),
        ("schema_usage", "Schema USAGE", "schema_usage"),
        ("insert_function_exists", "Insert function exists", "insert_function_exists"),
        ("insert_function_execute", "Insert function EXECUTE", "insert_function_execute"),
        ("config_table_exists", "Config table exists", "config_table_exists"),
        ("config_table_insert", "Config table INSERT", "config_table_insert"),
        ("config_table_select", "Config table SELECT", "config_table_select"),
        ("log_table_exists", "Log table exists", "log_table_exists"),
        ("log_table_select", "Log table SELECT", "log_table_select"),
        ("sequence_exists", "Sequence exists", "sequence_exists"),
        ("sequence_usage", "Sequence USAGE", "sequence_usage"),
    ]

    checks: List[Dict[str, Any]] = []
    for key, label, field in mapping:
        value = readiness.get(field)
        if value is True:
            status = STATUS_READY
            summary = "Granted or present."
        elif value is False:
            status = STATUS_FAILED
            summary = "Missing or not granted."
        elif value is None:
            status = STATUS_UNKNOWN
            summary = "Could not evaluate (prerequisite object missing)."
        else:
            status = STATUS_UNKNOWN
            summary = "Unexpected readiness value."

        checks.append(
            _check(
                key,
                label,
                category,
                status,
                summary,
                details={
                    "db_user": readiness.get("db_user"),
                    "db_name": readiness.get("db_name"),
                    field: value,
                },
            )
        )
    return checks


def check_scheduler() -> List[Dict[str, Any]]:
    category = "scheduler"
    checks: List[Dict[str, Any]] = []

    health_url = _scheduler_health_url()
    ok, payload, message = _http_get_json(health_url, timeout=2.0)
    if ok:
        summary = "Scheduler health endpoint responded."
        if isinstance(payload, dict) and payload.get("ok") is False:
            checks.append(
                _check(
                    "scheduler_health",
                    "Scheduler /health",
                    category,
                    STATUS_WARNING,
                    "Health endpoint returned ok=false.",
                    details=_scrub_secrets(payload),
                )
            )
        else:
            checks.append(
                _check(
                    "scheduler_health",
                    "Scheduler /health",
                    category,
                    STATUS_READY,
                    summary,
                    details=_scrub_secrets(payload) if payload is not None else None,
                )
            )
    else:
        checks.append(
            _check(
                "scheduler_health",
                "Scheduler /health",
                category,
                STATUS_FAILED,
                f"Scheduler health check failed: {message}",
                details={"url": health_url},
                remediation_hint="Ensure partition-job-scheduler.service is active on 127.0.0.1:8765.",
            )
        )

    status_ok, status_payload, status_message = fetch_scheduler_status()
    if status_ok and isinstance(status_payload, dict):
        checks.append(
            _check(
                "scheduler_status",
                "Scheduler in-memory status",
                category,
                STATUS_READY,
                "Scheduler status endpoint returned JSON.",
                details=_scrub_secrets(status_payload),
            )
        )
    else:
        checks.append(
            _check(
                "scheduler_status",
                "Scheduler in-memory status",
                category,
                STATUS_WARNING if ok else STATUS_FAILED,
                status_message,
                details={"url": scheduler_status_url()},
                remediation_hint="Check scheduler logs if the service is running but status is unavailable.",
            )
        )
    return checks


def check_systemd_services() -> List[Dict[str, Any]]:
    category = "host"
    checks: List[Dict[str, Any]] = []
    for unit in ALLOWLISTED_SYSTEMD_UNITS:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=2,
                shell=False,
            )
            state = (result.stdout or "").strip() or (result.stderr or "").strip()
            if result.returncode == 0 and state == "active":
                checks.append(
                    _check(
                        f"systemd_{unit}",
                        unit,
                        category,
                        STATUS_READY,
                        "active",
                        details={"state": state},
                    )
                )
            elif state in ("inactive", "failed", "deactivating"):
                checks.append(
                    _check(
                        f"systemd_{unit}",
                        unit,
                        category,
                        STATUS_FAILED,
                        state or "not active",
                        details={"returncode": result.returncode, "state": state},
                        remediation_hint=f"Run: systemctl status {unit} and journalctl -u {unit}",
                    )
                )
            else:
                checks.append(
                    _check(
                        f"systemd_{unit}",
                        unit,
                        category,
                        STATUS_WARNING,
                        state or "unknown state",
                        details={"returncode": result.returncode, "state": state},
                    )
                )
        except FileNotFoundError:
            checks.append(
                _check(
                    f"systemd_{unit}",
                    unit,
                    category,
                    STATUS_UNKNOWN,
                    "systemctl is not available on this host.",
                )
            )
            break
        except subprocess.TimeoutExpired:
            checks.append(
                _check(
                    f"systemd_{unit}",
                    unit,
                    category,
                    STATUS_UNKNOWN,
                    "systemctl timed out after 2s.",
                )
            )
        except OSError as exc:
            checks.append(
                _check(
                    f"systemd_{unit}",
                    unit,
                    category,
                    STATUS_UNKNOWN,
                    f"Could not query systemd: {exc}",
                )
            )
    return checks


def check_local_ports() -> List[Dict[str, Any]]:
    category = "host"
    checks: List[Dict[str, Any]] = []
    for port, label in LOCAL_TCP_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                checks.append(
                    _check(
                        f"tcp_{port}",
                        f"TCP 127.0.0.1:{port}",
                        category,
                        STATUS_READY,
                        f"{label} port is accepting connections.",
                        details={"port": port, "label": label},
                    )
                )
        except OSError as exc:
            checks.append(
                _check(
                    f"tcp_{port}",
                    f"TCP 127.0.0.1:{port}",
                    category,
                    STATUS_FAILED,
                    f"{label} port is not reachable.",
                    details={"port": port, "label": label, "error": str(exc)},
                    remediation_hint="Confirm the corresponding systemd unit is active.",
                )
            )
    return checks


def check_runtime_configuration() -> List[Dict[str, Any]]:
    category = "configuration"
    checks: List[Dict[str, Any]] = []

    required_vars = ("DB_NAME", "DB_USER", "DB_PASSWORD")
    for name in required_vars:
        if name == "DB_PASSWORD":
            present = _env_configured(name)
            checks.append(
                _check(
                    f"env_{name.lower()}",
                    name,
                    category,
                    STATUS_READY if present else STATUS_FAILED,
                    "Configured" if present else "Missing",
                )
            )
            continue
        present = _env_configured(name)
        checks.append(
            _check(
                f"env_{name.lower()}",
                name,
                category,
                STATUS_READY if present else STATUS_FAILED,
                "Set" if present else "Missing",
                remediation_hint=None if present else f"Add {name} to the application .env file.",
            )
        )

    optional_vars = (
        "DB_HOST",
        "DB_PORT",
        "PARTITION_JOB_SCHEMA",
        "PARTITION_SCHEDULER_STATUS_URL",
        "PARTITION_API_ORIGIN",
    )
    for name in optional_vars:
        present = _env_configured(name)
        checks.append(
            _check(
                f"env_{name.lower()}",
                name,
                category,
                STATUS_READY if present else STATUS_WARNING,
                "Set" if present else "Using default",
            )
        )
    return checks


def check_legacy_conflicts() -> List[Dict[str, Any]]:
    category = "legacy"
    try:
        with _connection(_pgagent_db_kwargs()) as conn:
            with conn.cursor() as cur:
                cur.execute(_PGAGENT_EXISTS_SQL)
                exists_row = cur.fetchone()
                if not exists_row or not exists_row[0]:
                    return [
                        _check(
                            "legacy_pgagent_scanners",
                            "Legacy pgAgent scanner jobs",
                            category,
                            STATUS_NOT_REQUIRED,
                            "pgAgent is not installed; no legacy scanner conflict to evaluate.",
                        )
                    ]

            conflicts: List[Dict[str, Any]] = []
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_PGAGENT_LEGACY_STEPS_SQL)
                for step in cur.fetchall():
                    if not bool(step.get("job_enabled")) or not bool(step.get("step_enabled")):
                        continue
                    code = (step.get("code") or "").lower()
                    for scanner in _LEGACY_PGAGENT_SCANNERS:
                        if scanner in code:
                            conflicts.append(
                                {
                                    "job_id": step.get("job_id"),
                                    "job_name": step.get("job_name"),
                                    "scanner": scanner,
                                }
                            )

            if not conflicts:
                return [
                    _check(
                        "legacy_pgagent_scanners",
                        "Legacy pgAgent scanner jobs",
                        category,
                        STATUS_READY,
                        "No enabled pgAgent steps reference legacy polling scanners.",
                    )
                ]

            return [
                _check(
                    "legacy_pgagent_scanners",
                    "Legacy pgAgent scanner jobs",
                    category,
                    STATUS_WARNING,
                    "Enabled pgAgent steps still reference legacy run_partition_create_jobs/drop scanners.",
                    details={"conflicts": conflicts},
                    remediation_hint="Disable or remove legacy pgAgent jobs after migrating to the realtime scheduler.",
                )
            ]
    except DatabaseError as exc:
        return [
            _check(
                "legacy_pgagent_scanners",
                "Legacy pgAgent scanner jobs",
                category,
                STATUS_UNKNOWN,
                exc.message,
            )
        ]


def check_pgagent_access() -> List[Dict[str, Any]]:
    category = "legacy"
    try:
        with _connection(_pgagent_db_kwargs()) as conn:
            with conn.cursor() as cur:
                cur.execute(_PGAGENT_EXISTS_SQL)
                exists_row = cur.fetchone()
                if not exists_row or not exists_row[0]:
                    return [
                        _check(
                            "pgagent_access",
                            "pgAgent catalog access",
                            category,
                            STATUS_NOT_REQUIRED,
                            "pgAgent is not installed in the configured database.",
                        )
                    ]

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT COUNT(*) AS job_count FROM pgagent.pga_job;"
                )
                row = dict(cur.fetchone() or {})
                count = int(row.get("job_count") or 0)
                return [
                    _check(
                        "pgagent_access",
                        "pgAgent catalog access",
                        category,
                        STATUS_READY,
                        "pgAgent catalog is readable.",
                        details={"job_count": count},
                    )
                ]
    except PgAgentNotInstalledError:
        return [
            _check(
                "pgagent_access",
                "pgAgent catalog access",
                category,
                STATUS_NOT_REQUIRED,
                "pgAgent is not installed.",
            )
        ]
    except DatabaseError as exc:
        return [
            _check(
                "pgagent_access",
                "pgAgent catalog access",
                category,
                STATUS_WARNING,
                exc.message,
                remediation_hint="Verify PGAGENT_DB_* settings if pgAgent runs in a separate database.",
            )
        ]


def check_frontend_runtime() -> List[Dict[str, Any]]:
    category = "frontend"
    url = "http://127.0.0.1:8501/"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=2) as response:
            status = int(getattr(response, "status", 200))
            if 200 <= status < 400:
                return [
                    _check(
                        "frontend_http",
                        "Next.js UI HTTP",
                        category,
                        STATUS_READY,
                        f"UI responded with HTTP {status}.",
                        details={"url": url, "status": status},
                    )
                ]
            return [
                _check(
                    "frontend_http",
                    "Next.js UI HTTP",
                    category,
                    STATUS_WARNING,
                    f"UI responded with HTTP {status}.",
                    details={"url": url, "status": status},
                )
            ]
    except Exception as exc:  # noqa: BLE001
        return [
            _check(
                "frontend_http",
                "Next.js UI HTTP",
                category,
                STATUS_FAILED,
                "UI is not reachable on port 8501.",
                details={"url": url, "error": str(exc)},
                remediation_hint="Ensure partition-job-ui.service is active.",
            )
        ]


def _collect_all_checks() -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    checks.append(check_database_connection())
    checks.extend(check_required_tables())
    checks.extend(check_required_functions())
    checks.extend(check_database_privileges())
    checks.extend(check_scheduler())
    checks.extend(check_systemd_services())
    checks.extend(check_local_ports())
    checks.extend(check_runtime_configuration())
    checks.extend(check_legacy_conflicts())
    checks.extend(check_pgagent_access())
    checks.extend(check_frontend_runtime())
    return checks


def _group_categories(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_category: Dict[str, List[Dict[str, Any]]] = {key: [] for key, _ in _CATEGORY_META}
    for item in checks:
        cat = item.get("category") or "configuration"
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(item)

    grouped: List[Dict[str, Any]] = []
    for key, label in _CATEGORY_META:
        cat_checks = by_category.get(key) or []
        if not cat_checks:
            continue
        statuses = [str(c.get("status", STATUS_UNKNOWN)) for c in cat_checks]
        grouped.append(
            {
                "key": key,
                "label": label,
                "status": _rollup(statuses),
                "checks": cat_checks,
            }
        )
    return grouped


def _count_statuses(checks: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {
        STATUS_READY: 0,
        STATUS_WARNING: 0,
        STATUS_FAILED: 0,
        STATUS_NOT_REQUIRED: 0,
        STATUS_UNKNOWN: 0,
    }
    for item in checks:
        status = str(item.get("status", STATUS_UNKNOWN))
        if status not in counts:
            counts[STATUS_UNKNOWN] += 1
        else:
            counts[status] += 1
    return counts


def build_system_readiness(force_refresh: bool = False) -> Dict[str, Any]:
    """Build the PartOps readiness report (10s in-process cache)."""
    global _cache_payload, _cache_monotonic_at

    now_mono = time.monotonic()
    if (
        not force_refresh
        and _cache_payload is not None
        and (now_mono - _cache_monotonic_at) < _CACHE_TTL_SECONDS
    ):
        cached = dict(_cache_payload)
        cached["from_cache"] = True
        return cached

    checks = _collect_all_checks()
    categories = _group_categories(checks)
    category_statuses = [str(c["status"]) for c in categories]
    overall = _overall(category_statuses)

    payload: Dict[str, Any] = {
        "overall_status": overall,
        "overall_label": _OVERALL_LABELS.get(overall, overall.title()),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "from_cache": False,
        "counts": _count_statuses(checks),
        "categories": categories,
        "product": dict(_PRODUCT),
    }
    payload = _scrub_secrets(payload)

    _cache_payload = payload
    _cache_monotonic_at = now_mono
    return dict(payload)
