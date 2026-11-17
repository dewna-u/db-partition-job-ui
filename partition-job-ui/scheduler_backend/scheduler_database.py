"""Short-lived PostgreSQL access for the realtime scheduler backend.

Every public helper opens a connection, does one logical operation, then closes.
No connection pool. No module-level connection/cursor objects.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Generator, Optional

from dotenv import load_dotenv
from psycopg import Connection, Error as PsycopgError, connect
from psycopg.errors import InsufficientPrivilege, UndefinedFunction, UndefinedTable
from psycopg.rows import dict_row

from db_credentials import (
    DatabaseCredentialError,
    invalidate_database_secret_cache,
    is_authentication_error,
    resolve_database_credentials,
)
from observability import emit_db_connection_failure
from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.db_safety import (
    assert_expected_database,
    read_current_database,
)
from scheduler_backend.retry_policy import (
    DEFAULT_EXHAUSTED_RETRY_SECONDS,
    DEFAULT_FAILURE_DELAYS,
    DEFAULT_OVERDUE_TOLERANCE_SECONDS,
    DEFAULT_RETRY_MAX_ATTEMPTS,
    DEFAULT_TARGET_LOCK_COOLDOWNS,
    DEFAULT_TARGET_LOCK_MAX_WAIT_SECONDS,
    parse_bounded_int,
    parse_bounded_seconds,
    parse_delay_list,
)

logger = logging.getLogger(__name__)

_IDENTIFIER_RE_MSG = "Database identifier values must be simple SQL identifiers."


def _load_env() -> None:
    """Load scheduler config from .env.realtime only.

    Do not fall back to .env: that file is owned by partitionui (mode 600) and
    causes Permission denied when the scheduler runs as enterprisedb.
    """
    load_dotenv(".env.realtime", override=False)


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _identifier(name: str, default: str) -> str:
    value = (os.getenv(name) or default).strip()
    if not value or not value.replace("_", "").isalnum() or value[0].isdigit():
        raise RuntimeError(_IDENTIFIER_RE_MSG + f" ({name})")
    return value


def parse_max_concurrent_jobs(raw: Optional[str]) -> int:
    """Parse PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS (default 1; reject invalid).

    Absent / unset → 1 (serial). Empty, 0, negative, float, or non-integer text
    raise RuntimeError — never silently coerced.
    """
    if raw is None:
        return 1
    text = str(raw).strip()
    if text == "":
        raise RuntimeError(
            "PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS must be a positive integer "
            "(got empty value)."
        )
    # Reject floats, signs, and non-digit text — do not coerce silently.
    if not text.isdigit():
        raise RuntimeError(
            "PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS must be a positive integer "
            "(got {!r}).".format(raw)
        )
    value = int(text)
    if value < 1:
        raise RuntimeError(
            "PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS must be >= 1 (got {}).".format(
                value
            )
        )
    return value


def load_scheduler_config() -> SchedulerConfig:
    _load_env()
    config = SchedulerConfig(
        lookahead_seconds=float(
            os.getenv("PARTITION_SCHEDULER_LOOKAHEAD_SECONDS", "120")
        ),
        reconcile_seconds=float(
            os.getenv("PARTITION_SCHEDULER_RECONCILE_SECONDS", "30")
        ),
        bind_host=os.getenv("PARTITION_SCHEDULER_BIND_HOST", "127.0.0.1").strip()
        or "127.0.0.1",
        bind_port=int(os.getenv("PARTITION_SCHEDULER_BIND_PORT", "8765")),
        lock_file=os.getenv(
            "PARTITION_SCHEDULER_LOCK_FILE",
            os.path.join(tempfile.gettempdir(), "partition-job-scheduler.lock"),
        ).strip()
        or os.path.join(tempfile.gettempdir(), "partition-job-scheduler.lock"),
        connect_retry_seconds=float(
            os.getenv("PARTITION_SCHEDULER_CONNECT_RETRY_SECONDS", "5")
        ),
        max_connect_retry_seconds=float(
            os.getenv("PARTITION_SCHEDULER_MAX_CONNECT_RETRY_SECONDS", "60")
        ),
        max_concurrent_jobs=parse_max_concurrent_jobs(
            os.getenv("PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS")
        ),
        schema=_identifier("PARTITION_JOB_SCHEMA", "mubasher_oms"),
        upcoming_function=_identifier(
            "PARTITION_UPCOMING_JOBS_FUNCTION", "get_upcoming_partition_jobs"
        ),
        scheduled_run_function=_identifier(
            "PARTITION_SCHEDULED_RUN_FUNCTION", "run_partition_job_scheduled"
        ),
        overdue_tolerance_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS"),
            DEFAULT_OVERDUE_TOLERANCE_SECONDS,
            0.0,
            600.0,
            "PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS",
        ),
        retry_max_attempts=parse_bounded_int(
            os.getenv("PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS"),
            DEFAULT_RETRY_MAX_ATTEMPTS,
            1,
            10,
            "PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS",
        ),
        retry_delays=parse_delay_list(
            os.getenv("PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS"),
            DEFAULT_FAILURE_DELAYS,
            "PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS",
        ),
        target_lock_cooldowns=parse_delay_list(
            os.getenv("PARTITION_SCHEDULER_TARGET_LOCK_COOLDOWN_SECONDS"),
            DEFAULT_TARGET_LOCK_COOLDOWNS,
            "PARTITION_SCHEDULER_TARGET_LOCK_COOLDOWN_SECONDS",
        ),
        target_lock_max_wait_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_TARGET_LOCK_MAX_WAIT_SECONDS"),
            DEFAULT_TARGET_LOCK_MAX_WAIT_SECONDS,
            30.0,
            6 * 3600.0,
            "PARTITION_SCHEDULER_TARGET_LOCK_MAX_WAIT_SECONDS",
        ),
        exhausted_retry_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_EXHAUSTED_RETRY_SECONDS"),
            DEFAULT_EXHAUSTED_RETRY_SECONDS,
            1.0,
            3600.0,
            "PARTITION_SCHEDULER_EXHAUSTED_RETRY_SECONDS",
        ),
        reconcile_function=_identifier(
            "PARTITION_RECONCILE_MISSED_FUNCTION", "reconcile_missed_partition_job"
        ),
        attention_table=_identifier(
            "PARTITION_JOB_ATTENTION_TABLE", "partition_job_attention"
        ),
        # Never configurable off in production: the scheduler must not run
        # against a database missing required execution functions.
        framework_check_enabled=True,
        framework_recheck_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_FRAMEWORK_RECHECK_SECONDS"),
            300.0,
            30.0,
            3600.0,
            "PARTITION_SCHEDULER_FRAMEWORK_RECHECK_SECONDS",
        ),
        # Phase 5D: remote targets.
        target_cooldown_base_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_TARGET_COOLDOWN_BASE_SECONDS"),
            15.0, 1.0, 300.0, "PARTITION_SCHEDULER_TARGET_COOLDOWN_BASE_SECONDS",
        ),
        target_cooldown_max_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_TARGET_COOLDOWN_MAX_SECONDS"),
            300.0, 15.0, 3600.0, "PARTITION_SCHEDULER_TARGET_COOLDOWN_MAX_SECONDS",
        ),
        target_preflight_ttl_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_TARGET_PREFLIGHT_TTL_SECONDS"),
            300.0, 10.0, 3600.0, "PARTITION_SCHEDULER_TARGET_PREFLIGHT_TTL_SECONDS",
        ),
        manual_slot_timeout_seconds=parse_bounded_seconds(
            os.getenv("PARTITION_SCHEDULER_MANUAL_SLOT_TIMEOUT_SECONDS"),
            60.0, 1.0, 600.0, "PARTITION_SCHEDULER_MANUAL_SLOT_TIMEOUT_SECONDS",
        ),
    )
    validate_ownership_window(config)
    return config


def validate_ownership_window(config: SchedulerConfig) -> None:
    """Fail closed if the scheduler could first see due rows only after they are due.

    Missed-run classification relies on the scheduler seeing each occurrence
    BEFORE it is due (lookahead) or within the overdue tolerance. If the
    lookahead were shorter than the reconcile interval, normal occurrences
    could be discovered late and wrongly treated as historical.
    """
    if float(config.reconcile_seconds) <= 0:
        raise RuntimeError("PARTITION_SCHEDULER_RECONCILE_SECONDS must be > 0.")
    if float(config.lookahead_seconds) < float(config.reconcile_seconds):
        raise RuntimeError(
            "PARTITION_SCHEDULER_LOOKAHEAD_SECONDS ({}) must be >= "
            "PARTITION_SCHEDULER_RECONCILE_SECONDS ({}) so every occurrence is "
            "discovered before it is due.".format(
                config.lookahead_seconds, config.reconcile_seconds
            )
        )


def _db_kwargs() -> dict[str, Any]:
    _load_env()
    creds = resolve_database_credentials()
    return {
        "host": _require("DB_HOST"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": _require("DB_NAME"),
        "user": creds["user"],
        "password": creds["password"],
        "sslmode": (os.getenv("DB_SSLMODE") or "prefer").strip() or "prefer",
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    }


def _connect_with_auth_retry() -> Connection:
    """Connect using resolved credentials; refresh AWS secret once on auth failure."""
    try:
        return connect(**_db_kwargs())
    except PsycopgError as exc:
        if not is_authentication_error(exc):
            raise
        logger.warning(
            "Scheduler DB authentication failed; invalidating credential cache "
            "and retrying once"
        )
        invalidate_database_secret_cache()
        return connect(**_db_kwargs())


@contextmanager
def open_connection(purpose: str) -> Generator[Connection, None, None]:
    """Open one short-lived connection, verify DB name, then always close.

    Database identity uses ``SELECT current_database()``. With psycopg's
    default ``autocommit=False`` that SELECT would otherwise leave an open
    transaction, causing later ``conn.transaction()`` to nest as a savepoint
    whose outer rollback discarded EXECUTED next_run_time updates. After the
    identity check we ``commit()`` so the connection is idle before yield.
    That commit ends only the read-only safety SELECT — no business DML yet.
    """
    logger.debug("Opening DB connection for %s", purpose)
    conn: Optional[Connection] = None
    try:
        try:
            conn = _connect_with_auth_retry()
        except DatabaseCredentialError:
            logger.exception(
                "event=database_connection_failed component=scheduler "
                "cause=credential_resolution purpose=%s",
                purpose,
            )
            # Credential failures are metered in db_credentials; do not double-count
            # as a generic DB connection failure.
            raise
        except PsycopgError:
            logger.exception(
                "event=database_connection_failed component=scheduler purpose=%s",
                purpose,
            )
            emit_db_connection_failure(component="scheduler")
            raise
        except OSError:
            logger.exception(
                "event=database_connection_failed component=scheduler purpose=%s",
                purpose,
            )
            emit_db_connection_failure(component="scheduler")
            raise
        assert_expected_database(read_current_database(conn))
        # End the implicit transaction from the identity SELECT.
        conn.commit()
        yield conn
        logger.debug("DB operation completed for %s", purpose)
    finally:
        if conn is not None and not conn.closed:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error while closing DB connection for %s", purpose)
            else:
                logger.debug("DB connection closed for %s", purpose)


def count_failed_jobs(config: SchedulerConfig) -> int:
    """Count enabled jobs whose latest ACTUAL execution failed (gauge).

    ``last_run_status`` keeps its established meaning — outcome of the latest
    *scheduled* execution (manual runs never change it). A
    failed scheduled run is therefore only counted while no LATER manual run
    has succeeded::

        SELECT COUNT(*)
        FROM {schema}.partitioning_job_table j
        WHERE j.is_enabled IS TRUE
          AND j.last_run_status = 'FAIL'
          AND latest manual execution after j.last_run_time is not MANUAL_SUCCESS

    Disabled rows, SUCCESS, and NULL ``last_run_status`` are excluded. One row
    per job — never a count of execution attempts. If the scheduler role
    cannot read the log table yet (Phase 5A grant not applied), falls back to
    the pre-5A rule rather than emitting nothing.
    """
    table = _identifier("PARTITION_JOB_TABLE", "partitioning_job_table")
    log_table = _identifier("PARTITION_JOB_LOG_TABLE", "partitioning_job_table_log")
    base = (
        "SELECT COUNT(*)::bigint AS failed_count "
        "FROM {schema}.{table} AS j "
        "WHERE j.is_enabled IS TRUE "
        "  AND j.last_run_status = 'FAIL'"
    ).format(schema=config.schema, table=table)
    recovered_filter = (
        " AND COALESCE(("
        "SELECT l.last_run_status FROM {schema}.{log_table} AS l "
        "WHERE l.job_id = j.job_id "
        "  AND l.last_run_status IN ('MANUAL_SUCCESS', 'MANUAL_FAIL') "
        "  AND j.last_run_time IS NOT NULL "
        "  AND l.job_runtime > j.last_run_time "
        "ORDER BY l.job_runtime DESC, l.job_log_id DESC LIMIT 1"
        "), 'MANUAL_FAIL') <> 'MANUAL_SUCCESS'"
    ).format(schema=config.schema, log_table=log_table)
    with open_connection("failed-job count") as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(base + recovered_filter)
                row = cur.fetchone()
        except (InsufficientPrivilege, UndefinedTable):
            conn.rollback()
            logger.warning(
                "event=failed_jobs_count_fallback reason=log_table_unreadable "
                "(apply Phase 5A grant; counting scheduled FAIL rows only)"
            )
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(base)
                row = cur.fetchone()
    if not row or row.get("failed_count") is None:
        return 0
    return int(row["failed_count"])


def _row_to_job(row: dict) -> ScheduledJob:
    job_id = int(row["job_id"])
    expected = row["expected_run_time"]
    if not isinstance(expected, datetime):
        raise RuntimeError(f"expected_run_time for job {job_id} is not a timestamp")
    target_id = row.get("database_target_id")
    return ScheduledJob(
        job_id=job_id,
        expected_run_time=expected,
        delay_seconds=float(row["delay_seconds"]),
        job_name=row.get("job_name"),
        is_create=row.get("is_create"),
        database_target_id=int(target_id) if target_id is not None else None,
        target_key=row.get("target_key"),
        is_control_target=row.get("is_control_target"),
    )


def fetch_upcoming_jobs(config: SchedulerConfig) -> list[ScheduledJob]:
    """
    Retrieve upcoming (and overdue) jobs of every enabled target of this hub,
    then close the connection. Job definitions live centrally in the CONTROL
    database: target databases are never queried for discovery.

    Uses DATABASE time for delay_seconds. Phase 5D: partops_get_upcoming_jobs
    (with database_target_id / target_key); falls back to the control-only
    get_upcoming_partition_jobs if the 5D migration is not applied (the
    framework gate then blocks dispatch anyway).
    """
    lookahead = timedelta(seconds=max(float(config.lookahead_seconds), 0.0))
    target_sql = (
        f"SELECT job_id, expected_run_time, delay_seconds, job_name, is_create, "
        f"database_target_id, target_key, is_control_target "
        f"FROM {config.schema}.{config.upcoming_targets_function}(%(lookahead)s)"
    )
    legacy_sql = (
        f"SELECT job_id, expected_run_time, delay_seconds, job_name, is_create "
        f"FROM {config.schema}.{config.upcoming_function}(%(lookahead)s)"
    )
    with open_connection("upcoming-job refresh") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(target_sql, {"lookahead": lookahead})
            except UndefinedFunction:
                conn.rollback()
                logger.warning("event=target_aware_discovery_unavailable "
                               "(apply 20261117_04; control-database jobs only)")
                cur.execute(legacy_sql, {"lookahead": lookahead})
            rows = cur.fetchall()
    return [_row_to_job(dict(row)) for row in rows]


# Timestamps returned from inside the business transaction are rendered as
# text by the server. A partition worker / db_config_para may change DateStyle
# for the transaction; psycopg's text timestamp loader rejects non-ISO styles
# ("unexpected DateStyle: Redwood, SHOW_TIME") AFTER the work ran, which used
# to roll back the occurrence and feed the retry loop. to_char() makes the
# result independent of the session DateStyle.
_TS_TEXT_FORMAT_SQL = "YYYY-MM-DD HH24:MI:SS.US"
_TS_TEXT_FORMAT_PY = "%Y-%m-%d %H:%M:%S.%f"


def _parse_db_timestamp(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, _TS_TEXT_FORMAT_PY)
    except ValueError:
        # Never turn an already-committed DB result into a Python failure.
        logger.warning("event=unparseable_db_timestamp value=%r", text[:40])
        return None


def _occurrence_sql(config: SchedulerConfig, function_name: str, args: str) -> str:
    return (
        f"SELECT r.status, r.job_id, r.message, "
        f"to_char(r.next_run_time, '{_TS_TEXT_FORMAT_SQL}') AS next_run_time, "
        f"r.is_create "
        f"FROM {config.schema}.{function_name}({args}) AS r"
    )


def _run_occurrence_function(
    purpose: str, job_id: int, sql: str, params: dict[str, Any]
) -> dict[str, Any]:
    """One short-lived connection, one top-level transaction, one result row.

    Any psycopg error means the transaction rolled back — reported as
    FAILED_DATABASE (never the cacheable SQL status FAILED).
    """
    with open_connection(purpose) as conn:
        try:
            # Top-level transaction: identity check already committed above.
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()
                    if not row:
                        # No SQL result row — do not claim a committed occurrence.
                        return {
                            "status": "FAILED_DATABASE",
                            "job_id": job_id,
                            "message": "Database function returned no row.",
                            "next_run_time": None,
                            "is_create": None,
                        }
                    result = dict(row)
            result["next_run_time"] = _parse_db_timestamp(result.get("next_run_time"))
            return result
        except PsycopgError as exc:
            # Transaction aborted / rolled back — occurrence NOT handled.
            logger.exception(
                "event=scheduled_db_error purpose=%r job_id=%s sqlstate=%s",
                purpose,
                job_id,
                getattr(exc, "sqlstate", None),
            )
            return {
                "status": "FAILED_DATABASE",
                "job_id": job_id,
                "message": "Database error; transaction did not complete.",
                "next_run_time": None,
                "is_create": None,
            }


def execute_scheduled_job(
    config: SchedulerConfig, job_id: int, expected_run_time: datetime
) -> dict[str, Any]:
    """
    Open a NEW connection, execute one scheduled occurrence, close immediately.

    Returns the first result row from run_partition_job_scheduled(...). Called
    only for OWNED occurrences; the DB still revalidates everything and, as a
    safety ceiling, never executes an occurrence later than
    partition_job_max_execution_lateness() (returns MISSED_* instead).
    """
    sql = _occurrence_sql(
        config,
        config.scheduled_run_function,
        "%(job_id)s, %(expected_run_time)s",
    )
    return _run_occurrence_function(
        f"scheduled job {job_id}",
        job_id,
        sql,
        {"job_id": job_id, "expected_run_time": expected_run_time},
    )


RECONCILE_REASONS = frozenset({"MISSED_SCHEDULED_RUN", "RETRY_EXHAUSTED"})


def reconcile_occurrence(
    config: SchedulerConfig,
    job_id: int,
    expected_run_time: datetime,
    reason: str,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    """Ask the database to reconcile an occurrence WITHOUT executing it.

    Calls reconcile_missed_partition_job(job_id, expected, reason, detail):
    revalidates under the job lock, records a PENDING_REVIEW attention item and
    advances next_run_time to the first future occurrence. Never runs
    CREATE/DROP. ``reason``: MISSED_SCHEDULED_RUN (historical occurrence) or
    RETRY_EXHAUSTED (owned occurrence whose retry budget is used up).
    """
    if reason not in RECONCILE_REASONS:
        raise ValueError("Unsupported reconcile reason: {!r}".format(reason))
    sql = _occurrence_sql(
        config,
        config.reconcile_function,
        "%(job_id)s, %(expected_run_time)s, %(reason)s, %(detail)s",
    )
    return _run_occurrence_function(
        f"reconcile {reason.lower()} job {job_id}",
        job_id,
        sql,
        {
            "job_id": job_id,
            "expected_run_time": expected_run_time,
            "reason": reason,
            "detail": (detail or "")[:500] or None,
        },
    )


def reconcile_exhausted_occurrence(
    config: SchedulerConfig,
    job_id: int,
    expected_run_time: datetime,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    """Backwards-compatible wrapper: RETRY_EXHAUSTED reconciliation."""
    return reconcile_occurrence(
        config, job_id, expected_run_time, "RETRY_EXHAUSTED", detail
    )


def check_framework_compatibility(config: SchedulerConfig) -> Any:
    """READ-ONLY framework gate for the scheduler (Phase 5B).

    Compares the connected database with sql/framework/framework_manifest.json
    for everything the scheduler needs: required execution functions with the
    expected signatures, EXECUTE for the connected role, attention/log tables,
    PUBLIC/definer hardening and the framework version. Returns a
    FrameworkReport; ``report.ok`` False means: do not dispatch.
    """
    from framework_manifest import fetch_catalog_snapshot, load_manifest, scheduler_gate

    manifest = load_manifest()
    with open_connection("framework compatibility check") as conn:
        snapshot = fetch_catalog_snapshot(conn, manifest)
    return scheduler_gate(manifest, snapshot)


def fetch_control_target(config: SchedulerConfig) -> Any:
    """The control database's own target row (Phase 5C), read over the CONTROL
    connection. Fails closed if missing, duplicated, disabled or naming another
    database than the one we are connected to."""
    from database_targets import (
        TargetConfigError,
        control_target,
        fetch_target_rows,
        load_targets_from_rows,
    )

    with open_connection("control target lookup") as conn:
        rows = fetch_target_rows(conn, config.schema)
        actual = read_current_database(conn)
        conn.rollback()
    target = control_target(load_targets_from_rows(rows))
    if target is None:
        raise TargetConfigError("exactly one control-database target must be registered")
    if target.database_name != actual or not target.is_enabled:
        raise TargetConfigError(
            "control target {!r} does not match connected database {!r} or is disabled".format(
                target.target_key, actual))
    return target


def count_pending_attention(config: SchedulerConfig) -> Optional[int]:
    """COUNT of attention items in PENDING_REVIEW, or None if the table is absent."""
    sql = (
        "SELECT COUNT(*)::bigint AS pending_count "
        "FROM {schema}.{table} AS a "
        "WHERE a.status = 'PENDING_REVIEW'"
    ).format(schema=config.schema, table=config.attention_table)
    with open_connection("pending-attention count") as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                row = cur.fetchone()
        except UndefinedTable:
            logger.warning(
                "event=attention_table_missing (apply Phase 5A migrations)"
            )
            return None
    if not row or row.get("pending_count") is None:
        return 0
    return int(row["pending_count"])


# ---------------------------------------------------------------------------
# Phase 5D — remote execution: CONTROL-database ledger operations.
# Each call = one short control transaction (never held during target DDL).
# ---------------------------------------------------------------------------
def fetch_targets(config: SchedulerConfig) -> dict:
    """Registry of this hub's targets (valid rows only), read over CONTROL."""
    from database_targets import DatabaseTarget, TargetConfigError, fetch_target_rows

    with open_connection("target registry") as conn:
        rows = fetch_target_rows(conn, config.schema)
        conn.rollback()
    targets = {}
    for row in rows:
        try:
            target = DatabaseTarget.from_row(row)
        except (TargetConfigError, KeyError, TypeError, ValueError):
            logger.error("event=target_registry_row_invalid target_id=%s (skipped)", row.get("target_id"))
            continue
        targets[target.target_id] = target
    return targets


def _control_call(purpose: str, sql: str, params: dict, many: bool = False) -> Any:
    """Run one control-database function in its own committed transaction."""
    with open_connection(purpose) as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()] if cur.description is not None else []
    if many:
        return rows
    return rows[0] if rows else {}


def remote_claim(
    config: SchedulerConfig,
    job_id: int,
    expected_run_time: Optional[datetime],
    kind: str,
    executor: str,
    attention_id: Optional[int] = None,
    confirm_drop: bool = False,
) -> dict:
    """partops_remote_claim: revalidate + create/reuse the attempt (no DDL)."""
    sql = (f"SELECT * FROM {config.schema}.partops_remote_claim("
           "%(job_id)s, %(expected)s, %(kind)s, %(executor)s, %(attention_id)s, %(confirm_drop)s)")
    return _control_call(f"remote claim job {job_id}", sql, {
        "job_id": job_id, "expected": expected_run_time, "kind": kind, "executor": executor,
        "attention_id": attention_id, "confirm_drop": bool(confirm_drop)})


def remote_mark_dispatching(config: SchedulerConfig, attempt_id: int, executor: str) -> str:
    row = _control_call("remote mark dispatching",
                        f"SELECT {config.schema}.partops_remote_mark_dispatching(%(a)s, %(e)s) AS state",
                        {"a": attempt_id, "e": executor})
    return str(row.get("state") or "")


def remote_record_success(config: SchedulerConfig, attempt_id: int, duration_ms: Optional[int]) -> str:
    row = _control_call("remote record success",
                        f"SELECT {config.schema}.partops_remote_record_success(%(a)s, %(d)s) AS state",
                        {"a": attempt_id, "d": duration_ms})
    return str(row.get("state") or "")


def remote_record_failure(
    config: SchedulerConfig, attempt_id: int, error_class: str,
    summary: Optional[str], duration_ms: Optional[int],
) -> dict:
    return _control_call("remote record failure",
                         f"SELECT * FROM {config.schema}.partops_remote_record_failure(%(a)s, %(c)s, %(s)s, %(d)s)",
                         {"a": attempt_id, "c": error_class, "s": (summary or "")[:500] or None,
                          "d": duration_ms})


def remote_mark_unknown(config: SchedulerConfig, attempt_id: int, summary: Optional[str]) -> dict:
    return _control_call("remote mark unknown",
                         f"SELECT * FROM {config.schema}.partops_remote_mark_unknown(%(a)s, %(s)s)",
                         {"a": attempt_id, "s": (summary or "")[:500] or None})


def remote_finalize(config: SchedulerConfig, attempt_id: int) -> dict:
    return _control_call("remote finalize",
                         f"SELECT * FROM {config.schema}.partops_remote_finalize(%(a)s)",
                         {"a": attempt_id})


def remote_recover_attempts(config: SchedulerConfig, executor: str) -> list:
    return _control_call("remote attempt recovery",
                         f"SELECT * FROM {config.schema}.partops_remote_recover_attempts(%(e)s)",
                         {"e": executor}, many=True)


def count_unknown_outcomes(config: SchedulerConfig) -> Optional[int]:
    """Unresolved OUTCOME_UNKNOWN attempts (None if the ledger is absent)."""
    sql = (f"SELECT COUNT(*)::bigint AS n FROM {config.schema}.partops_execution_attempt "
           "WHERE state = 'OUTCOME_UNKNOWN'")
    with open_connection("unknown-outcome count") as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                row = cur.fetchone()
        except UndefinedTable:
            return None
    return int((row or {}).get("n") or 0)
