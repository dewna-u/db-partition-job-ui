#!/usr/bin/env python3
"""DISPOSABLE-ONLY Phase 5D multi-database integration (run by run_disposable_db_test.sh).

Databases (all throwaway, one local initdb cluster):
  partops_disposable  CONTROL + target db01 (jobs, ledger, history, attention)
  partops_target2     managed target db02
  db03                registered but DISABLED; db04 registered UNREACHABLE (port 1)

Worker / cron functions here are the TEST-ONLY STUBS from
sql/tests/disposable/stubs (they record calls in partops_test.worker_calls of
THEIR OWN database). Nothing here validates production worker business logic.

Part A — engine level, real control functions + real target transactions:
  routing, commit, proven rollback (bounded retry → exhaustion), unknown
  outcome (no retry, attention, paused job, guarded edits, resolution),
  finalization after a "crash" (no re-dispatch), restart during DISPATCHING,
  disabled / unreachable targets, table locks per target, manual remote run.
Part B — the real PartitionScheduler: global + per-target concurrency and
  failure isolation (db04 down while db01 / db02 progress).

Fixtures are written as the disposable superuser; every PartOps call runs as
partition_job_scheduler / partition_job_ui. Exit 0 = all expectations met.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import psycopg  # noqa: E402
import psycopg.rows  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PORT = int(os.environ["DB_PORT"])
CONTROL_DB = os.environ["DB_NAME"]
TARGET2_DB = "partops_target2"
FAILURES: list = []


def check(cond: bool, label: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def su(db: str = CONTROL_DB):
    return psycopg.connect(host="127.0.0.1", port=PORT, dbname=db, user="postgres", autocommit=True)


def q(sql: str, params=None, db: str = CONTROL_DB):
    with su(db) as c:
        cur = c.execute(sql, params or {})
        rows = cur.fetchall() if cur.description is not None else []
    return rows


def q1(sql: str, params=None, db: str = CONTROL_DB):
    rows = q(sql, params, db)
    return rows[0][0] if rows else None


def as_role(role: str, sql: str, params=None):
    with su() as c:
        c.execute("SET ROLE " + role)
        try:
            cur = c.execute(sql, params or {})
            return cur.fetchall() if cur.description is not None else []
        finally:
            c.execute("RESET ROLE")


def new_job(job_id: int, target_key: str, table: str, is_create=True, due_in="-1 second",
            config="{}"):
    q("""
      INSERT INTO mubasher_oms.partitioning_job_table
        (job_id, job_name, is_enabled, table_schema, table_name, db_config_para, frequency,
         next_run_time, partition_unit, partition_period, is_create, create_drop_interval,
         job_schedule, database_target_id)
      VALUES (%(id)s, %(name)s, true, 'itest', %(t)s, %(cfg)s::jsonb, '1 day',
              clock_timestamp()::timestamp + %(due)s::interval, 'day', 1, %(c)s, '1 month',
              '0 0 2 * * *',
              (SELECT target_id FROM mubasher_oms.partops_database_target WHERE target_key = %(k)s))
    """, {"id": job_id, "name": "IT_%s" % job_id, "t": table, "cfg": config, "due": due_in,
          "c": is_create, "k": target_key})
    return q1("SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = %(i)s",
              {"i": job_id})


def calls(table: str, db: str) -> int:
    return int(q1("SELECT count(*) FROM partops_test.worker_calls WHERE table_name = %(t)s",
                  {"t": table}, db) or 0)


def attempt(attempt_id: int) -> dict:
    with su() as c:
        c.row_factory = psycopg.rows.dict_row
        return c.execute("SELECT * FROM mubasher_oms.partops_execution_attempt WHERE attempt_id = %(a)s",
                         {"a": attempt_id}).fetchone()


class CommitLost:
    """Target connection wrapper: COMMIT 'fails' like a dropped connection."""

    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    def commit(self):
        self._c.close()  # server aborts the open transaction; client cannot know
        raise psycopg.OperationalError("connection lost during COMMIT (simulated)")


def part_a() -> None:
    from database_targets import DatabaseTarget, fetch_target_rows, load_targets_from_rows
    from scheduler_backend import scheduler_database as sdb
    from scheduler_backend.models import SchedulerConfig
    from scheduler_backend.remote_execution import (
        FAILED, NOT_DISPATCHED, SUCCEEDED, UNKNOWN, ControlLedger, RemoteClaim,
        run_target_transaction,
    )

    cfg = SchedulerConfig()
    ex = "itest-executor"
    with psycopg.connect(host="127.0.0.1", port=PORT, dbname=CONTROL_DB,
                         user="partition_job_scheduler") as c:
        targets = {t.target_key: t for t in load_targets_from_rows(fetch_target_rows(c)).values()}
    ledger = ControlLedger(
        mark_dispatching=lambda a: sdb.remote_mark_dispatching(cfg, a, ex),
        record_success=lambda a, d: sdb.remote_record_success(cfg, a, d),
        record_failure=lambda a, cl, s, d: sdb.remote_record_failure(cfg, a, cl, s, d),
        mark_unknown=lambda a, s: sdb.remote_mark_unknown(cfg, a, s),
    )

    def claim(job_id, expected, kind="SCHEDULED", **kw):
        return sdb.remote_claim(cfg, job_id, expected, kind, ex, **kw)

    def execute(job_id, row, target="db02", connect=None):
        return run_target_transaction(targets[target], RemoteClaim.from_row(job_id, row), ledger,
                                      connect=connect)

    print("== A0 discovery (control DB only; enabled targets)")
    t_due = new_job(2001, "db02", "rem_orders")
    new_job(2008, "db03", "disabled_t")
    rows = sdb.fetch_upcoming_jobs(SchedulerConfig(lookahead_seconds=600))
    keys = {(j.job_id, j.target_key) for j in rows}
    check((2001, "db02") in keys, "partops_get_upcoming_jobs returns the db02 job with target_key")
    check(all(j.job_id != 2008 for j in rows), "job of DISABLED target db03 not discovered")

    print("== A1 remote success: routing + target COMMIT + control finalization")
    r = claim(2001, t_due)
    check(r["decision"] == "CLAIMED" and r["attempt_state"] == "PREPARED", "claim CLAIMED (PREPARED)")
    out = execute(2001, r)
    check(out.kind == SUCCEEDED and out.ledger_recorded,
          "target COMMIT confirmed ({} {} {})".format(out.kind, out.error_class, out.summary))
    check(attempt(r["attempt_id"])["state"] == "TARGET_SUCCEEDED", "ledger TARGET_SUCCEEDED before finalization")
    fin = sdb.remote_finalize(cfg, r["attempt_id"])
    check(fin["decision"] == "FINALIZED" and fin["result"] == "SUCCESS", "control finalization FINALIZED/SUCCESS")
    check(calls("rem_orders", TARGET2_DB) == 1 and calls("rem_orders", CONTROL_DB) == 0,
          "db02 job touched db02 only")
    log = q("SELECT last_run_status, database_target_id, execution_attempt_id FROM "
            "mubasher_oms.partitioning_job_table_log WHERE job_id = 2001")
    check(log == [("SUCCESS", targets["db02"].target_id, r["attempt_id"])], "history row SUCCESS with target + attempt")
    nxt = q1("SELECT next_run_time > clock_timestamp()::timestamp AND last_run_status = 'SUCCESS' "
             "FROM mubasher_oms.partitioning_job_table WHERE job_id = 2001")
    check(bool(nxt), "next_run_time advanced to the next legitimate occurrence; last status SUCCESS")
    check(sdb.remote_finalize(cfg, r["attempt_id"])["decision"] == "ALREADY_FINALIZED", "finalize idempotent")

    print("== A2 worker raises + rollback confirmed → bounded retry → exhaustion")
    t2 = new_job(2002, "db02", "fail_orders")
    r = claim(2002, t2)
    out = execute(2002, r)
    a = attempt(r["attempt_id"])
    check(out.kind == FAILED and a["state"] == "TARGET_FAILED" and a["failure_count"] == 1,
          "known failure: TARGET_FAILED, failure_count=1")
    check(calls("fail_orders", TARGET2_DB) == 0, "target transaction rolled back (no committed worker row)")
    check(q1("SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 2002") == t2,
          "schedule NOT advanced after a retryable failure")
    r2 = claim(2002, t2)
    check(r2["decision"] == "CLAIMED" and r2["attempt_id"] == r["attempt_id"], "retry reuses the same attempt row")
    execute(2002, r2)
    rec = sdb.reconcile_occurrence(cfg, 2002, t2, "RETRY_EXHAUSTED", "itest exhausted")
    a = attempt(r["attempt_id"])
    check(rec["status"] == "MISSED_RESCHEDULED", "exhaustion reconciled (no DDL), next occurrence scheduled")
    check(a["state"] == "FINALIZED" and a["result"] == "RETRY_EXHAUSTED" and a["failure_count"] == 2,
          "attempt FINALIZED RETRY_EXHAUSTED")
    att = q("SELECT reason, database_target_id, execution_attempt_id FROM mubasher_oms.partition_job_attention "
            "WHERE job_id = 2002")
    check(att == [("RETRY_EXHAUSTED", targets["db02"].target_id, r["attempt_id"])], "attention RETRY_EXHAUSTED with target/attempt")
    check(q1("SELECT last_run_status FROM mubasher_oms.partitioning_job_table WHERE job_id = 2002") == "FAIL",
          "job last_run_status FAIL after exhausted worker failures")

    print("== A3 connection dies around COMMIT → OUTCOME_UNKNOWN, never retried")
    t3 = new_job(2003, "db02", "rem_unknown")
    r = claim(2003, t3)
    import database_targets as dt
    real_connect = psycopg.connect
    out = execute(2003, r, connect=lambda **k: CommitLost(real_connect(**k)))
    a = attempt(r["attempt_id"])
    check(out.kind == UNKNOWN and a["state"] == "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN recorded")
    check(q1("SELECT next_run_time IS NULL FROM mubasher_oms.partitioning_job_table WHERE job_id = 2003"),
          "automatic scheduling of the job paused")
    check(q1("SELECT count(*) FROM mubasher_oms.partition_job_attention WHERE job_id = 2003 "
             "AND reason = 'REMOTE_OUTCOME_UNKNOWN' AND execution_attempt_id = %(a)s", {"a": r["attempt_id"]}) == 1,
          "REMOTE_OUTCOME_UNKNOWN attention item")
    check(claim(2003, t3)["decision"] == "NEEDS_REVIEW", "re-claim of the occurrence → NEEDS_REVIEW (no dispatch)")
    check(claim(2003, None, "MANUAL")["decision"] == "NEEDS_REVIEW", "manual run refused while unresolved")
    check(q1("SELECT count(*) FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 2003 "
             "AND last_run_status = 'OUTCOME_UNKNOWN'") == 1, "history shows OUTCOME_UNKNOWN")
    for label, sql in (
            ("table change refused (55006)",
             "UPDATE mubasher_oms.partitioning_job_table SET table_name = 'x' WHERE job_id = 2003"),
            ("target change refused (55006)",
             "SELECT mubasher_oms.partops_set_job_target(2003, mubasher_oms.partops_control_target_id())")):
        try:
            as_role("partition_job_ui" if "partops_set_job_target" in sql else "postgres", sql)
            check(False, label)
        except psycopg.Error as exc:
            check(exc.sqlstate == "55006", label)
    q("UPDATE mubasher_oms.partitioning_job_table SET is_enabled = false WHERE job_id = 2003")
    check(True, "disable allowed while unknown")
    try:
        q("UPDATE mubasher_oms.partitioning_job_table SET is_enabled = true WHERE job_id = 2003")
        check(False, "re-enable refused while unknown")
    except psycopg.Error as exc:
        check(exc.sqlstate == "55006", "re-enable refused while unknown")
    att_id = q1("SELECT attention_id FROM mubasher_oms.partition_job_attention WHERE job_id = 2003")
    try:
        as_role("partition_job_ui", "SELECT * FROM mubasher_oms.acknowledge_partition_job_attention(%(a)s, 'x')",
                {"a": att_id})
        check(False, "acknowledge refused before resolution")
    except psycopg.Error as exc:
        check(exc.sqlstate == "22023", "acknowledge refused before resolution")
    res = as_role("partition_job_ui",
                  "SELECT result FROM mubasher_oms.partops_resolve_remote_outcome(%(a)s, 'NOT_APPLIED', "
                  "'itest: partition absent on db02')", {"a": r["attempt_id"]})
    check(res == [("RESOLVED_NOT_APPLIED",)], "operator resolution (UI role) finalizes the attempt")
    check(q1("SELECT status FROM mubasher_oms.partition_job_attention WHERE attention_id = %(a)s",
             {"a": att_id}) == "ACKNOWLEDGED", "attention acknowledged by resolution")
    check(calls("rem_unknown", TARGET2_DB) == 0, "nothing re-executed on the target")

    print("== A4 commit succeeds + control finalization fails → finalization only")
    t4 = new_job(2004, "db02", "rem_fin")
    r = claim(2004, t4)
    out = execute(2004, r)
    check(out.kind == SUCCEEDED and attempt(r["attempt_id"])["state"] == "TARGET_SUCCEEDED",
          "target committed; finalization 'failed' (not called)")
    check(claim(2004, t4)["decision"] == "FINALIZE_PENDING", "re-claim → FINALIZE_PENDING (no dispatch)")
    rec = sdb.remote_recover_attempts(cfg, "itest-restart")
    acts = {row["attempt_id"]: row["action"] for row in rec}
    check(acts.get(r["attempt_id"]) == "FINALIZE", "restart recovery: TARGET_SUCCEEDED → FINALIZE")
    fin = sdb.remote_finalize(cfg, r["attempt_id"])
    check(fin["decision"] == "FINALIZED", "finalization retried successfully")
    check(calls("rem_fin", TARGET2_DB) == 1, "target DDL executed exactly once (no redispatch)")

    print("== A5 scheduler restart during DISPATCHING → OUTCOME_UNKNOWN, no redispatch")
    t5 = new_job(2005, "db02", "rem_crash")
    r = claim(2005, t5)
    check(sdb.remote_mark_dispatching(cfg, r["attempt_id"], ex) == "DISPATCHING", "attempt DISPATCHING (process dies here)")
    rec = sdb.remote_recover_attempts(cfg, "itest-restart-2")
    acts = {row["attempt_id"]: row["action"] for row in rec}
    check(acts.get(r["attempt_id"]) == "MARKED_UNKNOWN", "restart recovery marks DISPATCHING as UNKNOWN")
    check(attempt(r["attempt_id"])["state"] == "OUTCOME_UNKNOWN", "ledger OUTCOME_UNKNOWN")
    check(claim(2005, t5)["decision"] == "NEEDS_REVIEW" and calls("rem_crash", TARGET2_DB) == 0,
          "never redispatched")

    print("== A6 control-target job keeps the local executor and touches db01 only")
    t6 = new_job(2006, "db01", "loc_orders")
    st = as_role("partition_job_scheduler",
                 "SELECT status FROM mubasher_oms.run_partition_job_scheduled(2006, %(t)s)", {"t": t6})
    check(st == [("EXECUTED",)], "local run_partition_job_scheduled EXECUTED")
    check(calls("loc_orders", CONTROL_DB) == 1 and calls("loc_orders", TARGET2_DB) == 0, "db01 job touched db01 only")
    check(claim(2006, None, "MANUAL")["decision"] == "NOT_REMOTE", "remote claim refuses control jobs")

    print("== A7 disabled and unreachable targets")
    check(claim(2008, q1("SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id=2008"))
          ["decision"] == "SKIPPED_TARGET_DISABLED", "claim on disabled target refused")
    t7 = new_job(2007, "db04", "unreach_t")
    r = claim(2007, t7)
    out = execute(2007, r, target="db04")
    a = attempt(r["attempt_id"])
    check(out.kind == NOT_DISPATCHED and out.error_class == "UNREACHABLE", "unreachable → NOT_DISPATCHED")
    check(a["state"] == "TARGET_FAILED" and a["dispatch_started_at"] is None, "never dispatched (retry-eligible)")

    print("== A8 target-side table lock (per target, shared key with local executors)")
    t9 = new_job(2009, "db02", "locked_t")
    holder = su(TARGET2_DB)
    holder.execute("SELECT pg_advisory_lock(87421032, (abs(hashtext('partition_job_target:itest.locked_t')::bigint) % 2147483647)::integer)")
    r = claim(2009, t9)
    out = execute(2009, r)
    check(out.kind == NOT_DISPATCHED and out.error_class == "TARGET_LOCKED", "busy table on db02 → TARGET_LOCKED")
    t10 = new_job(2010, "db01", "locked_t")
    st = as_role("partition_job_scheduler",
                 "SELECT status FROM mubasher_oms.run_partition_job_scheduled(2010, %(t)s)", {"t": t10})
    check(st == [("EXECUTED",)], "same schema.table on db01 NOT blocked by db02's lock")
    holder.close()
    r = claim(2009, t9)
    check(execute(2009, r).kind == SUCCEEDED, "after release the db02 occurrence executes")

    print("== A9 manual remote run (engine path, schedule unchanged)")
    before = q1("SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 2001")
    r = claim(2001, None, "MANUAL")
    out = execute(2001, r)
    fin = sdb.remote_finalize(cfg, r["attempt_id"])
    check(out.kind == SUCCEEDED and fin["result"] == "MANUAL_SUCCESS", "manual remote MANUAL_SUCCESS")
    check(q1("SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 2001") == before,
          "manual run leaves next_run_time unchanged")
    new_job(2011, "db02", "drop_t", is_create=False)
    check(claim(2011, None, "MANUAL")["decision"] == "DROP_NOT_CONFIRMED", "manual DROP requires confirmation")
    check(claim(2011, None, "MANUAL", confirm_drop=True)["decision"] == "CLAIMED", "confirmed DROP claimed")

    print("== A10 privileges: app roles cannot write the ledger directly")
    for role in ("partition_job_scheduler", "partition_job_ui"):
        try:
            as_role(role, "UPDATE mubasher_oms.partops_execution_attempt SET state = 'PREPARED'")
            check(False, role + " UPDATE ledger refused")
        except psycopg.Error as exc:
            check(exc.sqlstate == "42501", role + " UPDATE ledger refused")
    try:
        as_role("partition_job_ui", "SELECT * FROM mubasher_oms.partops_remote_claim(2001, NULL, 'MANUAL', 'x')")
        check(False, "UI role cannot claim (scheduler-only)")
    except psycopg.Error as exc:
        check(exc.sqlstate == "42501", "UI role cannot claim (scheduler-only)")


def overlaps(rows):
    """Max number of simultaneously running stub calls in [(start, end)]."""
    events = sorted([(s, 1) for s, _ in rows] + [(e, -1) for _, e in rows])
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def run_scheduler(global_max: int, seconds: float):
    from scheduler_backend.models import SchedulerConfig
    from scheduler_backend.scheduler import PartitionScheduler

    cfg = SchedulerConfig(max_concurrent_jobs=global_max, lookahead_seconds=60, reconcile_seconds=1,
                          framework_check_enabled=True, framework_recheck_seconds=30,
                          target_cooldown_base_seconds=2)
    sched = PartitionScheduler(cfg)

    async def go():
        task = asyncio.create_task(sched.run())
        await asyncio.sleep(seconds)
        sched.request_shutdown()
        await asyncio.wait_for(task, timeout=60)
    asyncio.run(go())
    return sched


def windows(tables, db):
    return q("SELECT started_at, finished_at FROM partops_test.worker_calls "
             "WHERE table_name = ANY(%(t)s) AND finished_at IS NOT NULL", {"t": tables}, db)


def part_b() -> None:
    print("== B1 scheduler: global=2, db01=1, db02=1; db04 unreachable (isolation)")
    q("UPDATE mubasher_oms.partops_database_target SET max_concurrent_jobs = 1 WHERE target_key IN ('db01','db02')")
    for i, (k, t) in enumerate((("db01", "slow_c1"), ("db01", "slow_c2"), ("db02", "slow_r1"),
                                ("db02", "slow_r2"), ("db04", "unreach_b"))):
        new_job(3001 + i, k, t, due_in="4 seconds")
    sched = run_scheduler(2, 16)
    fw = sched.status.extra.get("framework_status")
    check(fw == "ready", "framework gate ready with test-only stubs (status={}, failures={})".format(
        fw, sched.status.extra.get("framework_failures")))
    c1 = windows(["slow_c1", "slow_c2"], CONTROL_DB)
    r1 = windows(["slow_r1", "slow_r2"], TARGET2_DB)
    check(len(c1) == 2 and len(r1) == 2, "db01 and db02 jobs all executed while db04 was down")
    check(overlaps(c1) == 1 and overlaps(r1) == 1, "per-target limit 1 respected on db01 and db02")
    check(overlaps(c1 + r1) == 2, "both targets ran simultaneously (global 2)")
    st = q1("SELECT state || ':' || COALESCE(error_class,'') FROM mubasher_oms.partops_execution_attempt "
            "WHERE job_id = 3005 ORDER BY attempt_id DESC LIMIT 1")
    check(st is not None and st.startswith("TARGET_FAILED:UNREACHABLE"), "db04 attempt TARGET_FAILED (UNREACHABLE), retry-eligible")
    tstat = sched.status.extra.get("targets", {})
    check(tstat.get("db04", {}).get("available") is False, "db04 in availability cooldown")
    check(tstat.get("db01", {}).get("available") is True and tstat.get("db02", {}).get("available") is True,
          "db01/db02 unaffected")
    ok_rows = q("SELECT count(*) FROM mubasher_oms.partitioning_job_table_log WHERE job_id BETWEEN 3001 AND 3004 "
                "AND last_run_status = 'SUCCESS'")[0][0]
    check(ok_rows == 4, "4 SUCCESS history rows (local + remote)")
    check(calls("unreach_b", TARGET2_DB) == 0 and calls("unreach_b", CONTROL_DB) == 0,
          "db04 job never executed anywhere else")

    print("== B2 scheduler: global=3, db01=1, db02=2")
    q("UPDATE mubasher_oms.partops_database_target SET max_concurrent_jobs = 2 WHERE target_key = 'db02'")
    q("UPDATE mubasher_oms.partops_database_target SET is_enabled = false WHERE target_key = 'db04'")
    for i, (k, t) in enumerate((("db01", "slow_c3"), ("db01", "slow_c4"), ("db02", "slow_r3"),
                                ("db02", "slow_r4"), ("db02", "slow_r5"))):
        new_job(3101 + i, k, t, due_in="4 seconds")
    run_scheduler(3, 18)
    c2 = windows(["slow_c3", "slow_c4"], CONTROL_DB)
    r2 = windows(["slow_r3", "slow_r4", "slow_r5"], TARGET2_DB)
    check(len(c2) == 2 and len(r2) == 3, "all jobs executed")
    check(overlaps(c2) == 1, "db01 limit 1")
    check(overlaps(r2) == 2, "db02 limit 2 (two concurrent)")
    check(overlaps(c2 + r2) <= 3 and overlaps(c2 + r2) == 3, "global limit 3 reached, never exceeded")


def main() -> int:
    t0 = time.time()
    part_a()
    part_b()
    print("REMOTE_EXECUTION", "OK" if not FAILURES else "FAILED ({})".format(len(FAILURES)),
          "in {:.0f}s".format(time.time() - t0))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
