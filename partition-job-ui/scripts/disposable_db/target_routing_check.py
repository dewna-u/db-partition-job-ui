#!/usr/bin/env python3
"""DISPOSABLE-ONLY Phase 5C routing check (run by run_disposable_db_test.sh).

Reads the target registry over the CONTROL connection, then opens a separate
connection per managed target via database_targets.open_target_connection and
runs the read-only per-target health check. Proves:
  * control vs target routing (each target lands in its own database)
  * identity verification (current_database == registry database_name)
  * health isolation (disabled / unreachable targets do not affect others)
Never writes. Exit 0 = all expectations met.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from psycopg import connect  # noqa: E402

from database_targets import (  # noqa: E402
    DISABLED,
    FRAMEWORK_INCOMPATIBLE,
    UNREACHABLE,
    check_target_health,
    fetch_target_rows,
    load_targets_from_rows,
    open_target_connection,
)


def main() -> int:
    control = connect(host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
                      dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                      password=os.environ.get("DB_PASSWORD", ""))
    try:
        targets = load_targets_from_rows(fetch_target_rows(control))
        control.rollback()
    finally:
        control.close()
    by_key = {t.target_key: t for t in targets.values()}
    ok = True
    for key, expected_db in (("db01", os.environ["DB_NAME"]), ("db02", "partops_target2")):
        with open_target_connection(by_key[key], "routing check") as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                actual = cur.fetchone()[0]
        print("ROUTE {} -> {}".format(key, actual))
        ok &= actual == expected_db
    expected_state = {
        "db01": FRAMEWORK_INCOMPATIBLE,  # legacy framework functions not captured/installed
        "db02": FRAMEWORK_INCOMPATIBLE,  # connected + identity OK; workers absent
        "db03": DISABLED,
        "db04": UNREACHABLE,
    }
    for key, want in expected_state.items():
        health = check_target_health(by_key[key])
        fw = health.get("framework") or {}
        print("HEALTH {} {} {}".format(key, health["state"], fw.get("counts")))
        ok &= health["state"] == want
    db02 = check_target_health(by_key["db02"])
    problems = {p["object"] for p in (db02.get("framework") or {}).get("problems", [])}
    print("DB02 problems:", sorted(problems))
    # Remote target: only target_execution scope (workers), no version/registry checks.
    ok &= not any("framework_version" in p or "control_target" in p for p in problems)
    ok &= any("create_any_table_partition" in p for p in problems)
    print("ROUTING", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
