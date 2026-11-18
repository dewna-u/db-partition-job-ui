"""Database access for partition job creation and pgAgent job listing."""

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator, Optional

from dotenv import load_dotenv
from psycopg import Connection, Error as PsycopgError
from psycopg.errors import UndefinedColumn, UndefinedFunction, UndefinedTable
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from db_credentials import (
    DatabaseCredentialError,
    invalidate_database_secret_cache,
    is_authentication_error,
    resolve_database_credentials,
)
from observability import emit_db_connection_failure
from job_autofill import (
    INVOCATION_CALL,
    PROKIND_FUNCTION,
    PROKIND_PROCEDURE,
    CalledRoutine,
    PartitionOperationEvidence,
    build_db_config_json,
    calculate_next_run,
    convert_pgagent_schedule_to_cron,
    extract_called_functions,
    extract_db_config_from_function,
    extract_partition_settings,
    extract_single_called_routine,
    extract_single_target_table,
    extract_target_table_references,
    infer_frequency,
    infer_is_create,
    infer_partition_operation_from_function,
)

load_dotenv()

logger = logging.getLogger(__name__)

APPLICATION_NAME = "partition-job-ui"

# Authoritative partition-job objects. Overridable by environment only (never by
# user input) so a deployment with different object names does not need a code
# change. Both values are validated as plain PostgreSQL identifiers before use.
DEFAULT_PARTITION_JOB_SCHEMA = "mubasher_oms"
DEFAULT_PARTITION_JOB_FUNCTION = "insert_data_to_partition_job_table"
DEFAULT_PARTITION_JOB_UPDATE_FUNCTION = "update_data_to_partition_job_table"
DEFAULT_PARTITION_JOB_TABLE = "partitioning_job_table"
DEFAULT_PARTITION_JOB_LOG_TABLE = "partitioning_job_table_log"
DEFAULT_PARTITION_JOB_SEQUENCE = "seq_partitioning_job_id"
DEFAULT_MANUAL_RUN_FUNCTION = "run_partition_job_manual"
DEFAULT_PARTITION_JOB_ATTENTION_TABLE = "partition_job_attention"
DEFAULT_ATTENTION_RECOVERY_FUNCTION = "run_partition_job_attention_recovery"
DEFAULT_ATTENTION_ACKNOWLEDGE_FUNCTION = "acknowledge_partition_job_attention"

# Attention item lifecycle (Phase 5A; mirrors the table CHECK constraint).
ATTENTION_STATUS_PENDING = "PENDING_REVIEW"
ATTENTION_STATUSES = frozenset(
    {ATTENTION_STATUS_PENDING, "MANUALLY_RERUN", "ACKNOWLEDGED"}
)

# The two generic scanners that replace per-table pgAgent jobs. The application
# only ever *detects* these; it never creates pgAgent jobs.
GENERIC_CREATE_SCANNER = "run_partition_create_jobs"
GENERIC_DROP_SCANNER = "run_partition_drop_jobs"

# Identity argument list of the insert function, used for the readiness check.
INSERT_FUNCTION_ARGUMENT_TYPES = (
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

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Fixed SQL — never built from user input via f-string / format / concatenation.
_PGAGENT_EXISTS_SQL = "SELECT to_regclass('pgagent.pga_job') IS NOT NULL;"

_PGAGENT_JOBS_SQL = """
SELECT
    jobid AS job_id,
    jobname AS job_name,
    jobenabled AS enabled,
    jobhostagent AS host_agent,
    jobnextrun AS next_run,
    joblastrun AS last_run,
    jobdesc AS description
FROM pgagent.pga_job
ORDER BY jobid;
"""

# Named notation (=>) is used deliberately: it is independent of parameter order,
# so a signature difference fails loudly as undefined_function instead of binding
# the wrong value to the wrong column. Only the schema/function identifiers are
# interpolated; every value is a bound parameter.
_CREATE_PARTITION_JOB_SQL_TEMPLATE = """
SELECT {schema}.{function}(
    p_job_name                 => %(job_name)s,
    p_is_enabled               => %(is_enabled)s,
    p_table_schema             => %(table_schema)s,
    p_table_name               => %(table_name)s,
    p_db_config_para           => %(db_config)s,
    p_job_schedule             => %(job_schedule)s,
    p_frequency                => %(frequency)s::interval,
    p_next_run_time            => %(next_run_time)s,
    p_partition_unit           => %(partition_unit)s,
    p_partition_period         => %(partition_period)s,
    p_is_create                => %(is_create)s,
    p_is_create_drop_interval  => %(create_drop_interval)s::interval
) AS result;
"""

_UPDATE_PARTITION_JOB_SQL_TEMPLATE = """
SELECT {schema}.{function}(
    p_job_id                   => %(job_id)s,
    p_job_name                 => %(job_name)s,
    p_is_enabled               => %(is_enabled)s,
    p_table_schema             => %(table_schema)s,
    p_table_name               => %(table_name)s,
    p_db_config_para           => %(db_config)s,
    p_job_schedule             => %(job_schedule)s,
    p_frequency                => %(frequency)s::interval,
    p_next_run_time            => %(next_run_time)s,
    p_partition_unit           => %(partition_unit)s,
    p_partition_period         => %(partition_period)s,
    p_is_create                => %(is_create)s,
    p_is_create_drop_interval  => %(create_drop_interval)s::interval
) AS result;
"""

# Read-only view of the parameterised configurations. Each row IS a partition job.
_PARTITION_JOBS_SQL_TEMPLATE = """
SELECT
    j.job_id,
    j.job_name,
    j.is_enabled,
    j.table_schema,
    j.table_name,
    j.db_config_para,
    j.frequency,
    j.last_run_time,
    j.next_run_time,
    j.last_run_status,
    j.partition_unit,
    j.partition_period,
    j.is_create,
    j.create_drop_interval,
    j.job_schedule,
    -- Phase 5C: works before and after migration 20261117_01.
    (to_jsonb(j) ->> 'database_target_id')::integer AS database_target_id,
    (
        SELECT l.execution_duration_ms
          FROM {schema}.{log_table} AS l
         WHERE l.job_id = j.job_id
         ORDER BY l.job_runtime DESC NULLS LAST, l.job_log_id DESC
         LIMIT 1
    ) AS last_execution_duration_ms,
    (
        SELECT AVG(l.execution_duration_ms)::bigint
          FROM {schema}.{log_table} AS l
         WHERE l.job_id = j.job_id
           AND l.execution_duration_ms IS NOT NULL
    ) AS avg_execution_duration_ms
FROM {schema}.{table} AS j
ORDER BY j.job_id DESC;
"""

_PARTITION_JOBS_SQL_TEMPLATE_LEGACY = """
SELECT
    job_id,
    job_name,
    is_enabled,
    table_schema,
    table_name,
    db_config_para,
    frequency,
    last_run_time,
    next_run_time,
    last_run_status,
    partition_unit,
    partition_period,
    is_create,
    create_drop_interval,
    job_schedule,
    (to_jsonb(j) ->> 'database_target_id')::integer AS database_target_id
FROM {schema}.{table} AS j
ORDER BY job_id DESC;
"""

_PARTITION_JOB_BY_ID_SQL_TEMPLATE = """
SELECT
    j.job_id,
    j.job_name,
    j.is_enabled,
    j.table_schema,
    j.table_name,
    j.db_config_para,
    j.frequency,
    j.last_run_time,
    j.next_run_time,
    j.last_run_status,
    j.partition_unit,
    j.partition_period,
    j.is_create,
    j.create_drop_interval,
    j.job_schedule,
    -- Phase 5C: works before and after migration 20261117_01.
    (to_jsonb(j) ->> 'database_target_id')::integer AS database_target_id,
    (
        SELECT l.execution_duration_ms
          FROM {schema}.{log_table} AS l
         WHERE l.job_id = j.job_id
         ORDER BY l.job_runtime DESC NULLS LAST, l.job_log_id DESC
         LIMIT 1
    ) AS last_execution_duration_ms,
    (
        SELECT AVG(l.execution_duration_ms)::bigint
          FROM {schema}.{log_table} AS l
         WHERE l.job_id = j.job_id
           AND l.execution_duration_ms IS NOT NULL
    ) AS avg_execution_duration_ms
FROM {schema}.{table} AS j
WHERE j.job_id = %(job_id)s;
"""

_PARTITION_JOB_BY_ID_SQL_TEMPLATE_LEGACY = """
SELECT
    job_id,
    job_name,
    is_enabled,
    table_schema,
    table_name,
    db_config_para,
    frequency,
    last_run_time,
    next_run_time,
    last_run_status,
    partition_unit,
    partition_period,
    is_create,
    create_drop_interval,
    job_schedule,
    (to_jsonb(j) ->> 'database_target_id')::integer AS database_target_id
FROM {schema}.{table} AS j
WHERE job_id = %(job_id)s;
"""

_PARTITION_JOB_LOGS_SQL_TEMPLATE = """
SELECT
    job_log_id,
    job_id,
    job_name,
    last_run_status,
    job_runtime,
    job_error,
    execution_duration_ms,
    (to_jsonb(l) ->> 'database_target_id')::integer AS database_target_id,
    (to_jsonb(l) ->> 'execution_attempt_id')::bigint AS execution_attempt_id
FROM (
    -- Phase 5F.1: to_jsonb() only for the rows returned, not every row sorted.
    SELECT * FROM {schema}.{table}
    ORDER BY job_runtime DESC, job_log_id DESC
    LIMIT %(row_limit)s
) AS l
ORDER BY job_runtime DESC, job_log_id DESC;
"""

_PARTITION_JOB_LOGS_SQL_TEMPLATE_LEGACY = """
SELECT
    job_log_id,
    job_id,
    job_name,
    last_run_status,
    job_runtime,
    job_error
FROM {schema}.{table}
ORDER BY job_runtime DESC
LIMIT %(row_limit)s;
"""

# Manual execution of an already-configured row. The database function decides
# between create/drop and writes history; next_run_time is intentionally left
# untouched so a manual retry never disturbs the automatic schedule.
_RUN_PARTITION_JOB_MANUAL_SQL_TEMPLATE = """
SELECT status, message, execution_duration_ms
  FROM {schema}.{function}(%(job_id)s);
"""

# Missed-run attention items (Phase 5A) joined with the CURRENT job row so the
# UI can show the already-advanced normal schedule next to the missed time.
_ATTENTION_LIST_SQL_TEMPLATE = """
SELECT
    a.attention_id,
    a.job_id,
    a.job_name,
    a.table_schema,
    a.table_name,
    a.is_create,
    a.reason,
    a.missed_run_time,
    a.detected_at,
    a.rescheduled_to,
    a.detail,
    a.status,
    a.reviewed_at,
    a.resolution,
    a.manual_execution_result,
    a.manual_execution_message,
    a.manual_execution_duration_ms,
    a.manual_executed_at,
    (to_jsonb(a) ->> 'database_target_id')::integer AS database_target_id,
    (to_jsonb(a) ->> 'execution_attempt_id')::bigint AS execution_attempt_id,
    j.next_run_time AS current_next_run_time,
    j.is_enabled    AS job_is_enabled
FROM {schema}.{attention_table} AS a
LEFT JOIN {schema}.{table} AS j ON j.job_id = a.job_id
WHERE (%(status)s::text IS NULL OR a.status = %(status)s::text)
  AND (%(attention_id)s::bigint IS NULL OR a.attention_id = %(attention_id)s::bigint)
  AND (%(job_id)s::numeric IS NULL OR a.job_id = %(job_id)s::numeric)
ORDER BY a.detected_at DESC, a.attention_id DESC
LIMIT %(row_limit)s;
"""

_ATTENTION_RECOVERY_SQL_TEMPLATE = """
SELECT result_status, result_message, result_duration_ms,
       attention_status, attention_job_id
  FROM {schema}.{function}(%(attention_id)s);
"""

_ATTENTION_ACKNOWLEDGE_SQL_TEMPLATE = """
SELECT attention_status, attention_job_id, acknowledged_at
  FROM {schema}.{function}(%(attention_id)s, %(note)s);
"""

# Read-only privilege introspection. to_reg* keeps the check safe when an object
# is absent, so a missing object reports as "missing" instead of raising.
_DATABASE_READINESS_SQL = """
SELECT
    current_user AS db_user,
    current_database() AS db_name,
    to_regnamespace(%(schema_name)s) IS NOT NULL AS schema_exists,
    CASE WHEN to_regnamespace(%(schema_name)s) IS NOT NULL
         THEN has_schema_privilege(current_user, %(schema_name)s, 'USAGE')
    END AS schema_usage,
    to_regprocedure(%(insert_function)s) IS NOT NULL AS insert_function_exists,
    CASE WHEN to_regprocedure(%(insert_function)s) IS NOT NULL
         THEN has_function_privilege(current_user, %(insert_function)s, 'EXECUTE')
    END AS insert_function_execute,
    to_regclass(%(config_table)s) IS NOT NULL AS config_table_exists,
    CASE WHEN to_regclass(%(config_table)s) IS NOT NULL
         THEN has_table_privilege(current_user, %(config_table)s, 'INSERT')
    END AS config_table_insert,
    CASE WHEN to_regclass(%(config_table)s) IS NOT NULL
         THEN has_table_privilege(current_user, %(config_table)s, 'SELECT')
    END AS config_table_select,
    to_regclass(%(log_table)s) IS NOT NULL AS log_table_exists,
    CASE WHEN to_regclass(%(log_table)s) IS NOT NULL
         THEN has_table_privilege(current_user, %(log_table)s, 'SELECT')
    END AS log_table_select,
    to_regclass(%(sequence_name)s) IS NOT NULL AS sequence_exists,
    CASE WHEN to_regclass(%(sequence_name)s) IS NOT NULL
         THEN has_sequence_privilege(current_user, %(sequence_name)s, 'USAGE')
    END AS sequence_usage;
"""

# Every enabled SQL step, used to detect the two generic scanner jobs.
_PGAGENT_ALL_STEPS_SQL = """
SELECT
    j.jobid AS job_id,
    j.jobname AS job_name,
    j.jobenabled AS enabled,
    s.jstid AS step_id,
    s.jstenabled AS step_enabled,
    s.jstcode AS code
FROM pgagent.pga_job j
JOIN pgagent.pga_jobstep s ON s.jstjobid = j.jobid
WHERE s.jstkind = 's'
ORDER BY j.jobid, s.jstid;
"""

_PGAGENT_DETAIL_OBJECTS_SQL = """
SELECT
    to_regclass('pgagent.pga_job') IS NOT NULL
    AND to_regclass('pgagent.pga_jobstep') IS NOT NULL
    AND to_regclass('pgagent.pga_schedule') IS NOT NULL;
"""

_PGAGENT_JOB_BY_ID_SQL = """
SELECT
    jobid AS job_id,
    jobname AS job_name,
    jobenabled AS enabled,
    jobhostagent AS host_agent,
    jobnextrun AS next_run,
    jobdesc AS description
FROM pgagent.pga_job
WHERE jobid = %(job_id)s;
"""

_PGAGENT_STEPS_SQL = """
SELECT
    jstid AS step_id,
    jstname AS step_name,
    jstenabled AS enabled,
    jstkind AS kind,
    jstcode AS code,
    jstdbname AS dbname
FROM pgagent.pga_jobstep
WHERE jstjobid = %(job_id)s
ORDER BY jstid;
"""

_PGAGENT_SCHEDULES_SQL = """
SELECT
    jscid AS schedule_id,
    jscname AS schedule_name,
    jscenabled AS enabled,
    jscstart AS start_time,
    jscend AS end_time,
    jscminutes AS minutes,
    jschours AS hours,
    jscweekdays AS weekdays,
    jscmonthdays AS monthdays,
    jscmonths AS months
FROM pgagent.pga_schedule
WHERE jscjobid = %(job_id)s
ORDER BY jscid;
"""

# Schemas that can never hold an application partition routine or table.
SYSTEM_SCHEMAS = ["pg_catalog", "information_schema", "pg_toast"]

# Routine discovery. Read-only and fully parameterised: the routine name and the
# optional schema are bound values, never interpolated. A pgAgent step may call a
# FUNCTION or a PROCEDURE, qualified or unqualified, so resolution is done
# against the catalog rather than assumed from the step text. The definition is
# selected here as well, so one query yields both the identity and the body.
_ROUTINE_CANDIDATES_SQL = """
SELECT
    p.oid::bigint                            AS routine_oid,
    n.nspname                                AS routine_schema,
    p.proname                                AS routine_name,
    p.prokind::text                          AS routine_kind,
    p.pronargs                               AS input_argument_count,
    p.pronargdefaults                        AS default_argument_count,
    pg_get_function_identity_arguments(p.oid) AS identity_arguments,
    pg_get_functiondef(p.oid)                AS routine_definition
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proname = %(routine_name)s
  AND p.prokind::text = ANY (%(kinds)s::text[])
  AND n.nspname <> ALL (%(system_schemas)s::text[])
  AND (
        %(routine_schema)s::text IS NULL
        OR n.nspname = %(routine_schema)s::text
      )
ORDER BY n.nspname, p.oid;
"""

# Resolve an unqualified relation name to its real schema. Ordinary and
# partitioned tables only; system and temporary schemas are excluded.
_TABLE_CANDIDATES_SQL = """
SELECT
    n.nspname AS table_schema,
    c.relname AS table_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = %(table_name)s
  AND c.relkind::text = ANY (ARRAY['r', 'p'])
  AND c.relpersistence::text <> 't'
  AND n.nspname <> ALL (%(system_schemas)s::text[])
ORDER BY n.nspname;
"""


class DatabaseError(Exception):
    """Safe, user-facing database error."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class PgAgentNotInstalledError(DatabaseError):
    """Raised when pgagent.pga_job is not present."""


class DatabaseConflictError(DatabaseError):
    """The request conflicts with current database state (HTTP 409).

    Examples: target table busy (MANUAL_TARGET_LOCKED), attention item already
    reviewed or being processed concurrently.
    """


class DatabaseNotFoundError(DatabaseError):
    """The referenced row does not exist (HTTP 404)."""


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise DatabaseError(
            f"Missing required environment variable: {name}. "
            "Check the application .env file."
        )
    return value


def _optional_env(name: str) -> str:
    return os.getenv(name, "").strip()


def _configured_identifier(env_name: str, default: str) -> str:
    """Return an environment-configured PostgreSQL identifier, strictly validated."""
    value = _optional_env(env_name) or default
    if not _IDENTIFIER_RE.match(value):
        raise DatabaseError(
            f"Invalid database configuration. {env_name} must be a plain "
            "PostgreSQL identifier (letters, digits, and underscores only)."
        )
    return value


def partition_job_function_identity() -> tuple[str, str]:
    """Return the (schema, function) the application calls to store a configuration."""
    return (
        _configured_identifier(
            "PARTITION_JOB_SCHEMA", DEFAULT_PARTITION_JOB_SCHEMA
        ),
        _configured_identifier(
            "PARTITION_JOB_FUNCTION", DEFAULT_PARTITION_JOB_FUNCTION
        ),
    )


def partition_job_update_function_identity() -> tuple[str, str]:
    """Return the (schema, function) used to update an existing configuration."""
    return (
        _configured_identifier(
            "PARTITION_JOB_SCHEMA", DEFAULT_PARTITION_JOB_SCHEMA
        ),
        _configured_identifier(
            "PARTITION_JOB_UPDATE_FUNCTION", DEFAULT_PARTITION_JOB_UPDATE_FUNCTION
        ),
    )


def partition_job_schema() -> str:
    return _configured_identifier(
        "PARTITION_JOB_SCHEMA", DEFAULT_PARTITION_JOB_SCHEMA
    )


def partition_job_table_name() -> str:
    return _configured_identifier(
        "PARTITION_JOB_TABLE", DEFAULT_PARTITION_JOB_TABLE
    )


def partition_job_log_table_name() -> str:
    return _configured_identifier(
        "PARTITION_JOB_LOG_TABLE", DEFAULT_PARTITION_JOB_LOG_TABLE
    )


def partition_job_sequence_name() -> str:
    return _configured_identifier(
        "PARTITION_JOB_SEQUENCE", DEFAULT_PARTITION_JOB_SEQUENCE
    )


def manual_run_function_name() -> str:
    return _configured_identifier(
        "PARTITION_JOB_MANUAL_FUNCTION", DEFAULT_MANUAL_RUN_FUNCTION
    )


def build_create_partition_job_sql() -> str:
    """Build the parameterised function call. Identifiers come from env, not user input."""
    schema, function_name = partition_job_function_identity()
    return _CREATE_PARTITION_JOB_SQL_TEMPLATE.format(
        schema=schema, function=function_name
    )


def build_update_partition_job_sql() -> str:
    schema, function_name = partition_job_update_function_identity()
    return _UPDATE_PARTITION_JOB_SQL_TEMPLATE.format(
        schema=schema, function=function_name
    )


def build_partition_jobs_sql(*, include_duration: bool = True) -> str:
    template = (
        _PARTITION_JOBS_SQL_TEMPLATE
        if include_duration
        else _PARTITION_JOBS_SQL_TEMPLATE_LEGACY
    )
    return template.format(
        schema=partition_job_schema(),
        table=partition_job_table_name(),
        log_table=partition_job_log_table_name(),
    )


def build_partition_job_by_id_sql(*, include_duration: bool = True) -> str:
    template = (
        _PARTITION_JOB_BY_ID_SQL_TEMPLATE
        if include_duration
        else _PARTITION_JOB_BY_ID_SQL_TEMPLATE_LEGACY
    )
    return template.format(
        schema=partition_job_schema(),
        table=partition_job_table_name(),
        log_table=partition_job_log_table_name(),
    )


def build_partition_job_logs_sql(*, include_duration: bool = True) -> str:
    template = (
        _PARTITION_JOB_LOGS_SQL_TEMPLATE
        if include_duration
        else _PARTITION_JOB_LOGS_SQL_TEMPLATE_LEGACY
    )
    return template.format(
        schema=partition_job_schema(), table=partition_job_log_table_name()
    )


def build_run_partition_job_manual_sql() -> str:
    return _RUN_PARTITION_JOB_MANUAL_SQL_TEMPLATE.format(
        schema=partition_job_schema(), function=manual_run_function_name()
    )


def partition_job_attention_table_name() -> str:
    return _configured_identifier(
        "PARTITION_JOB_ATTENTION_TABLE", DEFAULT_PARTITION_JOB_ATTENTION_TABLE
    )


def build_attention_list_sql() -> str:
    return _ATTENTION_LIST_SQL_TEMPLATE.format(
        schema=partition_job_schema(),
        attention_table=partition_job_attention_table_name(),
        table=partition_job_table_name(),
    )


def build_attention_recovery_sql() -> str:
    return _ATTENTION_RECOVERY_SQL_TEMPLATE.format(
        schema=partition_job_schema(),
        function=_configured_identifier(
            "PARTITION_ATTENTION_RECOVERY_FUNCTION",
            DEFAULT_ATTENTION_RECOVERY_FUNCTION,
        ),
    )


def build_attention_acknowledge_sql() -> str:
    return _ATTENTION_ACKNOWLEDGE_SQL_TEMPLATE.format(
        schema=partition_job_schema(),
        function=_configured_identifier(
            "PARTITION_ATTENTION_ACKNOWLEDGE_FUNCTION",
            DEFAULT_ATTENTION_ACKNOWLEDGE_FUNCTION,
        ),
    )


def _main_db_kwargs() -> dict[str, Any]:
    """Connection kwargs for the main database (partition function)."""
    try:
        port = int(os.getenv("DB_PORT", "5444").strip() or "5444")
        connect_timeout = int(
            os.getenv("DB_CONNECT_TIMEOUT", "5").strip() or "5"
        )
    except ValueError as exc:
        raise DatabaseError(
            "Invalid database configuration. DB_PORT and DB_CONNECT_TIMEOUT must be integers."
        ) from exc

    try:
        creds = resolve_database_credentials()
    except DatabaseCredentialError as exc:
        raise DatabaseError(str(exc)) from None

    return {
        "host": os.getenv("DB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        "port": port,
        "dbname": _require_env("DB_NAME"),
        "user": creds["user"],
        "password": creds["password"],
        "sslmode": os.getenv("DB_SSLMODE", "prefer").strip() or "prefer",
        "connect_timeout": connect_timeout,
        "application_name": APPLICATION_NAME,
    }


def _pgagent_db_kwargs() -> dict[str, Any]:
    """
    Connection kwargs for the pgAgent database.

    Blank optional PGAGENT_DB_* values fall back to the main database config.
    """
    main = _main_db_kwargs()

    host = _optional_env("PGAGENT_DB_HOST") or main["host"]
    dbname = _optional_env("PGAGENT_DB_NAME") or main["dbname"]
    user = _optional_env("PGAGENT_DB_USER") or main["user"]
    password = _optional_env("PGAGENT_DB_PASSWORD") or main["password"]
    sslmode = _optional_env("PGAGENT_DB_SSLMODE") or main["sslmode"]

    port_raw = _optional_env("PGAGENT_DB_PORT")
    if port_raw:
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise DatabaseError(
                "Invalid database configuration. PGAGENT_DB_PORT must be an integer."
            ) from exc
    else:
        port = main["port"]

    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "sslmode": sslmode,
        "connect_timeout": main["connect_timeout"],
        "application_name": APPLICATION_NAME,
    }


_PERMISSION_OBJECT_RE = re.compile(
    r"permission denied for (\w+) ([A-Za-z0-9_.]+)", re.IGNORECASE
)


def _safe_diagnostics(exc: PsycopgError) -> dict[str, str]:
    """
    Collect non-secret PostgreSQL diagnostics for server-side logging.

    Only structured diagnostic fields are read — never connection parameters,
    passwords, DSNs, or bound parameter values.
    """
    diag = getattr(exc, "diag", None)
    fields = (
        "sqlstate",
        "severity",
        "message_primary",
        "message_detail",
        "message_hint",
        "schema_name",
        "table_name",
        "column_name",
        "constraint_name",
        "datatype_name",
        "context",
    )
    collected: dict[str, str] = {}
    for field in fields:
        value = getattr(diag, field, None) if diag is not None else None
        if value:
            collected[field] = str(value)
    if "sqlstate" not in collected:
        sqlstate = getattr(exc, "sqlstate", None)
        if sqlstate:
            collected["sqlstate"] = str(sqlstate)
    return collected


def _permission_detail(diagnostics: dict[str, str]) -> str:
    """Return a short, non-secret description of which object was denied."""
    match = _PERMISSION_OBJECT_RE.search(diagnostics.get("message_primary", ""))
    if match:
        return f" PostgreSQL reported: permission denied for {match.group(1)} {match.group(2)}."
    return ""


def _map_psycopg_error(exc: PsycopgError) -> DatabaseError:
    """
    Map a psycopg exception to a safe user-facing DatabaseError.

    Classification is driven by SQLSTATE, which is authoritative. Message-text
    matching is only a last resort for failures that carry no SQLSTATE (typically
    connection-level errors), so unrelated failures are never reported as
    permission problems.
    """
    diagnostics = _safe_diagnostics(exc)
    sqlstate = diagnostics.get("sqlstate", "")
    logger.exception(
        "Database operation failed (%s)",
        ", ".join(f"{key}={value}" for key, value in diagnostics.items())
        or "no diagnostics available",
    )

    # 55006 object_in_use — Phase 5D job configuration guard (remote attempt
    # in flight or unresolved). The message is ours and safe to show.
    if sqlstate == "55006":
        primary = getattr(getattr(exc, "diag", None), "message_primary", None)
        text = (primary or "").strip().split("\n")[0][:400]
        return DatabaseConflictError(
            text or "The job has a remote execution in progress or unresolved; try again later."
        )
    # 42501 insufficient_privilege — the only genuine permission classification.
    if sqlstate == "42501":
        return DatabaseError(
            "The application database user does not have the required permission."
            + _permission_detail(diagnostics)
        )
    # 42883 undefined_function — missing function or a parameter name/type mismatch.
    if sqlstate == "42883":
        schema, function_name = DEFAULT_PARTITION_JOB_SCHEMA, DEFAULT_PARTITION_JOB_FUNCTION
        try:
            schema, function_name = partition_job_function_identity()
        except DatabaseError:
            pass
        return DatabaseError(
            f"The partition-job function {schema}.{function_name} was not found, or its "
            "parameter names and types do not match what the application sends. "
            "Check the deployed function signature."
        )
    # 42809 wrong_object_type — for example a procedure invoked with SELECT.
    if sqlstate == "42809":
        return DatabaseError(
            "The partition-job database object exists but is not a callable function "
            "of the expected kind. Check the deployed object type."
        )
    if sqlstate == "42P01":
        return DatabaseError(
            "A table required by the partition-job function was not found."
        )
    if sqlstate in ("3F000", "42704"):
        return DatabaseError(
            "A schema or object required by the partition-job function was not found."
        )
    # Parameter / type adaptation problems, including malformed JSON and intervals.
    if sqlstate in (
        "22P02",
        "22007",
        "22008",
        "22023",
        "22032",
        "42804",
        "42P13",
        "42601",
    ):
        return DatabaseError(
            "One or more submitted values were rejected by the database as an "
            "invalid type or format. Review the schedule, frequency, interval, "
            "and Database Configuration JSON values."
        )
    if sqlstate == "23505":
        return DatabaseError(
            "A partition job with the same unique value may already exist."
        )
    if sqlstate == "23502":
        return DatabaseError(
            "A required partition-job value was missing (NOT NULL constraint)."
        )
    if sqlstate == "23503":
        return DatabaseError(
            "A submitted value does not reference an existing related record "
            "(foreign key constraint)."
        )
    if sqlstate in ("23514", "23P01"):
        return DatabaseError(
            "A submitted value was rejected by a database constraint."
        )
    if sqlstate in ("28000", "28P01"):
        return DatabaseError(
            "Database authentication failed. Check the application database configuration."
        )
    if sqlstate == "53300":
        return DatabaseError(
            "The database refused the connection because too many clients are connected."
        )
    if sqlstate.startswith("25"):
        return DatabaseError(
            "The database transaction failed and was rolled back. No configuration was stored."
        )
    if sqlstate.startswith("08") or sqlstate in ("57P01", "57P02", "57P03"):
        return DatabaseError(
            "Unable to connect to the database. Check the database configuration and server logs."
        )
    if sqlstate.startswith("P0"):
        return DatabaseError(
            "The partition-job function raised an error. Check the server logs for details."
        )

    if not sqlstate:
        # No SQLSTATE: almost always a client/connection level failure.
        msg = str(exc).lower()
        if (
            "could not connect" in msg
            or "connection refused" in msg
            or "timeout" in msg
            or "could not translate host name" in msg
        ):
            return DatabaseError(
                "Unable to connect to the database. Check the database configuration and server logs."
            )
        return DatabaseError(
            "The database operation failed. Check the server logs for details."
        )

    return DatabaseError(
        "The database operation failed "
        f"(SQLSTATE {sqlstate}). Check the server logs for details."
    )


def _connect_psycopg_with_auth_retry(kwargs_factory: Any) -> Connection:
    """Connect once; on auth failure invalidate AWS secret cache and retry once."""
    from psycopg import connect

    kwargs = kwargs_factory() if callable(kwargs_factory) else kwargs_factory
    try:
        return connect(**kwargs)
    except PsycopgError as exc:
        if not is_authentication_error(exc):
            raise
        logger.warning(
            "Database authentication failed; invalidating credential cache and retrying once"
        )
        invalidate_database_secret_cache()
        if callable(kwargs_factory):
            kwargs = kwargs_factory()
        else:
            raise
        return connect(**kwargs)


@contextmanager
def _connection(kwargs_factory: Any) -> Generator[Connection, None, None]:
    """Open a short-lived connection and always close it.

    ``kwargs_factory`` may be a dict (tests/legacy) or a zero-arg callable that
    returns connection kwargs. Callables enable a single secret-cache refresh
    retry after authentication failures.
    """
    conn: Optional[Connection] = None
    try:
        try:
            conn = _connect_psycopg_with_auth_retry(kwargs_factory)
        except PsycopgError as exc:
            logger.warning("event=database_connection_failed component=api")
            emit_db_connection_failure(component="api")
            raise _map_psycopg_error(exc) from None
        except DatabaseCredentialError as exc:
            # Credential failures are metered in db_credentials.
            raise DatabaseError(str(exc)) from None
        except OSError:
            logger.exception("event=database_connection_failed component=api")
            emit_db_connection_failure(component="api")
            raise DatabaseError(
                "Unable to connect to the database. Check the database configuration and server logs."
            ) from None
        try:
            yield conn
        except PsycopgError as exc:
            # Query/runtime errors are not connection failures.
            raise _map_psycopg_error(exc) from None
    finally:
        if conn is not None and not conn.closed:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — best-effort close
                logger.exception("Error while closing database connection")


def pgagent_table_exists() -> bool:
    """Return True if pgagent.pga_job exists in the configured pgAgent database."""
    with _connection(_pgagent_db_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(_PGAGENT_EXISTS_SQL)
            row = cur.fetchone()
            return bool(row and row[0])


def get_pgagent_jobs() -> list[dict]:
    """
    Return all pgAgent jobs as a list of dictionaries.

    Raises PgAgentNotInstalledError if pgagent.pga_job is missing.
    Opens a short-lived connection and closes it afterward.
    """
    with _connection(_pgagent_db_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(_PGAGENT_EXISTS_SQL)
            exists_row = cur.fetchone()
            if not exists_row or not exists_row[0]:
                raise PgAgentNotInstalledError(
                    "pgAgent is not installed in the configured pgAgent database, "
                    "or its objects are stored in another database."
                )

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_PGAGENT_JOBS_SQL)
            rows = cur.fetchall()
            return [dict(row) for row in rows]


def create_partition_job(data: dict, database_target_id: Optional[int] = None) -> Any:
    """
    Call the partition-job insert function and return its result.

    The function is the only write path — the UI never inserts into the partition
    job table directly. Runs in a single transaction: commit once on success,
    roll back on failure. Always closes the connection.
    """
    params = {
        "job_name": data["job_name"],
        "is_enabled": bool(data["is_enabled"]),
        "table_schema": data["table_schema"],
        "table_name": data["table_name"],
        # Adapted to jsonb by psycopg — never pre-encoded, so it cannot double-encode.
        "db_config": Jsonb(data["db_config"]),
        "job_schedule": data["job_schedule"],
        "frequency": data["frequency"],
        "next_run_time": data["next_run_time"],
        "partition_unit": data["partition_unit"],
        "partition_period": int(data["partition_period"]),
        "is_create": bool(data["is_create"]),
        "create_drop_interval": data["create_drop_interval"],
    }

    statement = build_create_partition_job_sql()
    if database_target_id is None:
        database_target_id = data.get("database_target_id")

    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    if database_target_id is not None:
                        # Phase 5D: the legacy insert function is unaware of
                        # targets; the BEFORE INSERT hook validates and applies
                        # this transaction-local request. Refuse if the hook is
                        # missing (the job would silently land on the control DB).
                        cur.execute(
                            "SELECT to_regprocedure(%(fn)s) IS NOT NULL",
                            {"fn": f"{partition_job_schema()}.fn_partops_job_target_on_insert()"},
                        )
                        if not cur.fetchone()[0]:
                            raise DatabaseConflictError(
                                "Creating jobs on another database target requires "
                                "migration 20261117_03."
                            )
                        cur.execute(
                            "SELECT set_config('partops.requested_target_id', %(t)s, true)",
                            {"t": str(int(database_target_id))},
                        )
                    cur.execute(statement, params)
                    # A void-returning function still produces one row holding
                    # NULL; a statement with no result set must not raise here.
                    result = None
                    if cur.description is not None:
                        row = cur.fetchone()
                        if row:
                            result = row[0]
            return result
        except DatabaseError:
            raise
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def _summarise_step(step: dict, routine: Optional[CalledRoutine] = None) -> dict:
    summary = {
        "step_id": step.get("step_id"),
        "step_name": step.get("step_name"),
        "enabled": bool(step.get("enabled")),
        "kind": step.get("kind"),
        "dbname": step.get("dbname"),
        "routine_schema": None,
        "routine_name": None,
        "invocation": None,
        "routine_label": None,
    }
    if routine is not None:
        summary["routine_schema"] = routine.schema
        summary["routine_name"] = routine.name
        summary["invocation"] = routine.invocation
        summary["routine_label"] = routine.display_name
    return summary


def _select_sql_steps(steps: list[dict]) -> list[dict]:
    selected = []
    for step in steps:
        kind = str(step.get("kind") or "").lower()
        if kind != "s":
            continue
        if not step.get("enabled"):
            continue
        selected.append(step)
    return selected


def _resolve_called_routine(
    step: dict,
) -> tuple[Optional[CalledRoutine], list[str]]:
    warnings: list[str] = []
    routine, warning = extract_single_called_routine(step.get("code") or "")
    if warning:
        warnings.append(warning)
    return routine, warnings


def _routine_signature(candidate: dict) -> str:
    kind = (
        "PROCEDURE"
        if candidate.get("routine_kind") == PROKIND_PROCEDURE
        else "FUNCTION"
    )
    arguments = candidate.get("identity_arguments") or ""
    return (
        f"{kind} {candidate.get('routine_schema')}."
        f"{candidate.get('routine_name')}({arguments})"
    )


def _argument_count_fits(candidate: dict, supplied: Optional[int]) -> bool:
    """
    Whether a supplied argument count is callable against this routine.

    PostgreSQL allows trailing arguments with defaults to be omitted, so the
    callable range is (pronargs - pronargdefaults) .. pronargs.
    """
    if supplied is None:
        return True
    total = int(candidate.get("input_argument_count") or 0)
    defaults = int(candidate.get("default_argument_count") or 0)
    return (total - defaults) <= supplied <= total


def _routine_candidates(
    cur: Any, routine: CalledRoutine
) -> tuple[list[dict], Optional[str]]:
    """
    Fetch catalog candidates for a parsed call, preferring the implied kind.

    CALL implies a procedure and SELECT a function. When the preferred kind finds
    nothing, both kinds are tried so a legacy routine registered as the other
    kind is still importable — with a warning rather than in silence.
    """
    params: dict[str, Any] = {
        "routine_name": routine.name,
        "routine_schema": routine.schema,
        "kinds": [routine.expected_prokind],
        "system_schemas": SYSTEM_SCHEMAS,
    }
    cur.execute(_ROUTINE_CANDIDATES_SQL, params)
    rows = [dict(row) for row in cur.fetchall()]
    if rows:
        return rows, None

    params["kinds"] = [PROKIND_FUNCTION, PROKIND_PROCEDURE]
    cur.execute(_ROUTINE_CANDIDATES_SQL, params)
    rows = [dict(row) for row in cur.fetchall()]
    if not rows:
        return [], None

    expected = "procedure" if routine.invocation == INVOCATION_CALL else "function"
    return rows, (
        f"The job step uses {routine.invocation}, which implies a {expected}, but "
        f"{routine.display_name} is registered as the other routine kind in "
        "PostgreSQL. It was inspected anyway."
    )


def _choose_routine_candidate(
    candidates: list[dict], supplied: Optional[int]
) -> tuple[Optional[dict], Optional[str]]:
    """Pick one candidate by argument count, or refuse when still ambiguous."""
    fitting = [
        candidate
        for candidate in candidates
        if _argument_count_fits(candidate, supplied)
    ]

    if len(fitting) == 1:
        return fitting[0], None

    if not fitting:
        if len(candidates) == 1:
            return candidates[0], (
                f"The job step supplies {supplied} argument(s), which does not match "
                f"{_routine_signature(candidates[0])}. That routine was inspected "
                "because it is the only one with this name. Verify the imported values."
            )
        signatures = "; ".join(
            _routine_signature(candidate) for candidate in candidates
        )
        return None, (
            f"No PostgreSQL routine with this name accepts {supplied} argument(s) "
            f"({signatures}). Manual review is required and routine-derived fields "
            "were left unchanged."
        )

    exact = [
        candidate
        for candidate in fitting
        if int(candidate.get("input_argument_count") or 0) == supplied
    ]
    if len(exact) == 1:
        return exact[0], None

    signatures = "; ".join(_routine_signature(candidate) for candidate in fitting)
    return None, (
        "Multiple PostgreSQL routines match this call, so manual review is required "
        f"({signatures}). Routine-derived fields were left unchanged."
    )


def _resolve_unqualified_table(
    cur: Any, names: list[str], routine_schema: Optional[str]
) -> tuple[Optional[tuple[str, str]], Optional[str]]:
    """
    Resolve an unqualified relation name from the routine body to a real schema.

    The routine's own schema is used only to break a tie between identically
    named tables — never to assume a schema that has no matching table.
    """
    if not names:
        return None, None
    if len(names) > 1:
        return None, (
            "The routine definition references several unqualified tables "
            f"({', '.join(names)}), so the target table was not resolved."
        )

    name = names[0]
    cur.execute(
        _TABLE_CANDIDATES_SQL,
        {"table_name": name, "system_schemas": SYSTEM_SCHEMAS},
    )
    rows = [dict(row) for row in cur.fetchall()]

    if not rows:
        return None, (
            f"The routine references the table {name}, but no table with that name "
            "was found in the configured database. Set Table Schema and Table Name "
            "manually."
        )
    if len(rows) == 1:
        return (rows[0]["table_schema"], rows[0]["table_name"]), None

    in_routine_schema = [
        row
        for row in rows
        if str(row.get("table_schema") or "").lower()
        == str(routine_schema or "").lower()
    ]
    if len(in_routine_schema) == 1:
        return (
            in_routine_schema[0]["table_schema"],
            in_routine_schema[0]["table_name"],
        ), None

    schemas = ", ".join(str(row.get("table_schema")) for row in rows)
    return None, (
        f"A table named {name} exists in more than one schema ({schemas}) and the "
        "routine does not qualify it. Set Table Schema manually."
    )


def _inspect_called_routine(
    routine: CalledRoutine,
) -> tuple[
    dict[str, Any], list[str], Optional[PartitionOperationEvidence], Optional[dict]
]:
    """
    Resolve the called routine in the catalog and derive auto-fill values from it.

    Strictly read-only: the routine is never executed and neither is the job step
    code. One catalog query yields the routine identity and its definition, and
    every downstream analysis — operation, target table, database configuration,
    partition settings — reads that same definition, so they can never disagree.

    Returns (autofill, warnings, operation_evidence, resolved_routine). The
    evidence is None when the definition could not be inspected at all (routine
    missing, ambiguous overload, or a failed catalog read), which callers must
    treat differently from an inspected-but-inconclusive body.
    """
    autofill: dict[str, Any] = {}
    warnings: list[str] = []
    operation: Optional[PartitionOperationEvidence] = None
    resolved: Optional[dict] = None

    try:
        with _connection(_main_db_kwargs) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                candidates, kind_warning = _routine_candidates(cur, routine)
                if kind_warning:
                    warnings.append(kind_warning)

                if not candidates:
                    warnings.append(
                        f"Could not safely resolve the PostgreSQL routine called by "
                        f"this pgAgent step ({routine.display_name}). No matching "
                        "function or procedure was found in the configured database, "
                        "so Table Schema, Table Name, Partition Unit, Partition "
                        "Period, Database Configuration and Create/Drop Interval "
                        "were left unchanged."
                    )
                    return autofill, warnings, None, None

                chosen, choice_warning = _choose_routine_candidate(
                    candidates, routine.argument_count
                )
                if choice_warning:
                    warnings.append(choice_warning)
                if chosen is None:
                    return autofill, warnings, None, None

                definition = chosen.get("routine_definition") or ""
                resolved = {
                    "schema": chosen.get("routine_schema"),
                    "name": chosen.get("routine_name"),
                    "kind": (
                        "PROCEDURE"
                        if chosen.get("routine_kind") == PROKIND_PROCEDURE
                        else "FUNCTION"
                    ),
                    "identity_arguments": chosen.get("identity_arguments") or "",
                    "invocation": routine.invocation,
                    "supplied_argument_count": routine.argument_count,
                    "signature": _routine_signature(chosen),
                }

                # Prefer a schema-qualified reference in the body; fall back to
                # resolving an unqualified one through the catalog.
                table_pair, table_warning = extract_single_target_table(definition)
                if table_pair is None:
                    _qualified, unqualified = extract_target_table_references(
                        definition
                    )
                    table_pair, resolve_warning = _resolve_unqualified_table(
                        cur, unqualified, chosen.get("routine_schema")
                    )
                    if table_pair is None:
                        message = resolve_warning or table_warning
                        if message:
                            warnings.append(message)
                if table_pair:
                    autofill["table_schema"] = table_pair[0]
                    autofill["table_name"] = table_pair[1]
    except DatabaseError as exc:
        warnings.append(exc.message)
        return autofill, warnings, None, None
    except PsycopgError as exc:
        warnings.append(_map_psycopg_error(exc).message)
        return autofill, warnings, None, None

    # The executable body is the authoritative source for CREATE vs DROP.
    operation = infer_partition_operation_from_function(definition)
    if operation.is_create is not None:
        autofill["is_create"] = operation.is_create
    logger.info(
        "Partition operation inferred for %s: %s — %s",
        resolved["signature"],
        operation.operation,
        operation.reason,
    )

    # Database Configuration comes from the function's own SET / set_config
    # statements. Reuses the definition already fetched above — no second query.
    db_config, config_warnings = extract_db_config_from_function(definition)
    warnings.extend(config_warnings)
    autofill["db_config"] = build_db_config_json(db_config)
    if not db_config:
        warnings.append(
            "The called routine sets no session configuration, so Database "
            "Configuration was left empty."
        )

    settings, setting_warnings = extract_partition_settings(definition)
    warnings.extend(setting_warnings)
    autofill.update(settings)
    return autofill, warnings, operation, resolved


def get_pgagent_job_details(
    job_id: int, step_id: Optional[int] = None
) -> dict:
    """
    Load one pgAgent job and derive safe auto-fill values.

    Read-only. Does not execute jstcode or the discovered function.
    Does not modify pgAgent jobs or insert partition configuration.
    """
    try:
        job_id_int = int(job_id)
    except (TypeError, ValueError) as exc:
        raise DatabaseError("pgAgent Job ID must be a whole number.") from exc
    if job_id_int < 1:
        raise DatabaseError("pgAgent Job ID must be greater than zero.")

    params = {"job_id": job_id_int}
    warnings: list[str] = []
    autofill: dict[str, Any] = {}

    with _connection(_pgagent_db_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(_PGAGENT_DETAIL_OBJECTS_SQL)
            exists_row = cur.fetchone()
            if not exists_row or not exists_row[0]:
                raise PgAgentNotInstalledError(
                    "pgAgent is not installed in the configured pgAgent database, "
                    "or its objects are stored in another database."
                )

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_PGAGENT_JOB_BY_ID_SQL, params)
            job_row = cur.fetchone()
            if not job_row:
                raise DatabaseError(
                    "No pgAgent job was found for the specified Job ID."
                )
            job = dict(job_row)

            cur.execute(_PGAGENT_STEPS_SQL, params)
            steps = [dict(row) for row in cur.fetchall()]

            cur.execute(_PGAGENT_SCHEDULES_SQL, params)
            schedules = [dict(row) for row in cur.fetchall()]

    job_name = job.get("job_name") or ""
    autofill["job_name"] = job_name
    autofill["is_enabled"] = bool(job.get("enabled"))

    next_run = job.get("next_run")

    # Weak fallback only, and never applied while the function body is readable.
    name_based_is_create = infer_is_create(job_name)

    enabled_schedules = [row for row in schedules if row.get("enabled")]
    schedule_info: dict[str, Any] = {
        "enabled_count": len(enabled_schedules),
        "total_count": len(schedules),
        "cron": None,
        "start_time": None,
        "end_time": None,
    }
    if len(enabled_schedules) == 0:
        warnings.append(
            "No enabled pgAgent schedule was found. Job Schedule was left unchanged."
        )
    elif len(enabled_schedules) > 1:
        warnings.append(
            "Multiple enabled pgAgent schedules were found. "
            "Job Schedule was left unchanged."
        )
    else:
        chosen_schedule = enabled_schedules[0]
        schedule_info["start_time"] = chosen_schedule.get("start_time")
        schedule_info["end_time"] = chosen_schedule.get("end_time")
        cron, schedule_warnings = convert_pgagent_schedule_to_cron(
            chosen_schedule.get("minutes"),
            chosen_schedule.get("hours"),
            chosen_schedule.get("monthdays"),
            chosen_schedule.get("months"),
            chosen_schedule.get("weekdays"),
        )
        warnings.extend(schedule_warnings)
        if cron:
            autofill["job_schedule"] = cron
            schedule_info["cron"] = cron
            derived_next_run = calculate_next_run(
                cron,
                start_time=chosen_schedule.get("start_time"),
                end_time=chosen_schedule.get("end_time"),
            )
            if derived_next_run is not None:
                next_run = derived_next_run
                autofill["next_run_time"] = derived_next_run
            frequency, frequency_warning = infer_frequency(cron)
            if frequency:
                autofill["frequency_amount"] = frequency[0]
                autofill["frequency_unit"] = frequency[1]
            elif frequency_warning:
                warnings.append(frequency_warning)

    if "next_run_time" not in autofill:
        if next_run is not None:
            autofill["next_run_time"] = next_run
        else:
            warnings.append(
                "Next Run Time could not be derived from the pgAgent schedule and was left unchanged."
            )

    sql_steps = _select_sql_steps(steps)
    step_summaries = []
    step_choices: list[dict[str, Any]] = []
    selected_step: Optional[dict] = None

    parsed_steps: list[tuple[dict, Optional[CalledRoutine], list[str]]] = []
    for step in sql_steps:
        routine, step_warnings = _resolve_called_routine(step)
        parsed_steps.append((step, routine, step_warnings))
        step_summaries.append(_summarise_step(step, routine))

    selected_routine: Optional[CalledRoutine] = None
    if step_id is not None:
        try:
            step_id_int = int(step_id)
        except (TypeError, ValueError) as exc:
            raise DatabaseError("Job step ID must be a whole number.") from exc
        for step, routine, step_warnings in parsed_steps:
            if step.get("step_id") == step_id_int:
                selected_step = step
                selected_routine = routine
                warnings.extend(step_warnings)
                break
        if selected_step is None:
            warnings.append(
                "The selected job step was not found or is not an enabled SQL step. "
                "Step-derived fields were left unchanged."
            )
    elif len(parsed_steps) == 0:
        warnings.append(
            "No enabled SQL job step was found. Database name, Table Schema, "
            "Table Name, and routine-derived fields were left unchanged."
        )
    elif len(parsed_steps) == 1:
        selected_step, selected_routine, step_warnings = parsed_steps[0]
        warnings.extend(step_warnings)
    else:
        routine_keys = {
            routine.display_name.lower()
            for _step, routine, _warn in parsed_steps
            if routine is not None
        }
        if len(routine_keys) == 1 and all(
            routine is not None for _step, routine, _warn in parsed_steps
        ):
            selected_step, selected_routine, step_warnings = parsed_steps[0]
            warnings.extend(step_warnings)
            warnings.append(
                "Multiple enabled SQL steps were found; they appear to call the same "
                "routine, so the first step was used."
            )
        else:
            warnings.append(
                "Multiple enabled SQL steps call different routines. "
                "Select a job step to continue auto-fill. "
                "Step-derived fields were left unchanged."
            )
            for step, routine, _step_warnings in parsed_steps:
                step_choices.append(_summarise_step(step, routine))

    database_name = None
    operation_evidence: Optional[PartitionOperationEvidence] = None
    resolved_routine: Optional[dict] = None
    if selected_step is not None:
        database_name = selected_step.get("dbname")

        if selected_routine is not None:
            (
                routine_autofill,
                routine_warnings,
                operation_evidence,
                resolved_routine,
            ) = _inspect_called_routine(selected_routine)
            autofill.update(routine_autofill)
            warnings.extend(routine_warnings)

    if "is_create" not in autofill:
        if operation_evidence is not None:
            # The body was readable but inconclusive. A name must not override
            # or substitute for that, so the user decides.
            warnings.append(
                "Could not safely determine CREATE/DROP from the partition routine. "
                + operation_evidence.reason
                + " Please review the operation manually."
            )
        else:
            fallback = None
            fallback_source = ""
            if selected_routine is not None:
                fallback = infer_is_create(selected_routine.name)
                fallback_source = "called routine name"
            if fallback is None:
                fallback = name_based_is_create
                fallback_source = "job name"
            if fallback is None:
                warnings.append(
                    "Could not safely determine CREATE/DROP: the partition routine "
                    "definition could not be inspected and the names are not "
                    "conclusive. Please review the operation manually."
                )
            else:
                autofill["is_create"] = fallback
                warnings.append(
                    "The partition routine definition could not be inspected, so the "
                    f"operation was taken from the {fallback_source}. A name is only "
                    "metadata — verify CREATE/DROP before saving."
                )

    public_steps = []
    for step in steps:
        step_routine, _warn = extract_single_called_routine(step.get("code") or "")
        public_steps.append(_summarise_step(step, step_routine))

    return {
        "job_id": job.get("job_id"),
        "job_name": job_name,
        "enabled": bool(job.get("enabled")),
        "next_run": next_run,
        "description": job.get("description"),
        "host_agent": job.get("host_agent"),
        "job_steps": public_steps,
        "database_name": database_name,
        "schedule": schedule_info,
        "warnings": warnings,
        "autofill": autofill,
        "step_choices": step_choices,
        "called_routine": resolved_routine,
    }


def _validate_job_id(job_id: Any) -> int:
    try:
        value = int(job_id)
    except (TypeError, ValueError) as exc:
        raise DatabaseError("Partition Job ID must be a whole number.") from exc
    if value < 1:
        raise DatabaseError("Partition Job ID must be greater than zero.")
    return value


def _query_jobs(conn: Connection) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        try:
            cur.execute(build_partition_jobs_sql(include_duration=True))
        except UndefinedColumn:
            conn.rollback()
            cur.execute(build_partition_jobs_sql(include_duration=False))
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row.setdefault("last_execution_duration_ms", None)
        row.setdefault("avg_execution_duration_ms", None)
    return rows


def get_partition_jobs() -> list[dict]:
    """Return every parameterised configuration row. Read-only."""
    with _connection(_main_db_kwargs) as conn:
        try:
            return _query_jobs(conn)
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def get_partition_job(job_id: Any) -> Optional[dict]:
    """Return one parameterised configuration row, or None. Read-only."""
    job_id_int = _validate_job_id(job_id)
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                try:
                    cur.execute(
                        build_partition_job_by_id_sql(include_duration=True),
                        {"job_id": job_id_int},
                    )
                except UndefinedColumn:
                    conn.rollback()
                    cur.execute(
                        build_partition_job_by_id_sql(include_duration=False),
                        {"job_id": job_id_int},
                    )
                row = cur.fetchone()
                if not row:
                    return None
                result = dict(row)
                result.setdefault("last_execution_duration_ms", None)
                result.setdefault("avg_execution_duration_ms", None)
                return result
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def get_partition_job_logs(limit: int = 100) -> list[dict]:
    """Return the most recent execution history rows. Read-only."""
    try:
        row_limit = int(limit)
    except (TypeError, ValueError):
        row_limit = 100
    row_limit = max(1, min(row_limit, 1000))

    with _connection(_main_db_kwargs) as conn:
        try:
            return _query_recent_logs(conn, row_limit)
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def _query_recent_logs(conn: Connection, row_limit: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        try:
            cur.execute(build_partition_job_logs_sql(include_duration=True), {"row_limit": row_limit})
        except UndefinedColumn:
            conn.rollback()
            cur.execute(build_partition_job_logs_sql(include_duration=False), {"row_limit": row_limit})
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row.setdefault("execution_duration_ms", None)
    return rows


def update_partition_job(job_id: Any, data: dict, new_target_id: Optional[int] = None) -> Any:
    """
    Update an existing partition-job configuration via the approved DB function.

    Never writes last_run_time / last_run_status / job_id. History is untouched.
    """
    job_id_int = _validate_job_id(job_id)
    params = {
        "job_id": job_id_int,
        "job_name": data["job_name"],
        "is_enabled": bool(data["is_enabled"]),
        "table_schema": data["table_schema"],
        "table_name": data["table_name"],
        "db_config": Jsonb(data["db_config"]),
        "job_schedule": data["job_schedule"],
        "frequency": data["frequency"],
        "next_run_time": data["next_run_time"],
        "partition_unit": data["partition_unit"],
        "partition_period": int(data["partition_period"]),
        "is_create": bool(data["is_create"]),
        "create_drop_interval": data["create_drop_interval"],
    }
    statement = build_update_partition_job_sql()

    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    if new_target_id is not None:
                        # Phase 5D: guarded move (refused while a remote attempt
                        # is open); committed together with the edit.
                        cur.execute(
                            f"SELECT {partition_job_schema()}.partops_set_job_target(%(j)s, %(t)s)",
                            {"j": job_id_int, "t": int(new_target_id)},
                        )
                    cur.execute(statement, params)
                    result = None
                    if cur.description is not None:
                        row = cur.fetchone()
                        if row:
                            result = row[0]
            return result
        except DatabaseError:
            raise
        except PsycopgError as exc:
            # P0002 / no_data_found style from the update function → not found.
            sqlstate = getattr(exc, "sqlstate", None) or ""
            if sqlstate == "55006":
                raise _map_psycopg_error(exc) from None
            if sqlstate in ("P0002", "02000") or "not found" in str(exc).lower():
                raise DatabaseError(f"Job {job_id_int} not found.") from None
            # 22023 invalid_parameter_value — validation / fail-closed schedule.
            if sqlstate == "22023":
                primary = getattr(getattr(exc, "diag", None), "message_primary", None)
                text = (primary or str(exc) or "").strip().split("\n")[0][:400]
                if not text or "password" in text.lower():
                    text = (
                        "The job update was rejected (invalid configuration or "
                        "schedule). Previous values were left unchanged."
                    )
                raise DatabaseError(text) from None
            raise _map_psycopg_error(exc) from None


_MANUAL_FAIL_STATUSES = frozenset({"MANUAL_FAIL", "FAIL", "FAILED"})
# Target table busy (scheduled or another manual run holds the 87421032 lock).
# Nothing executed and nothing logged by the database.
MANUAL_TARGET_LOCKED = "MANUAL_TARGET_LOCKED"
# Phase 5C: job belongs to another managed target; nothing executed here.
MANUAL_REMOTE_TARGET = "MANUAL_REMOTE_TARGET"


def run_partition_job_manual(job_id: Any) -> Any:
    """
    Execute an already-configured partition job immediately.

    Delegates entirely to the database function, which chooses create or drop and
    writes MANUAL_SUCCESS / MANUAL_FAIL with execution_duration_ms on the same
    log INSERT. The function returns that outcome instead of RAISEing on worker
    failure so this connection can COMMIT the failure log first.

    next_run_time is deliberately never modified here — scheduling stays under
    database control so a manual retry does not disturb the automatic schedule.
    """
    job_id_int = _validate_job_id(job_id)
    statement = build_run_partition_job_manual_sql()
    result: Optional[dict] = None
    failure_message: Optional[str] = None
    conflict_message: Optional[str] = None

    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(statement, {"job_id": job_id_int})
                    row = cur.fetchone() if cur.description is not None else None
        except DatabaseError:
            raise
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None

        status = ""
        message = None
        duration_ms = None
        if row:
            status = str(row[0] or "").strip()
            if len(row) > 1:
                message = row[1]
            if len(row) > 2:
                duration_ms = row[2]
            result = {
                "status": status,
                "message": message,
                "execution_duration_ms": duration_ms,
            }
            if status.upper() == MANUAL_REMOTE_TARGET:
                conflict_message = (
                    str(message).strip()
                    if message
                    else "The job belongs to another database target; nothing was executed."
                )
            elif status.upper() == MANUAL_TARGET_LOCKED:
                conflict_message = (
                    str(message).strip()
                    if message
                    else "The target table is busy; nothing was executed."
                )
            elif status.upper() in _MANUAL_FAIL_STATUSES:
                failure_message = (
                    str(message).strip()
                    if message
                    else "Manual partition job execution failed."
                )

    if conflict_message:
        raise DatabaseConflictError(conflict_message)
    if failure_message:
        raise DatabaseError(failure_message)

    return result


def get_generic_partition_schedulers() -> dict[str, Any]:
    """
    Detect the two generic pgAgent scanner jobs. Strictly read-only.

    These are the only pgAgent jobs the new architecture needs: they poll the
    configuration table. The application never creates or modifies pgAgent jobs.
    """
    schema = partition_job_schema()
    wanted = {
        GENERIC_CREATE_SCANNER: "create_scheduler",
        GENERIC_DROP_SCANNER: "drop_scheduler",
    }
    found: dict[str, Any] = {"create_scheduler": None, "drop_scheduler": None}

    with _connection(_pgagent_db_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(_PGAGENT_EXISTS_SQL)
            exists_row = cur.fetchone()
            if not exists_row or not exists_row[0]:
                raise PgAgentNotInstalledError(
                    "pgAgent is not installed in the configured pgAgent database, "
                    "or its objects are stored in another database."
                )

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_PGAGENT_ALL_STEPS_SQL)
            steps = [dict(row) for row in cur.fetchall()]

            matched_job_ids: dict[str, int] = {}
            for step in steps:
                for called_schema, called_function in extract_called_functions(
                    step.get("code") or ""
                ):
                    if called_schema.lower() != schema.lower():
                        continue
                    key = wanted.get(called_function.lower())
                    if key is None or found[key] is not None:
                        continue
                    found[key] = {
                        "job_id": step.get("job_id"),
                        "job_name": step.get("job_name"),
                        "enabled": bool(step.get("enabled")),
                        "step_enabled": bool(step.get("step_enabled")),
                        "function": f"{called_schema}.{called_function}",
                        "schedule": None,
                    }
                    matched_job_ids[key] = int(step.get("job_id"))

            # Render each detected scanner's own pgAgent schedule for the demo view.
            for key, job_id in matched_job_ids.items():
                cur.execute(_PGAGENT_SCHEDULES_SQL, {"job_id": job_id})
                schedules = [dict(row) for row in cur.fetchall()]
                enabled = [row for row in schedules if row.get("enabled")]
                if len(enabled) != 1:
                    continue
                cron, _warnings = convert_pgagent_schedule_to_cron(
                    enabled[0].get("minutes"),
                    enabled[0].get("hours"),
                    enabled[0].get("monthdays"),
                    enabled[0].get("months"),
                    enabled[0].get("weekdays"),
                )
                found[key]["schedule"] = cron

    return found


def get_database_readiness(conn: Optional[Connection] = None) -> dict[str, Any]:
    """
    Report whether the current role can use the partition-job objects.

    Read-only introspection only — this never grants or changes any privilege.
    ``conn``: reuse an already-open short-lived connection (Phase 5F.1).
    """
    schema = partition_job_schema()
    _fn_schema, function_name = partition_job_function_identity()
    params = {
        "schema_name": schema,
        "insert_function": (
            f"{schema}.{function_name}({INSERT_FUNCTION_ARGUMENT_TYPES})"
        ),
        "config_table": f"{schema}.{partition_job_table_name()}",
        "log_table": f"{schema}.{partition_job_log_table_name()}",
        "sequence_name": f"{schema}.{partition_job_sequence_name()}",
    }

    def _run(c: Connection) -> dict[str, Any]:
        try:
            with c.cursor(row_factory=dict_row) as cur:
                cur.execute(_DATABASE_READINESS_SQL, params)
                row = cur.fetchone()
                return dict(row) if row else {}
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None

    if conn is not None:
        return _run(conn)
    with _connection(_main_db_kwargs) as own:
        return _run(own)


# ---------------------------------------------------------------------------
# Phase 5A — missed-run attention review (DBA workflow)
# ---------------------------------------------------------------------------

_ATTENTION_MIGRATION_HINT = (
    "Missed-run review is not available: apply migrations "
    "20261110_01_partition_job_attention.sql and "
    "20261110_02_missed_run_reconcile_retry_and_manual_lock.sql."
)


def _validate_attention_id(attention_id: Any) -> int:
    try:
        value = int(attention_id)
    except (TypeError, ValueError) as exc:
        raise DatabaseError("Attention item ID must be a whole number.") from exc
    if value < 1:
        raise DatabaseError("Attention item ID must be greater than zero.")
    return value


def _attention_error(exc: PsycopgError) -> DatabaseError:
    """Map attention-function errors to safe, specific API errors."""
    sqlstate = getattr(exc, "sqlstate", None) or ""
    primary = getattr(getattr(exc, "diag", None), "message_primary", None)
    text = (primary or "").strip().split("\n")[0][:400]
    if "password" in text.lower():
        text = ""
    if sqlstate == "55P03":
        return DatabaseConflictError(
            "This missed run is already being processed by another request. "
            "Refresh and try again."
        )
    if sqlstate == "P0002":
        return DatabaseNotFoundError(text or "Attention item not found.")
    if sqlstate == "22023":
        return DatabaseConflictError(
            text or "The attention item can no longer be changed."
        )
    if sqlstate in ("42P01", "42883"):
        logger.error("event=attention_objects_missing sqlstate=%s", sqlstate)
        return DatabaseError(_ATTENTION_MIGRATION_HINT)
    return _map_psycopg_error(exc)


def get_partition_job_attention(
    status: Optional[str] = ATTENTION_STATUS_PENDING,
    limit: int = 200,
    attention_id: Optional[int] = None,
) -> tuple[list[dict], bool]:
    """Return attention items (newest first) and whether the table exists.

    ``status=None`` returns every status. Read-only. If the Phase 5A migration
    has not been applied yet the result is ``([], False)`` so the UI degrades
    instead of failing.
    """
    if status is not None and status not in ATTENTION_STATUSES:
        raise DatabaseError("Unsupported attention status filter.")
    try:
        row_limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        row_limit = 200
    attention_int = (
        _validate_attention_id(attention_id) if attention_id is not None else None
    )
    with _connection(_main_db_kwargs) as conn:
        try:
            return _attention_rows(conn, status=status, limit=row_limit,
                                   attention_id=attention_int), True
        except UndefinedTable:
            logger.warning("event=attention_table_missing (Phase 5A migration not applied)")
            return [], False
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def _attention_rows(
    conn: Connection,
    *,
    status: Optional[str],
    limit: int,
    attention_id: Optional[int] = None,
    job_id: Optional[int] = None,
) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(build_attention_list_sql(), {
            "status": status, "attention_id": attention_id, "job_id": job_id,
            "row_limit": max(1, min(int(limit), 1000)),
        })
        return [dict(row) for row in cur.fetchall()]


def get_partition_job_attention_item(attention_id: Any) -> Optional[dict]:
    """Return one attention item by id (any status), or None."""
    items, available = get_partition_job_attention(
        status=None, limit=1, attention_id=_validate_attention_id(attention_id)
    )
    if not available:
        raise DatabaseError(_ATTENTION_MIGRATION_HINT)
    return items[0] if items else None


def acknowledge_partition_job_attention(
    attention_id: Any, note: Optional[str] = None
) -> dict:
    """Mark a missed run as reviewed WITHOUT executing it. No DDL.

    Never changes next_run_time. Single transaction; commit on success.
    """
    attention_int = _validate_attention_id(attention_id)
    clean_note = (note or "").strip()[:500] or None
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        build_attention_acknowledge_sql(),
                        {"attention_id": attention_int, "note": clean_note},
                    )
                    row = cur.fetchone()
        except PsycopgError as exc:
            raise _attention_error(exc) from None
    logger.info(
        "event=attention_acknowledged attention_id=%s job_id=%s",
        attention_int,
        (row or {}).get("attention_job_id"),
    )
    return dict(row) if row else {"attention_status": "ACKNOWLEDGED"}


def recover_partition_job_attention(attention_id: Any) -> dict:
    """Run a missed occurrence manually via the DB recovery function.

    The database executes through run_partition_job_manual (caller privileges,
    target-table lock, MANUAL_* log row) and records the outcome on the
    attention row in the SAME transaction, which is committed before any
    failure is reported. next_run_time is never changed.
    """
    attention_int = _validate_attention_id(attention_id)
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        build_attention_recovery_sql(),
                        {"attention_id": attention_int},
                    )
                    row = cur.fetchone()
        except PsycopgError as exc:
            raise _attention_error(exc) from None

    if not row:
        raise DatabaseError("Missed-run recovery returned no result.")
    result = {
        "status": str(row.get("result_status") or ""),
        "message": row.get("result_message"),
        "execution_duration_ms": row.get("result_duration_ms"),
        "attention_status": row.get("attention_status"),
        "job_id": row.get("attention_job_id"),
    }
    logger.info(
        "event=attention_recovery attention_id=%s job_id=%s result=%s attention_status=%s",
        attention_int,
        result["job_id"],
        result["status"],
        result["attention_status"],
    )
    status_upper = result["status"].upper()
    if status_upper in (MANUAL_TARGET_LOCKED, MANUAL_REMOTE_TARGET):
        raise DatabaseConflictError(
            str(result["message"] or "The target table is busy; nothing was executed.")
        )
    if status_upper in _MANUAL_FAIL_STATUSES:
        raise DatabaseError(
            str(result["message"] or "Missed-run recovery failed.")
            + " The failure was recorded; the item remains pending review."
        )
    return result



# ---------------------------------------------------------------------------
# Phase 5C — hub-local database targets (read over the CONTROL connection)
# ---------------------------------------------------------------------------
# CONTROL database connection = _connection(_main_db_kwargs): jobs, registry,
# attention, history, readiness. MANAGED target connections are only opened by
# database_targets.open_target_connection (health / framework checks).

_TARGET_MIGRATION_HINT = (
    "Database targets are not available: apply migration "
    "20261117_01_partops_database_target.sql."
)


def list_database_targets(conn: Optional[Connection] = None) -> tuple[list[Any], list[dict], bool]:
    """Return (valid targets, invalid-row errors, registry_available).

    Each invalid registry row is reported separately (CONFIG_ERROR) and never
    hides the valid targets.
    """
    from database_targets import DatabaseTarget, TargetConfigError, fetch_target_rows

    def _rows(c: Connection) -> Optional[list]:
        try:
            return fetch_target_rows(c, partition_job_schema())
        except UndefinedTable:
            logger.warning("event=target_registry_missing (Phase 5C migration not applied)")
            if conn is not None:
                c.rollback()
            return None
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None

    if conn is not None:
        rows = _rows(conn)
    else:
        with _connection(_main_db_kwargs) as own:
            rows = _rows(own)
    if rows is None:
        return [], [], False
    targets: list[Any] = []
    invalid: list[dict] = []
    for row in rows:
        try:
            targets.append(DatabaseTarget.from_row(row))
        except (TargetConfigError, KeyError, TypeError, ValueError) as exc:
            invalid.append({
                "target_id": row.get("target_id"),
                "target_key": row.get("target_key"),
                "error": str(exc)[:300],
            })
    return targets, invalid, True


def get_database_target(target_id: Any) -> Optional[Any]:
    """One valid target by id (None if unknown). Raises DatabaseError if invalid."""
    try:
        wanted = int(target_id)
    except (TypeError, ValueError):
        raise DatabaseError("Database target ID must be a whole number.") from None
    targets, invalid, available = list_database_targets()
    if not available:
        raise DatabaseError(_TARGET_MIGRATION_HINT)
    for bad in invalid:
        if bad.get("target_id") == wanted:
            raise DatabaseError("Database target {} is misconfigured: {}".format(wanted, bad["error"]))
    for t in targets:
        if t.target_id == wanted:
            return t
    return None


# ---------------------------------------------------------------------------
# Phase 5D — remote execution attempts (CONTROL database; read + resolution)
# ---------------------------------------------------------------------------
_ATTEMPT_MIGRATION_HINT = (
    "Remote execution attempts are not available: apply migrations "
    "20261117_03 and 20261117_04."
)
ATTEMPT_STATES = frozenset(
    {"PREPARED", "DISPATCHING", "TARGET_SUCCEEDED", "TARGET_FAILED", "OUTCOME_UNKNOWN", "FINALIZED"}
)
_ATTEMPT_COLUMNS = (
    "attempt_id, job_id, database_target_id, target_key_snapshot AS target_key, "
    "execution_kind, expected_run_time, operation, job_name_snapshot AS job_name, "
    "table_schema_snapshot AS table_schema, table_name_snapshot AS table_name, "
    "partition_unit_snapshot AS partition_unit, partition_period_snapshot AS partition_period, "
    "create_drop_interval_snapshot::text AS create_drop_interval, "
    "db_config_snapshot AS db_config, planned_next_run_time, attention_id, state, "
    "failure_count, created_at, updated_at, dispatch_started_at, target_finished_at, "
    "finalized_at, result, error_class, error_summary, execution_duration_ms"
)


def _validate_attempt_id(attempt_id: Any) -> int:
    try:
        value = int(attempt_id)
    except (TypeError, ValueError) as exc:
        raise DatabaseError("Execution attempt ID must be a whole number.") from exc
    if value < 1:
        raise DatabaseError("Execution attempt ID must be greater than zero.")
    return value


def list_execution_attempts(
    state: Optional[str] = None,
    open_only: bool = False,
    job_id: Optional[int] = None,
    attempt_id: Optional[int] = None,
    limit: int = 200,
) -> tuple[list[dict], bool]:
    """Remote execution attempts, newest first. Read-only; no credentials.

    Returns ``([], False)`` if the Phase 5D ledger is not installed.
    """
    if state is not None and state not in ATTEMPT_STATES:
        raise DatabaseError("Unsupported execution attempt state filter.")
    row_limit = max(1, min(int(limit or 200), 1000))
    sql = (
        f"SELECT {_ATTEMPT_COLUMNS} FROM {partition_job_schema()}.partops_execution_attempt "
        "WHERE (%(state)s::text IS NULL OR state = %(state)s::text) "
        "  AND (NOT %(open_only)s OR state <> 'FINALIZED') "
        "  AND (%(job_id)s::numeric IS NULL OR job_id = %(job_id)s::numeric) "
        "  AND (%(attempt_id)s::bigint IS NULL OR attempt_id = %(attempt_id)s::bigint) "
        "ORDER BY attempt_id DESC LIMIT %(row_limit)s"
    )
    params = {
        "state": state, "open_only": bool(open_only),
        "job_id": _validate_job_id(job_id) if job_id is not None else None,
        "attempt_id": _validate_attempt_id(attempt_id) if attempt_id is not None else None,
        "row_limit": row_limit,
    }
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()], True
        except UndefinedTable:
            logger.warning("event=execution_attempt_table_missing (Phase 5D migration not applied)")
            return [], False
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def page_execution_attempts(
    *,
    state: Optional[str] = None,
    open_only: bool = False,
    job_id: Optional[int] = None,
    target_id: Optional[int] = None,
    limit: Any = 50,
    offset: Any = 0,
) -> dict[str, Any]:
    """One page of remote execution attempts (newest first) + unknown count.

    ``unknown_count`` counts ALL unresolved OUTCOME_UNKNOWN attempts (not just
    this page) so unknown outcomes stay visible regardless of paging/filters.
    """
    if state is not None and state not in ATTEMPT_STATES:
        raise DatabaseError("Unsupported execution attempt state filter.")
    lim, off = _page_args(limit, offset, default_limit=50)
    where = []
    params: dict[str, Any] = {"lim": lim + 1, "off": off}
    if state:
        where.append("state = %(state)s")
        params["state"] = state
    if open_only:
        where.append("state <> 'FINALIZED'")
    if job_id is not None:
        where.append("job_id = %(job_id)s")
        params["job_id"] = _validate_job_id(job_id)
    if target_id is not None:
        where.append("database_target_id = %(target_id)s")
        params["target_id"] = int(target_id)
    table = f"{partition_job_schema()}.partops_execution_attempt"
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    started = time.monotonic()
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f"SELECT {_ATTEMPT_COLUMNS} FROM {table}{cond} "
                            "ORDER BY attempt_id DESC LIMIT %(lim)s OFFSET %(off)s", params)
                rows = [dict(r) for r in cur.fetchall()]
                cur.execute(f"SELECT count(*) AS n FROM {table} WHERE state = 'OUTCOME_UNKNOWN'")
                unknown = int((cur.fetchone() or {}).get("n") or 0)
        except UndefinedTable:
            return {"attempts": [], "available": False, "has_more": False, "limit": lim,
                    "offset": off, "next_offset": None, "previous_offset": None, "unknown_count": 0}
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None
    _emit_query_timing("attempts.query_duration_ms", started)
    has_more = len(rows) > lim
    return {
        "attempts": rows[:lim],
        "available": True,
        "has_more": has_more,
        "limit": lim,
        "offset": off,
        "next_offset": off + lim if has_more else None,
        "previous_offset": max(0, off - lim) if off > 0 else None,
        "unknown_count": unknown,
    }


def get_execution_attempt(attempt_id: Any) -> Optional[dict]:
    rows, available = list_execution_attempts(attempt_id=_validate_attempt_id(attempt_id), limit=1)
    if not available:
        raise DatabaseError(_ATTEMPT_MIGRATION_HINT)
    return rows[0] if rows else None


def get_job_open_attempt_state(job_id: Any) -> Optional[str]:
    """Most significant open remote attempt state of a job (None = none / pre-5D)."""
    job_id_int = _validate_job_id(job_id)
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {partition_job_schema()}.partops_job_open_attempt_state(%(j)s)",
                    {"j": job_id_int},
                )
                row = cur.fetchone()
        except UndefinedFunction:
            return None
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None
    return str(row[0]) if row and row[0] else None


def resolve_remote_outcome(attempt_id: Any, resolution: str, note: str) -> dict:
    """Operator resolution of an OUTCOME_UNKNOWN attempt. No DDL anywhere.

    APPLIED / NOT_APPLIED is the operator's finding after inspecting the target.
    The attempt is finalized, its attention item acknowledged and automatic
    scheduling of the job resumed (if enabled).
    """
    attempt_int = _validate_attempt_id(attempt_id)
    clean = (resolution or "").strip().upper()
    if clean not in ("APPLIED", "NOT_APPLIED"):
        raise DatabaseError("Resolution must be APPLIED or NOT_APPLIED.")
    clean_note = (note or "").strip()[:400]
    if not clean_note:
        raise DatabaseError("A note describing the inspected target state is required.")
    sql = (
        f"SELECT * FROM {partition_job_schema()}.partops_resolve_remote_outcome("
        "%(a)s, %(r)s, %(n)s)"
    )
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, {"a": attempt_int, "r": clean, "n": clean_note})
                    row = cur.fetchone()
        except PsycopgError as exc:
            raise _attention_error(exc) from None
    logger.info("event=remote_outcome_resolved attempt_id=%s result=%s",
                attempt_int, (row or {}).get("result"))
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Phase 5F.1 — bounded, paginated reads (one short-lived connection each)
# ---------------------------------------------------------------------------
HISTORY_MAX_LIMIT = 500
HISTORY_MAX_OFFSET = 100_000
MANUAL_RUN_STATUSES = ("MANUAL_SUCCESS", "MANUAL_FAIL", "MANUAL_OUTCOME_UNKNOWN")
_STATUS_FILTER_RE = re.compile(r"^[A-Z_]{1,40}$")

# Column list with the Phase 5D history columns read directly (index-friendly
# filters). The legacy fallback derives them with to_jsonb for databases that
# predate the columns (filtering by target then requires 20261117_03).
_HISTORY_COLUMNS = (
    "l.job_log_id, l.job_id, l.job_name, l.last_run_status, l.job_runtime, l.job_error, "
    "l.execution_duration_ms, l.database_target_id, l.execution_attempt_id"
)
_HISTORY_COLUMNS_LEGACY = (
    "l.job_log_id, l.job_id, l.job_name, l.last_run_status, l.job_runtime, l.job_error, "
    "(to_jsonb(l) ->> 'execution_duration_ms')::bigint AS execution_duration_ms, "
    "(to_jsonb(l) ->> 'database_target_id')::integer AS database_target_id, "
    "(to_jsonb(l) ->> 'execution_attempt_id')::bigint AS execution_attempt_id"
)


def _page_args(limit: Any, offset: Any, default_limit: int = 100) -> tuple[int, int]:
    try:
        lim = int(limit) if limit is not None else default_limit
        off = int(offset) if offset is not None else 0
    except (TypeError, ValueError) as exc:
        raise DatabaseError("limit and offset must be whole numbers.") from exc
    if lim < 1 or lim > HISTORY_MAX_LIMIT:
        raise DatabaseError(f"limit must be between 1 and {HISTORY_MAX_LIMIT}.")
    if off < 0 or off > HISTORY_MAX_OFFSET:
        raise DatabaseError(f"offset must be between 0 and {HISTORY_MAX_OFFSET}.")
    return lim, off


def query_partition_job_logs(
    *,
    limit: Any = 100,
    offset: Any = 0,
    target_id: Optional[int] = None,
    status: Optional[str] = None,
    job: Optional[str] = None,
    operation: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> dict[str, Any]:
    """One page of execution history, newest first (deterministic order).

    Filters are combined from FIXED SQL fragments with bound parameters.
    ``job``: a job id (digits) or a case-insensitive name fragment.
    ``operation``: CREATE / DROP of the job as currently configured.
    ``target_id``: NULL history rows (local executors, pre-5D) belong to the
    control database. Uses LIMIT n+1 for ``has_more`` (no COUNT(*) scan).
    """
    lim, off = _page_args(limit, offset)
    where: list[str] = []
    params: dict[str, Any] = {"lim": lim + 1, "off": off}
    if status:
        s = str(status).strip().upper()
        if not _STATUS_FILTER_RE.match(s):
            raise DatabaseError("Unsupported status filter.")
        where.append("l.last_run_status = %(status)s")
        params["status"] = s
    if job:
        j = str(job).strip()[:100]
        if j.isdigit():
            where.append("l.job_id = %(job_id)s")
            params["job_id"] = int(j)
        elif j:
            where.append("l.job_name ILIKE %(job_name)s")
            params["job_name"] = "%" + j.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if operation:
        op = str(operation).strip().upper()
        if op not in ("CREATE", "DROP"):
            raise DatabaseError("operation must be CREATE or DROP.")
        where.append(
            f"EXISTS (SELECT 1 FROM {partition_job_schema()}.{partition_job_table_name()} AS jj "
            "WHERE jj.job_id = l.job_id AND jj.is_create = %(is_create)s)"
        )
        params["is_create"] = op == "CREATE"
    if since is not None:
        where.append("l.job_runtime >= %(since)s")
        params["since"] = since
    if until is not None:
        where.append("l.job_runtime < %(until)s")
        params["until"] = until
    if target_id is not None:
        params["target_id"] = int(target_id)

    def build(columns: str, target_clause: Optional[str]) -> str:
        clauses = list(where) + ([target_clause] if target_clause else [])
        cond = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return (
            f"SELECT {columns} FROM {partition_job_schema()}.{partition_job_log_table_name()} AS l"
            f"{cond} ORDER BY l.job_runtime DESC, l.job_log_id DESC LIMIT %(lim)s OFFSET %(off)s"
        )

    started = time.monotonic()
    with _connection(_main_db_kwargs) as conn:
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                target_clause = None
                if target_id is not None:
                    # Resolve the control target ONCE (not per row): NULL
                    # history rows (local executors, pre-5D) belong to it.
                    try:
                        cur.execute(f"SELECT {partition_job_schema()}.partops_control_target_id() AS id")
                    except UndefinedFunction:
                        conn.rollback()
                        raise DatabaseError(
                            "Filtering history by database target requires migration 20261117_03."
                        ) from None
                    control_row = cur.fetchone() or {}
                    target_clause = (
                        "(l.database_target_id = %(target_id)s OR l.database_target_id IS NULL)"
                        if control_row.get("id") == params["target_id"]
                        else "l.database_target_id = %(target_id)s"
                    )
                try:
                    cur.execute(build(_HISTORY_COLUMNS, target_clause), params)
                except (UndefinedColumn, UndefinedFunction):
                    conn.rollback()
                    if target_clause is not None:
                        raise DatabaseError(
                            "Filtering history by database target requires migration 20261117_03."
                        ) from None
                    cur.execute(build(_HISTORY_COLUMNS_LEGACY, None), params)
                rows = [dict(r) for r in cur.fetchall()]
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None
    _emit_query_timing("history.query_duration_ms", started)
    has_more = len(rows) > lim
    rows = rows[:lim]
    return {
        "logs": rows,
        "limit": lim,
        "offset": off,
        "has_more": has_more,
        "next_offset": off + lim if has_more else None,
        "previous_offset": max(0, off - lim) if off > 0 else None,
    }


def _emit_query_timing(metric: str, started: float) -> None:
    try:
        from observability import timing

        timing(metric, (time.monotonic() - started) * 1000.0)
    except Exception:  # noqa: BLE001 — metrics never break reads
        logger.debug("query timing emit failed", exc_info=True)


_LATEST_MANUAL_RUN_SQL = """
SELECT l.job_log_id, l.job_id, l.last_run_status, l.job_runtime, l.job_error,
       (to_jsonb(l) ->> 'execution_duration_ms')::bigint AS execution_duration_ms,
       (to_jsonb(l) ->> 'database_target_id')::integer AS database_target_id
  FROM {schema}.{log_table} AS l
 WHERE l.job_id = %(job_id)s
   AND l.last_run_status IN ('MANUAL_SUCCESS', 'MANUAL_FAIL', 'MANUAL_OUTCOME_UNKNOWN')
 ORDER BY l.job_runtime DESC, l.job_log_id DESC
 LIMIT 1
"""


def _latest_manual_run(conn: Connection, job_id: int) -> Optional[dict]:
    """Latest MANUAL_* history row of THIS job only (index: job_id, job_runtime)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _LATEST_MANUAL_RUN_SQL.format(
                schema=partition_job_schema(), log_table=partition_job_log_table_name()),
            {"job_id": job_id},
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_job_runtime_context(job_id: Any) -> dict[str, Any]:
    """Everything the job drawer needs about one job, on ONE connection:

    latest manual run (that job only), the most significant open remote
    attempt state, and pending review items. ``last_run_status`` (latest
    SCHEDULED result) is not touched or redefined.
    """
    job_id_int = _validate_job_id(job_id)
    schema = partition_job_schema()
    out: dict[str, Any] = {
        "job_id": job_id_int,
        "latest_manual_run": None,
        "open_attempt_state": None,
        "pending_attention": [],
        "attention_available": True,
    }
    with _connection(_main_db_kwargs) as conn:
        try:
            out["latest_manual_run"] = _latest_manual_run(conn, job_id_int)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT {schema}.partops_job_open_attempt_state(%(j)s)", {"j": job_id_int})
                    row = cur.fetchone()
                out["open_attempt_state"] = str(row[0]) if row and row[0] else None
            except UndefinedFunction:
                conn.rollback()
            try:
                out["pending_attention"] = _attention_rows(
                    conn, status=ATTENTION_STATUS_PENDING, limit=50, job_id=job_id_int)
            except UndefinedTable:
                conn.rollback()
                out["attention_available"] = False
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None
    return out


def get_job_pending_attention(job_id: Any) -> tuple[list[dict], bool]:
    """Pending review items of ONE job (partial index on job_id, PENDING)."""
    job_id_int = _validate_job_id(job_id)
    with _connection(_main_db_kwargs) as conn:
        try:
            return _attention_rows(conn, status=ATTENTION_STATUS_PENDING, limit=50,
                                   job_id=job_id_int), True
        except UndefinedTable:
            return [], False
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def get_dashboard_snapshot(log_limit: int = 200) -> dict[str, Any]:
    """Jobs + recent history + privilege readiness on ONE connection.

    Each section fails independently (its error is reported, the others still
    load). Previously three separate connections per dashboard request.
    """
    out: dict[str, Any] = {"jobs": [], "logs": [], "readiness": None,
                           "jobs_error": None, "logs_error": None, "readiness_error": None}
    try:
        with _connection(_main_db_kwargs) as conn:
            for key, fn in (("jobs", lambda: _query_jobs(conn)),
                            ("logs", lambda: _query_recent_logs(conn, log_limit)),
                            ("readiness", lambda: get_database_readiness(conn))):
                try:
                    out[key] = fn()
                except DatabaseError as exc:
                    out[f"{key}_error"] = exc.message
                except PsycopgError as exc:
                    out[f"{key}_error"] = _map_psycopg_error(exc).message
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
    except DatabaseError as exc:
        for key in ("jobs", "logs", "readiness"):
            out[f"{key}_error"] = exc.message
    return out
