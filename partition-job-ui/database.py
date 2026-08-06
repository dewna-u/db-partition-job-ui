"""Database access for partition job creation and pgAgent job listing."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

from dotenv import load_dotenv
from psycopg import Connection, Error as PsycopgError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from job_autofill import (
    build_db_config_json,
    calculate_next_run,
    convert_pgagent_schedule_to_cron,
    count_top_level_call_arguments,
    extract_partition_settings,
    extract_single_called_function,
    extract_single_target_table,
    infer_frequency,
    infer_is_create,
)

load_dotenv()

logger = logging.getLogger(__name__)

APPLICATION_NAME = "partition-job-ui"

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

_CREATE_PARTITION_JOB_SQL = """
SELECT mubasher_oms.insert_data_to_partition_job_table(
    p_job_name                 => %(job_name)s,
    p_is_enabled               => %(is_enabled)s,
    p_table_schema             => %(table_schema)s,
    p_table_name               => %(table_name)s,
    p_db_config_para           => %(db_config)s::jsonb,
    p_job_schedule             => %(job_schedule)s,
    p_frequency                => %(frequency)s::interval,
    p_next_run_time            => %(next_run_time)s,
    p_partition_unit           => %(partition_unit)s,
    p_partition_period         => %(partition_period)s,
    p_is_create                => %(is_create)s,
    p_is_create_drop_interval  => %(create_drop_interval)s::interval
);
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

_FUNCTION_DEF_SQL = """
SELECT
    p.oid,
    pg_get_functiondef(p.oid) AS function_definition,
    pronargs AS argument_count
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = %(function_schema)s
  AND p.proname = %(function_name)s
ORDER BY p.oid;
"""


class DatabaseError(Exception):
    """Safe, user-facing database error."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class PgAgentNotInstalledError(DatabaseError):
    """Raised when pgagent.pga_job is not present."""


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

    return {
        "host": os.getenv("DB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        "port": port,
        "dbname": _require_env("DB_NAME"),
        "user": _require_env("DB_USER"),
        "password": _require_env("DB_PASSWORD"),
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


def _map_psycopg_error(exc: PsycopgError) -> DatabaseError:
    """Map a psycopg exception to a safe user-facing DatabaseError."""
    sqlstate = getattr(exc, "sqlstate", None) or ""
    # Diagnostic message for logging only — never shown to the user.
    logger.exception("Database operation failed (sqlstate=%s)", sqlstate)

    # Unique violation
    if sqlstate == "23505":
        return DatabaseError(
            "A partition job with the same unique value may already exist."
        )
    # Insufficient privilege
    if sqlstate in ("42501",):
        return DatabaseError(
            "The application database user does not have the required permission."
        )
    # Undefined function
    if sqlstate in ("42883",):
        return DatabaseError(
            "The partition-job function was not found in the configured database."
        )
    # Connection failures / cannot connect
    if sqlstate.startswith("08") or sqlstate in ("57P01", "57P02", "57P03"):
        return DatabaseError(
            "Unable to connect to the database. Check the database configuration and server logs."
        )

    msg = str(exc).lower()
    if "could not connect" in msg or "connection refused" in msg:
        return DatabaseError(
            "Unable to connect to the database. Check the database configuration and server logs."
        )
    if "permission denied" in msg or "insufficient privilege" in msg:
        return DatabaseError(
            "The application database user does not have the required permission."
        )
    if "does not exist" in msg and "function" in msg:
        return DatabaseError(
            "The partition-job function was not found in the configured database."
        )
    if "unique" in msg or "duplicate" in msg:
        return DatabaseError(
            "A partition job with the same unique value may already exist."
        )

    return DatabaseError(
        "The database operation failed. Check the server logs for details."
    )


@contextmanager
def _connection(kwargs: dict[str, Any]) -> Generator[Connection, None, None]:
    """Open a short-lived connection and always close it."""
    conn: Optional[Connection] = None
    try:
        from psycopg import connect

        conn = connect(**kwargs)
        yield conn
    except PsycopgError as exc:
        raise _map_psycopg_error(exc) from None
    except OSError:
        logger.exception("OS-level database connection failure")
        raise DatabaseError(
            "Unable to connect to the database. Check the database configuration and server logs."
        ) from None
    finally:
        if conn is not None and not conn.closed:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — best-effort close
                logger.exception("Error while closing database connection")


def pgagent_table_exists() -> bool:
    """Return True if pgagent.pga_job exists in the configured pgAgent database."""
    with _connection(_pgagent_db_kwargs()) as conn:
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
    with _connection(_pgagent_db_kwargs()) as conn:
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


def create_partition_job(data: dict) -> Any:
    """
    Call mubasher_oms.insert_data_to_partition_job_table(...) and return its result.

    Uses a transaction: commit on success, roll back on failure.
    Always closes the connection.
    """
    params = {
        "job_name": data["job_name"],
        "is_enabled": data["is_enabled"],
        "table_schema": data["table_schema"],
        "table_name": data["table_name"],
        "db_config": Jsonb(data["db_config"]),
        "job_schedule": data["job_schedule"],
        "frequency": data["frequency"],
        "next_run_time": data["next_run_time"],
        "partition_unit": data["partition_unit"],
        "partition_period": data["partition_period"],
        "is_create": data["is_create"],
        "create_drop_interval": data["create_drop_interval"],
    }

    with _connection(_main_db_kwargs()) as conn:
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(_CREATE_PARTITION_JOB_SQL, params)
                    row = cur.fetchone()
                    result = row[0] if row else None
            return result
        except DatabaseError:
            raise
        except PsycopgError as exc:
            raise _map_psycopg_error(exc) from None


def _summarise_step(step: dict, include_function: Optional[tuple[str, str]] = None) -> dict:
    summary = {
        "step_id": step.get("step_id"),
        "step_name": step.get("step_name"),
        "enabled": bool(step.get("enabled")),
        "kind": step.get("kind"),
        "dbname": step.get("dbname"),
    }
    if include_function:
        summary["function_schema"] = include_function[0]
        summary["function_name"] = include_function[1]
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


def _resolve_called_function(
    step: dict,
) -> tuple[Optional[tuple[str, str]], list[str]]:
    warnings: list[str] = []
    code = step.get("code") or ""
    function_pair, warning = extract_single_called_function(code)
    if warning:
        warnings.append(warning)
    return function_pair, warnings


def _inspect_called_function(
    function_schema: str,
    function_name: str,
    step_code: str,
) -> tuple[dict[str, Any], list[str]]:
    """Read function metadata only. Never execute the function or jstcode."""
    autofill: dict[str, Any] = {}
    warnings: list[str] = []

    try:
        with _connection(_main_db_kwargs()) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    _FUNCTION_DEF_SQL,
                    {
                        "function_schema": function_schema,
                        "function_name": function_name,
                    },
                )
                rows = [dict(row) for row in cur.fetchall()]
    except DatabaseError as exc:
        warnings.append(exc.message)
        return autofill, warnings

    if not rows:
        warnings.append(
            "The function called by the job step was not found in the configured database. "
            "Table Schema, Table Name, Partition Unit, Partition Period, "
            "and Create/Drop Interval were left unchanged."
        )
        return autofill, warnings

    chosen = rows[0]
    if len(rows) > 1:
        arg_count = count_top_level_call_arguments(
            step_code, function_schema, function_name
        )
        matching = []
        if arg_count is not None:
            matching = [
                row for row in rows if int(row.get("argument_count") or -1) == arg_count
            ]
        if len(matching) == 1:
            chosen = matching[0]
        else:
            warnings.append(
                "Multiple overloads exist for the called function and the correct one "
                "could not be identified safely. Function-derived fields were left unchanged."
            )
            return autofill, warnings

    definition = chosen.get("function_definition") or ""

    table_pair, table_warning = extract_single_target_table(definition)
    if table_pair:
        autofill["table_schema"] = table_pair[0]
        autofill["table_name"] = table_pair[1]
    elif table_warning:
        warnings.append(table_warning)

    settings, setting_warnings = extract_partition_settings(definition)
    warnings.extend(setting_warnings)
    autofill.update(settings)
    return autofill, warnings


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

    with _connection(_pgagent_db_kwargs()) as conn:
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

    is_create = infer_is_create(job_name)
    if is_create is None:
        warnings.append(
            "Create Partitions could not be inferred from the job name "
            "(CREATE/DROP token is missing or ambiguous) and was left unchanged."
        )
    else:
        autofill["is_create"] = is_create

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

    parsed_steps: list[tuple[dict, Optional[tuple[str, str]], list[str]]] = []
    for step in sql_steps:
        function_pair, step_warnings = _resolve_called_function(step)
        parsed_steps.append((step, function_pair, step_warnings))
        step_summaries.append(_summarise_step(step, function_pair))

    if step_id is not None:
        try:
            step_id_int = int(step_id)
        except (TypeError, ValueError) as exc:
            raise DatabaseError("Job step ID must be a whole number.") from exc
        for step, function_pair, step_warnings in parsed_steps:
            if step.get("step_id") == step_id_int:
                selected_step = step
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
            "Table Name, and function-derived fields were left unchanged."
        )
    elif len(parsed_steps) == 1:
        selected_step, _function_pair, step_warnings = parsed_steps[0]
        warnings.extend(step_warnings)
    else:
        function_keys = {
            pair for _step, pair, _warn in parsed_steps if pair is not None
        }
        if len(function_keys) == 1 and all(
            pair is not None for _step, pair, _warn in parsed_steps
        ):
            selected_step, _function_pair, step_warnings = parsed_steps[0]
            warnings.extend(step_warnings)
            warnings.append(
                "Multiple enabled SQL steps were found; they appear to call the same "
                "function, so the first step was used."
            )
        else:
            warnings.append(
                "Multiple enabled SQL steps call different functions. "
                "Select a job step to continue auto-fill. "
                "Step-derived fields were left unchanged."
            )
            for step, function_pair, _step_warnings in parsed_steps:
                choice = _summarise_step(step, function_pair)
                step_choices.append(choice)

    database_name = None
    if selected_step is not None:
        database_name = selected_step.get("dbname")
        db_config_json = build_db_config_json(database_name)
        if db_config_json:
            autofill["db_config"] = db_config_json
        else:
            warnings.append(
                "The job step has no database name. "
                "Database Configuration was left unchanged."
            )

        function_pair, _already_warned = extract_single_called_function(
            selected_step.get("code") or ""
        )
        if function_pair:
            func_autofill, func_warnings = _inspect_called_function(
                function_pair[0],
                function_pair[1],
                selected_step.get("code") or "",
            )
            autofill.update(func_autofill)
            warnings.extend(func_warnings)

    public_steps = []
    for step in steps:
        function_pair, _warn = extract_single_called_function(step.get("code") or "")
        public_steps.append(_summarise_step(step, function_pair))

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
    }
