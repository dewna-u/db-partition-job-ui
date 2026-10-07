# PartOps Issues

## ISSUE-001 — Scheduler repeatedly executes same overdue occurrence

Severity:
    Critical

Status:
    Fixed in source — production verification pending

Component:
    Realtime Scheduler
    (`scheduler_backend`, `mubasher_oms.run_partition_job_scheduled`)

Detected:
    Production observation while scheduler was running: after Job 33 reported
    `EXECUTED` for `expected_run_time ≈ 2026-09-18 00:15:00`, queue refresh
    rediscovered the same `job_id + expected_run_time` and executed again within
    seconds. Scheduler was stopped.

Description:
    The realtime scheduler re-processed the same scheduled occurrence
    `(job_id, expected_run_time)` in a tight loop after reporting EXECUTED for
    an overdue DROP job (Job 33).

Evidence:
    Runtime log pattern:
    - Timer fired for job_id=33 expected_run_time=2026-09-18 00:15:00
    - Scheduled job 33 result EXECUTED (DROP completed)
    - Queue refreshed
    - Next job again job_id=33 with the same expected_run_time
    - Immediate re-execution

    Live read-only Job 33 state (not modified):
    - `job_schedule = 0 15 0 * * 5` (Friday 00:15)
    - `frequency = 7 days`
    - `next_run_time = 2026-09-18 00:15:00` still present after EXECUTED logs
    - `last_run_status = FAIL`, `is_create = false`

Root Cause:
    Root cause confirmed from deployed source and live read-only inspection.

    Primary — transaction boundary bug in `open_connection()`:
    1. `SELECT current_database()` (DB identity check) ran with psycopg
       `autocommit=False`, starting an implicit outer transaction.
    2. `execute_scheduled_job()` then used `conn.transaction()`, which nested
       as a savepoint under that still-open outer transaction.
    3. `run_partition_job_scheduled` could return EXECUTED (worker DDL +
       next_run_time UPDATE + log INSERT inside the nested block).
    4. Closing the connection rolled back the uncommitted outer transaction,
       so next_run_time / log changes were not persisted.
    5. Refresh rediscovered the same occurrence → tight loop.

    Secondary contributing factors (also hardened in source):
    - SQL next-run validation / fail-closed behaviour for invalid schedules.
    - Python rapid-loop breaker (short TTL) for committed transitions only.
    - Python PsycopgError previously returned cacheable status `FAILED`
      (now `FAILED_DATABASE`, not cached).

    Not the cause of Job 33 loop: live call of
    `cron_to_interval_or_next_run('0 15 0 * * 5')` at ~2026-10-07 10:01 UTC
    correctly returned `next_run_time = 2026-10-09 00:15:00`
    (see ISSUE-003 — still not version-controlled, but not this incident).

Fix:
    - `open_connection()`: after identity assert, `conn.commit()` so the
      connection is idle before yield; `conn.transaction()` is then a real
      top-level business transaction.
    - PsycopgError / incomplete txn → `FAILED_DATABASE` (not cached).
    - SQL fail-closed + pre-DDL next validation (prior hardening retained).
    - Rapid-loop cache: EXECUTED / SQL FAILED / FAILED_INVALID_SCHEDULE only;
      TTL 300s, max 512.

Tests:
    Transaction-boundary tests in `test_scheduler_backend.py`;
    cache/status policy in `test_scheduler_phase0_safety.py`.

Deployment Notes:
    Deploy updated Python (`scheduler_database.py`, `scheduler.py`) AND the
    Phase 0 SQL migration body before starting the scheduler. Keep scheduler
    STOPPED until both are live. Fix production verification pending.

Remaining Risk:
    Production behaviour after controlled deploy not yet observed.
    Cron helper still not versioned (ISSUE-003).


## ISSUE-002 — Edit / Enable-Disable not covered in Phase 0

Severity:
    Medium (deferred)

Status:
    Open

Component:
    API / Jobs Edit path

Detected:
    Phase 0 scope explicitly excluded Edit/Enable-Disable feature work.

Description:
    Any remaining Edit or Enable/Disable product bugs are out of scope for
    Phase 0 and should be addressed in a later phase. Disabled-job execution
    safety at scheduled-run time remains enforced by
    `SKIPPED_DISABLED` in `run_partition_job_scheduled`.

Evidence:
    Phase 0 requirements; existing `SKIPPED_DISABLED` path in SQL.

Impact:
    Operators may still see Edit UX issues; scheduling safety for disabled rows
    at execution time is separate.

Root Cause:
    Deferred by phase scope.

Fix:
    Track for next feature phase.

Tests:
    Existing Edit API tests remain green; no Phase 0 Edit changes.

Deployment Notes:
    None for Phase 0.

Remaining Risk:
    Unrelated Edit defects may still exist.


## ISSUE-003 — Scheduler depends on non-version-controlled cron helper

Severity:
    Medium

Status:
    Open (not the Job 33 incident cause)

Component:
    Database framework function
    `mubasher_oms.cron_to_interval_or_next_run(text)`

Detected:
    Phase 0 hardening review; live read-only validation 2026-10-07.

Description:
    Scheduled execution depends on `cron_to_interval_or_next_run`, but the
    authoritative function body is not stored in this repository (bootstrap
    explicitly requires import from a reference DB).

Evidence:
    - No CREATE FUNCTION body under `sql/`.
    - Live read-only call with Job 33 schedule `0 15 0 * * 5` at
      ~2026-10-07 10:01:21 UTC returned
      `schedule_interval = NULL`, `next_run_time = 2026-10-09 00:15:00`
      (correct next Friday). Therefore the helper was **not** identified as
      the cause of the Job 33 tight loop (see ISSUE-001 transaction bug).

Impact:
    Local tests cannot fully validate helper semantics; future drift risk
    remains until the function is versioned. Not the proven Job 33 root cause.

Root Cause:
    Historical framework functions imported from a reference DB, never checked
    into this repo.

Fix:
    Future work should capture/version the authoritative definition safely.

Tests:
    N/A in-repo for live helper body.

Deployment Notes:
    Before scheduler start, verify
    `to_regprocedure('mubasher_oms.cron_to_interval_or_next_run(text)')`
    and review the live definition in a controlled DBA session.

Remaining Risk:
    Helper behaviour drift between environments until versioned.
