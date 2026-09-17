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
from psycopg.rows import dict_row

from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.db_safety import (
    assert_expected_database,
    read_current_database,
)

logger = logging.getLogger(__name__)

_IDENTIFIER_RE_MSG = "Database identifier values must be simple SQL identifiers."


def _load_env() -> None:
    """Prefer .env.realtime for the scheduler; never require overwriting .env."""
    # Local override first, then fallback to .env for shared non-secret settings.
    load_dotenv(".env.realtime", override=False)
    load_dotenv(".env", override=False)


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


def load_scheduler_config() -> SchedulerConfig:
    _load_env()
    return SchedulerConfig(
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
        schema=_identifier("PARTITION_JOB_SCHEMA", "mubasher_oms"),
        upcoming_function=_identifier(
            "PARTITION_UPCOMING_JOBS_FUNCTION", "get_upcoming_partition_jobs"
        ),
        scheduled_run_function=_identifier(
            "PARTITION_SCHEDULED_RUN_FUNCTION", "run_partition_job_scheduled"
        ),
    )


def _db_kwargs() -> dict[str, Any]:
    _load_env()
    return {
        "host": _require("DB_HOST"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": _require("DB_NAME"),
        "user": _require("DB_USER"),
        "password": _require("DB_PASSWORD"),
        "sslmode": (os.getenv("DB_SSLMODE") or "prefer").strip() or "prefer",
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    }


@contextmanager
def open_connection(purpose: str) -> Generator[Connection, None, None]:
    """Open one short-lived connection, verify DB name, then always close."""
    logger.debug("Opening DB connection for %s", purpose)
    conn: Optional[Connection] = None
    try:
        conn = connect(**_db_kwargs())
        assert_expected_database(read_current_database(conn))
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


def fetch_upcoming_jobs(config: SchedulerConfig) -> list[ScheduledJob]:
    """
    Retrieve upcoming (and overdue) jobs, then close the connection.

    Uses DATABASE time for delay_seconds via get_upcoming_partition_jobs().
    """
    lookahead = timedelta(seconds=max(float(config.lookahead_seconds), 0.0))
    sql = (
        f"SELECT job_id, expected_run_time, delay_seconds, job_name, is_create "
        f"FROM {config.schema}.{config.upcoming_function}(%(lookahead)s)"
    )
    rows: list[ScheduledJob] = []
    with open_connection("upcoming-job refresh") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, {"lookahead": lookahead})
            for row in cur.fetchall():
                job_id = int(row["job_id"])
                expected = row["expected_run_time"]
                if not isinstance(expected, datetime):
                    raise RuntimeError(
                        f"expected_run_time for job {job_id} is not a timestamp"
                    )
                delay = float(row["delay_seconds"])
                rows.append(
                    ScheduledJob(
                        job_id=job_id,
                        expected_run_time=expected,
                        delay_seconds=delay,
                        job_name=row.get("job_name"),
                        is_create=row.get("is_create"),
                    )
                )
    return rows


def execute_scheduled_job(
    config: SchedulerConfig, job_id: int, expected_run_time: datetime
) -> dict[str, Any]:
    """
    Open a NEW connection, execute one scheduled occurrence, close immediately.

    Returns the first result row from run_partition_job_scheduled(...).
    """
    sql = (
        f"SELECT status, job_id, message, next_run_time, is_create "
        f"FROM {config.schema}.{config.scheduled_run_function}"
        f"(%(job_id)s, %(expected_run_time)s)"
    )
    with open_connection(f"scheduled job {job_id}") as conn:
        try:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        sql,
                        {
                            "job_id": job_id,
                            "expected_run_time": expected_run_time,
                        },
                    )
                    row = cur.fetchone()
                    if not row:
                        return {
                            "status": "FAILED",
                            "job_id": job_id,
                            "message": "Scheduled executor returned no row.",
                            "next_run_time": None,
                            "is_create": None,
                        }
                    return dict(row)
        except PsycopgError as exc:
            logger.exception(
                "Scheduled execution failed for job_id=%s (sqlstate=%s)",
                job_id,
                getattr(exc, "sqlstate", None),
            )
            return {
                "status": "FAILED",
                "job_id": job_id,
                "message": "Database error during scheduled execution.",
                "next_run_time": None,
                "is_create": None,
            }
