"""Fail-closed helpers for realtime DB identity and migration tooling gates.

Normal scheduler runtime uses assert_expected_database() only.
Migration tooling must also require PARTITION_REALTIME_ALLOW_MIGRATION=true.
Neither helper prints passwords.
"""

from __future__ import annotations

import os
from typing import Any, Optional


class DatabaseSafetyError(RuntimeError):
    """Raised when connected database identity or migration gate fails."""


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(".env.realtime", override=False)
        load_dotenv(".env", override=False)
    except Exception:  # noqa: BLE001
        pass


def expected_database_name() -> str:
    _load_env()
    return (os.getenv("REALTIME_DB_EXPECTED_NAME") or "").strip()


def migration_allowed() -> bool:
    _load_env()
    return (os.getenv("PARTITION_REALTIME_ALLOW_MIGRATION") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def assert_expected_database(actual_database: str) -> None:
    """
    Fail closed if REALTIME_DB_EXPECTED_NAME is set and does not match.

    If the expected name is unset, raise — scheduler against the NEW DB must
    always declare which database it intends to use.
    """
    expected = expected_database_name()
    if not expected:
        raise DatabaseSafetyError(
            "REALTIME_DB_EXPECTED_NAME is required for the realtime scheduler. "
            "Set it in .env.realtime to the NEW database name."
        )
    if actual_database != expected:
        raise DatabaseSafetyError(
            f"Refusing database work: connected to '{actual_database}' but "
            f"REALTIME_DB_EXPECTED_NAME is '{expected}'."
        )


def assert_migration_gate(*, actual_database: Optional[str] = None) -> None:
    """Require explicit env approval before migration tooling may proceed."""
    if not migration_allowed():
        raise DatabaseSafetyError(
            "Migration blocked: set PARTITION_REALTIME_ALLOW_MIGRATION=true "
            "only after explicit approval. Default is false/absent."
        )
    if actual_database is not None:
        assert_expected_database(actual_database)


def read_current_database(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
    if not row:
        raise DatabaseSafetyError("Unable to read current_database().")
    return str(row[0])
