#!/usr/bin/env python3
"""READ-ONLY PartOps database framework preflight.

Compares a target database with sql/framework/framework_manifest.json and
prints PASS / WARNING / FAIL per check. Never modifies the database: the
catalog snapshot runs in a ``SET TRANSACTION READ ONLY`` transaction that is
rolled back.

Usage (DBA, against an APPROVED target):

  python scripts/preflight_framework.py --env-file .env.realtime \\
      --expect-database <dbname> [--scope all|api|scheduler] [--only-problems]

  # Offline: evaluate a previously saved snapshot (no DB connection)
  python scripts/preflight_framework.py --snapshot-in snapshot.json

Options:
  --snapshot-out FILE   also save the raw catalog snapshot (JSON, no secrets)
  --json                machine-readable output
  --strict              exit 1 on WARNING as well

Exit codes: 0 = PASS (or WARNING without --strict), 1 = WARNING with --strict,
2 = FAIL, 3 = usage / connection / identity error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework_manifest import (  # noqa: E402
    FAIL,
    WARNING,
    evaluate_snapshot,
    fetch_catalog_snapshot,
    format_report,
    load_manifest,
)


def _connect(expect_database: str):
    from psycopg import connect

    from db_credentials import resolve_database_credentials

    creds = resolve_database_credentials()
    host = (os.environ.get("DB_HOST") or "").strip()
    dbname = (os.environ.get("DB_NAME") or "").strip()
    if not host or not dbname:
        raise SystemExit("DB_HOST and DB_NAME must be set (use --env-file).")
    conn = connect(
        host=host,
        port=int(os.environ.get("DB_PORT") or "5432"),
        dbname=dbname,
        user=creds["user"],
        password=creds["password"],
        sslmode=(os.environ.get("DB_SSLMODE") or "prefer").strip() or "prefer",
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT") or "5"),
        application_name="partops-framework-preflight",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        actual = cur.fetchone()[0]
    conn.rollback()
    if actual != expect_database:
        conn.close()
        raise SystemExit(
            "Refusing: connected to {!r} but --expect-database is {!r}.".format(actual, expect_database)
        )
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="READ-ONLY PartOps framework preflight")
    parser.add_argument("--env-file", help="dotenv file with DB_* / credential settings")
    parser.add_argument("--expect-database", help="required: must equal current_database()")
    parser.add_argument("--scope", default="all", choices=["all", "api", "scheduler", "manual_run"])
    parser.add_argument("--snapshot-in", help="evaluate a saved snapshot (no DB connection)")
    parser.add_argument("--snapshot-out", help="save the catalog snapshot JSON")
    parser.add_argument("--only-problems", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_manifest()
    if args.snapshot_in:
        with open(args.snapshot_in, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    else:
        if not args.expect_database:
            print("--expect-database is required for a live preflight (identity safety).")
            return 3
        if args.env_file:
            from dotenv import load_dotenv

            load_dotenv(args.env_file, override=True)
        try:
            conn = _connect(args.expect_database)
        except SystemExit as exc:
            print(exc)
            return 3
        try:
            snapshot = fetch_catalog_snapshot(conn, manifest)
        finally:
            conn.close()
    if args.snapshot_out:
        with open(args.snapshot_out, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, default=str)

    scope = None if args.scope == "all" else args.scope
    report = evaluate_snapshot(
        manifest, snapshot, scope=scope, check_current_user_execute=(scope == "scheduler")
    )
    if args.json:
        print(json.dumps({
            "overall": report.overall,
            "counts": report.counts(),
            "expected_framework_version": report.expected_version,
            "database_framework_version": report.database_version,
            "results": [r.as_dict() for r in report.results],
        }, indent=2))
    else:
        print("PartOps framework preflight — database {!r} as {!r} ({})".format(
            snapshot.get("database"), snapshot.get("current_user"), snapshot.get("server_version")))
        print(format_report(report, only_problems=args.only_problems))
    if report.overall == FAIL:
        return 2
    if report.overall == WARNING and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
