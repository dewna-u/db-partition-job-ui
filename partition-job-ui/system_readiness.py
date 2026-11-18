"""Read-only PartOps system diagnostics for Partition Manager deployments."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from psycopg import Error as PsycopgError
from psycopg.rows import dict_row

from database import (
    INSERT_FUNCTION_ARGUMENT_TYPES,
    DatabaseError,
    PgAgentNotInstalledError,
    _connection,
    _main_db_kwargs,
    _map_psycopg_error,
    _pgagent_db_kwargs,
    get_database_readiness,
    manual_run_function_name,
    partition_job_function_identity,
    partition_job_log_table_name,
    partition_job_schema,
    partition_job_table_name,
    partition_job_update_function_identity,
)
from db_credentials import credential_provider_status, secret_provider
from observability import observability_status, timing
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

# Phase 5F.1: bounded in-process cache (failures cached too). The System page
# and Overview load readiness on demand; "Refresh checks" bypasses the cache.
_CACHE_TTL_SECONDS = 30.0
_cache_payload: Optional[Dict[str, Any]] = None
_cache_monotonic_at: float = 0.0
# Single-flight: concurrent requests wait for one computation instead of
# each running every check (each check may wait on a connect timeout).
_cache_lock = threading.Lock()

# Readiness-only bounds (Phase 5F.1). A readiness probe must answer quickly
# even when dependencies are down; normal API / scheduler connections keep
# DB_CONNECT_TIMEOUT. Bounded so a typo cannot make the probe hang.
_READINESS_HTTP_TIMEOUT = 1.5
_READINESS_PORT_TIMEOUT = 1.0


def readiness_connect_timeout() -> int:
    """Connect timeout (s) for readiness DB probes: env, bounded 2..10, default 3."""
    raw = (os.getenv("PARTITION_READINESS_CONNECT_TIMEOUT_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 3
    except ValueError:
        value = 3
    return max(2, min(value, 10))


def _clamp_connect_timeout(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(kwargs)
    current = out.get("connect_timeout")
    limit = readiness_connect_timeout()
    out["connect_timeout"] = limit if current is None else max(1, min(int(current), limit))
    return out


def _readiness_main_kwargs() -> Dict[str, Any]:
    return _clamp_connect_timeout(_main_db_kwargs())


def _readiness_pgagent_kwargs() -> Dict[str, Any]:
    return _clamp_connect_timeout(_pgagent_db_kwargs())


# One CONTROL-database connection is shared by the dependent checks of a
# single readiness build (thread-local; never kept across requests).
_shared = threading.local()


def _shared_conn() -> Any:
    """The readiness build's shared control connection, or None (standalone)."""
    return getattr(_shared, "conn", None)


def _reset_shared() -> None:
    conn = _shared_conn()
    if conn is not None:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def _control_connection() -> Generator[Any, None, None]:
    conn = getattr(_shared, "conn", None)
    if conn is None:
        with _connection(_readiness_main_kwargs) as own:
            yield own
        return
    try:
        yield conn
    except PsycopgError as exc:
        raise _map_psycopg_error(exc) from None
    finally:
        try:
            conn.rollback()  # each check starts clean
        except Exception:  # noqa: BLE001
            pass

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
    ("targets", "Database targets"),
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
        kwargs = _readiness_main_kwargs()  # bounded connect timeout (Phase 5F.1)
    except DatabaseError as exc:
        return _check(
            "database_connection",
            "Database connection",
            category,
            STATUS_FAILED,
            "Database environment is not configured.",
            details={"error": exc.message},
            remediation_hint=(
                "Configure DB_HOST/DB_PORT/DB_NAME and either "
                "PARTITION_DB_SECRET_PROVIDER=env with DB_USER/DB_PASSWORD, or "
                "PARTITION_DB_SECRET_PROVIDER=aws with PARTITION_DB_SECRET_ID."
            ),
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
        with _control_connection() as conn:
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
        with _control_connection() as conn:
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
                                remediation_hint=(
                                    "Apply 20260929_02_update_data_to_partition_job_table.sql "
                                    "then Phase 4 20261013_02_secure_update_partition_job.sql "
                                    "for Edit/Enable/Disable."
                                ),
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


def check_database_targets() -> List[Dict[str, Any]]:
    """Phase 5C: one READ-ONLY health item per managed target.

    The control database and every additional target get their own item; one
    unreachable target never turns the others (or the control DB) red.
    """
    category = "targets"
    from database import list_database_targets
    from database_targets import (
        DISABLED,
        HEALTHY,
        HEALTHY_WITH_WARNINGS,
        check_target_health,
    )
    from observability import emit_target_health

    try:
        targets, invalid, available = list_database_targets(_shared_conn())
        _reset_shared()
    except DatabaseError as exc:
        return [_check("target_registry", "Database target registry", category,
                       STATUS_FAILED, exc.message)]
    if not available:
        return [_check("target_registry", "Database target registry", category, STATUS_WARNING,
                       "Single-database mode: target registry not installed.",
                       remediation_hint="Apply migration 20261117_01_partops_database_target.sql.")]
    checks: List[Dict[str, Any]] = []
    status_map = {HEALTHY: STATUS_READY, HEALTHY_WITH_WARNINGS: STATUS_WARNING,
                  DISABLED: STATUS_NOT_REQUIRED}
    def _bounded_connect(**kwargs: Any) -> Any:
        from psycopg import connect as _pg_connect

        return _pg_connect(**_clamp_connect_timeout(kwargs))

    def _health(target: Any) -> Dict[str, Any]:
        try:
            return check_target_health(target, connect=_bounded_connect)
        except Exception as exc:  # noqa: BLE001 — isolate per target
            return {"state": "CONFIG_ERROR", "detail": type(exc).__name__}

    # Targets are independent: check them concurrently (bounded connect each).
    if len(targets) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(targets)),
                                thread_name_prefix="readiness-target") as pool:
            healths = list(pool.map(_health, targets))
    else:
        healths = [_health(t) for t in targets]
    for target, health in zip(targets, healths):
        state = str(health.get("state"))
        emit_target_health(target=target.target_key, state=state)
        role = "Control database" if target.is_control_database else "Managed target"
        checks.append(_check(
            "target_{}".format(target.target_key),
            "{} {} ({})".format(role, target.display_name, target.target_key),
            category,
            status_map.get(state, STATUS_FAILED),
            "{}: {}".format(state, health.get("detail") or ""),
            details={"target": target.to_public_dict(), "health": health},
        ))
    for bad in invalid:
        checks.append(_check(
            "target_invalid_{}".format(bad.get("target_id")),
            "Database target {}".format(bad.get("target_key")),
            category, STATUS_FAILED, "CONFIG_ERROR: " + str(bad.get("error")),
        ))
    return checks


def check_framework_manifest() -> List[Dict[str, Any]]:
    """Phase 5B: compare the database with sql/framework/framework_manifest.json.

    READ-ONLY (SET TRANSACTION READ ONLY + rollback). Evaluated for the API
    scope as the API role (EXECUTE on the functions the API calls). The
    scheduler runs its own gate for the scheduler scope.
    """
    category = "database"
    try:
        from framework_manifest import (
            FAIL as FW_FAIL,
            WARNING as FW_WARNING,
            evaluate_snapshot,
            fetch_catalog_snapshot,
            load_manifest,
        )

        manifest = load_manifest()
        with _control_connection() as conn:
            snapshot = fetch_catalog_snapshot(conn, manifest)
        report = evaluate_snapshot(
            manifest, snapshot, scope="api", check_current_user_execute=True
        )
    except DatabaseError as exc:
        return [_check("framework_manifest", "Database framework (manifest)", category,
                       STATUS_FAILED, exc.message)]
    except Exception as exc:  # noqa: BLE001 — readiness must never crash
        logger.exception("framework manifest check failed")
        return [_check("framework_manifest", "Database framework (manifest)", category,
                       STATUS_UNKNOWN, "Framework check could not run ({}).".format(type(exc).__name__))]
    counts = report.counts()
    problems = [r.as_dict() for r in report.results if r.status in (FW_FAIL, FW_WARNING)][:40]
    if report.failures:
        status = STATUS_FAILED
    elif report.warnings:
        status = STATUS_WARNING
    else:
        status = STATUS_READY
    summary = "Framework {}: PASS={} WARNING={} FAIL={}; application expects {}, database reports {}.".format(
        report.overall, counts["PASS"], counts["WARNING"], counts["FAIL"],
        report.expected_version, report.database_version or "unknown",
    )
    return [_check(
        "framework_manifest", "Database framework (manifest)", category, status, summary,
        details={"problems": problems},
        remediation_hint=(
            "Run scripts/preflight_framework.py as a DBA; capture missing legacy "
            "definitions per sql/framework/README.md; apply pending migrations."
        ) if status != STATUS_READY else None,
    )]


def check_database_privileges() -> List[Dict[str, Any]]:
    category = "database"
    try:
        readiness = get_database_readiness(_shared_conn())
        _reset_shared()
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
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="readiness-sched") as pool:
        status_future = pool.submit(fetch_scheduler_status)  # concurrent with /health
        ok, payload, message = _http_get_json(health_url, timeout=_READINESS_HTTP_TIMEOUT)
        status_ok, status_payload, status_message = status_future.result()
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
    def _probe(port: int) -> Optional[OSError]:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=_READINESS_PORT_TIMEOUT):
                return None
        except OSError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=max(1, len(LOCAL_TCP_PORTS)),
                            thread_name_prefix="readiness-port") as pool:
        results = list(pool.map(_probe, [port for port, _label in LOCAL_TCP_PORTS]))
    for (port, label), error in zip(LOCAL_TCP_PORTS, results):
        try:
            if error is not None:
                raise error
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

    # Datadog observability is informational only — never a readiness blocker.
    dd = observability_status()
    checks.append(
        _check(
            "datadog_observability",
            "Datadog observability",
            category,
            STATUS_NOT_REQUIRED,
            "enabled={0}; dogstatsd={1}:{2}; env={3}".format(
                dd.get("datadog_enabled"),
                "configured" if dd.get("dogstatsd_host_configured") else "missing",
                dd.get("dogstatsd_port"),
                dd.get("environment"),
            ),
            details=dd,
            remediation_hint=(
                "Set PARTOPS_DATADOG_ENABLED=true after Datadog Agent is installed "
                "and listening on localhost UDP 8125."
                if not dd.get("datadog_enabled")
                else None
            ),
        )
    )

    # Credential provider status never calls Secrets Manager.
    try:
        provider = secret_provider()
    except Exception as exc:  # noqa: BLE001
        provider = "invalid"
        checks.append(
            _check(
                "credential_provider",
                "Credential provider",
                category,
                STATUS_FAILED,
                str(exc),
            )
        )
    else:
        status = credential_provider_status()
        checks.append(
            _check(
                "credential_provider",
                "Credential provider",
                category,
                STATUS_READY
                if status.get("credential_configuration") == "ready"
                else STATUS_FAILED,
                "provider={0}; configuration={1}".format(
                    status.get("credential_provider"),
                    status.get("credential_configuration"),
                ),
                details={
                    k: v
                    for k, v in status.items()
                    if k
                    not in (
                        "error",
                    )
                },
                remediation_hint=(
                    None
                    if status.get("credential_configuration") == "ready"
                    else "Configure env credentials or AWS Secrets Manager variables."
                ),
            )
        )

    required_vars = ["DB_NAME"]
    if provider == "env":
        required_vars.extend(["DB_USER", "DB_PASSWORD"])
    elif provider == "aws":
        # Username may come from secret; DB_USER is optional fallback.
        required_vars.append("PARTITION_DB_SECRET_ID")
        if _env_configured("DB_USER"):
            checks.append(
                _check(
                    "env_db_user",
                    "DB_USER",
                    category,
                    STATUS_READY,
                    "Set (fallback if secret omits username)",
                )
            )
        checks.append(
            _check(
                "env_db_password",
                "DB_PASSWORD",
                category,
                STATUS_READY if not _env_configured("DB_PASSWORD") else STATUS_WARNING,
                (
                    "Not required in AWS mode (omit after Secrets Manager verification)"
                    if not _env_configured("DB_PASSWORD")
                    else "Still present — remove after AWS mode is verified"
                ),
            )
        )

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
        with _connection(_readiness_pgagent_kwargs) as conn:
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
        with _connection(_readiness_pgagent_kwargs) as conn:
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
        with urllib.request.urlopen(request, timeout=_READINESS_HTTP_TIMEOUT) as response:
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


# Checks that need the CONTROL database. When the connection check already
# failed they can only fail the same way, each after its own connect timeout
# (measured: ~47 s for a full refresh with the control DB down, longer than
# the UI proxy waits). Phase 5E.1: report them as "not checked" instead.
_CONTROL_DB_DEPENDENT: Tuple[Tuple[str, str, str], ...] = (
    ("required_tables", "Required tables", "database"),
    ("required_functions", "Required functions", "database"),
    ("framework_manifest", "Database framework (manifest)", "database"),
    ("target_registry", "Database target registry", "targets"),
    ("database_privileges", "Database privileges", "database"),
)


def _control_db_unavailable_checks() -> List[Dict[str, Any]]:
    return [
        _check(key, label, category, STATUS_UNKNOWN,
               "Not checked: the control database is unavailable (see Database connection).")
        for key, label, category in _CONTROL_DB_DEPENDENT
    ]


def _safe_db_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: kwargs.get(k) for k in ("host", "port", "dbname", "user", "sslmode")}


def _control_database_checks() -> List[Dict[str, Any]]:
    """Connection check + every control-DB dependent check on ONE connection.

    The connection probe IS the shared connection (Phase 5F.1: one control
    connection per readiness build instead of one per check). If the control
    database cannot be reached the dependent checks are not run (they could
    only fail the same way after their own timeouts).
    """
    try:
        safe = _safe_db_kwargs(_readiness_main_kwargs())
    except DatabaseError:
        # Not configured: no network wait; reuse the detailed config message.
        return [check_database_connection()] + _control_db_unavailable_checks()
    checks: List[Dict[str, Any]] = []
    try:
        with _connection(_readiness_main_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok;")
                row = cur.fetchone()
            conn.rollback()
            if not (row and row[0] == 1):
                return [_check("database_connection", "Database connection", "database", STATUS_FAILED,
                               "Connected but sanity query did not return expected result.",
                               details=safe)] + _control_db_unavailable_checks()
            checks.append(_check("database_connection", "Database connection", "database",
                                 STATUS_READY, "Connected to PostgreSQL.", details=safe))
            _shared.conn = conn
            try:
                checks.extend(check_required_tables())
                checks.extend(check_required_functions())
                checks.extend(check_framework_manifest())
                checks.extend(check_database_targets())
                checks.extend(check_database_privileges())
            finally:
                _shared.conn = None
    except DatabaseError as exc:
        if not checks:
            return [_check("database_connection", "Database connection", "database", STATUS_FAILED,
                           exc.message, details=safe,
                           remediation_hint="Verify database reachability, credentials, and pg_hba.conf.")
                    ] + _control_db_unavailable_checks()
        # The database went away during the dependent checks.
        logger.warning("event=readiness_control_db_lost detail=%s", exc.message)
        done = {c.get("key") for c in checks}
        checks.extend(c for c in _control_db_unavailable_checks() if c["key"] not in done)
    return checks


# Independent groups run concurrently; within the control-DB group the
# order is sequential on one connection. Each group is total-bounded by its
# own timeouts (connect <= readiness_connect_timeout(), HTTP 1.5 s, ports 1 s).
def _collect_all_checks() -> List[Dict[str, Any]]:
    groups: List[Callable[[], Any]] = [
        _control_database_checks,
        check_scheduler,
        check_systemd_services,
        check_local_ports,
        check_runtime_configuration,
        check_legacy_conflicts,
        check_pgagent_access,
        check_frontend_runtime,
    ]
    with ThreadPoolExecutor(max_workers=len(groups), thread_name_prefix="readiness") as pool:
        futures = [pool.submit(group) for group in groups]
        checks: List[Dict[str, Any]] = []
        for group, future in zip(groups, futures):
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — one group never breaks readiness
                logger.exception("event=readiness_group_failed group=%s", group.__name__)
                result = [_check(group.__name__, group.__name__, "configuration", STATUS_UNKNOWN,
                                 "Check could not run ({}).".format(type(exc).__name__))]
            checks.extend(result if isinstance(result, list) else [result])
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


def _cached(now_mono: float) -> Optional[Dict[str, Any]]:
    if _cache_payload is not None and (now_mono - _cache_monotonic_at) < _CACHE_TTL_SECONDS:
        cached = dict(_cache_payload)
        cached["from_cache"] = True
        return cached
    return None


def invalidate_readiness_cache() -> None:
    global _cache_payload, _cache_monotonic_at
    with _cache_lock:
        _cache_payload = None
        _cache_monotonic_at = 0.0


def build_system_readiness(force_refresh: bool = False) -> Dict[str, Any]:
    """Build the PartOps readiness report.

    Phase 5F.1: 30 s in-process TTL cache (successes AND failures), guarded by
    a lock so concurrent requests share one computation (single flight).
    ``force_refresh`` (the explicit "Refresh checks" action) bypasses and
    replaces the cache. Never cached indefinitely.
    """
    if not force_refresh:
        hit = _cached(time.monotonic())
        if hit is not None:
            return hit
    with _cache_lock:
        if not force_refresh:
            hit = _cached(time.monotonic())  # filled while we waited
            if hit is not None:
                return hit
        return _build_uncached()


def _build_uncached() -> Dict[str, Any]:
    global _cache_payload, _cache_monotonic_at
    started = time.monotonic()
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
    _cache_monotonic_at = time.monotonic()
    duration_ms = (time.monotonic() - started) * 1000.0
    payload["duration_ms"] = int(duration_ms)
    timing("readiness.duration_ms", duration_ms, tags={"overall": str(overall)})
    return dict(payload)
