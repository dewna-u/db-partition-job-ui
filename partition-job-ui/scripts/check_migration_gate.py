#!/usr/bin/env python3
"""Check migration safety gate WITHOUT executing SQL.

Usage:
  python scripts/check_migration_gate.py

Exit 0 only when PARTITION_REALTIME_ALLOW_MIGRATION=true and
REALTIME_DB_EXPECTED_NAME is set. Does not connect to PostgreSQL.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scheduler_backend.db_safety import (  # noqa: E402
    DatabaseSafetyError,
    assert_migration_gate,
    expected_database_name,
    migration_allowed,
)


def main() -> int:
    try:
        assert_migration_gate()
    except DatabaseSafetyError as exc:
        print(f"BLOCKED: {exc}")
        print(f"REALTIME_DB_EXPECTED_NAME={expected_database_name() or '(unset)'}")
        print(f"PARTITION_REALTIME_ALLOW_MIGRATION={migration_allowed()}")
        return 1
    print("Migration gate OPEN for tooling (SQL still requires human approval + TEMP TABLE).")
    print(f"REALTIME_DB_EXPECTED_NAME={expected_database_name()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
