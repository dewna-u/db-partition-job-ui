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
    The realtime scheduler appeared to re-process the same scheduled occurrence
    `(job_id, expected_run_time)` in a tight loop after a successful DROP/CREATE
    attempt on an overdue job.

Evidence:
    Runtime log pattern equivalent to:
    - Timer fired for job_id=33 expected_run_time=2026-09-18 00:15:00
    - Scheduled job 33 result EXECUTED (DROP completed)
    - Queue refreshed
    - Next job again job_id=33 with the same expected_run_time
    - Immediate re-execution

Impact:
    Uncontrolled repeated partition DDL for the same occurrence; risk of load,
    lock contention, and inconsistent operational history. Scheduler had to be
    left STOPPED.

Root Cause:
    Source-level defect consistent with the observed production incident and
    reproduced by regression tests (production DB row state was not re-queried
    in this investigation):

    1. Primary — `run_partition_job_scheduled` accepted cron-helper next-run
       values without requiring a future timestamp distinct from occurrence T,
       and originally calculated next-run after workers. Invalid next state
       could leave the job rediscoverable by `get_upcoming_partition_jobs`.

    2. Secondary — Python had no bounded process-local guard against
       re-executing a recently handled `(job_id, expected_run_time)`.

Fix:
    - SQL: validate next occurrence **before** CREATE/DROP; reject NULL /
      same-as-T / `<= T` / `<= now`; **fail closed** with
      `FAILED_INVALID_SCHEDULE`, `next_run_time = NULL`, no worker, no invented
      `+1 day` or frequency fallback.
    - Python: short-lived rapid-loop circuit breaker (TTL 300s, max 512)
      caching only committed occurrence transitions (`EXECUTED`, `FAILED`,
      `FAILED_INVALID_SCHEDULE`). Ordinary skips are not cached.
    - Logging: expected_run_time and returned next_run_time on results.

Tests:
    `test_scheduler_phase0_safety.py` plus SQL contract updates.

Deployment Notes:
    Deploy updated `scheduler_backend` Python AND redeploy
    `run_partition_job_scheduled` via
    `sql/migrations/20261007_01_run_partition_job_scheduled_occurrence_safety.sql`
    (body must match `sql/realtime_scheduler_v1.sql`) BEFORE restarting
    `partition-job-scheduler.service`. Keep scheduler STOPPED until both are
    applied. Production verification is still pending.

Remaining Risk:
    Authoritative body of `cron_to_interval_or_next_run` is not in this repo
    (see ISSUE-003). Fail-closed clears `next_run_time` until an operator
    corrects the schedule. Rapid-loop breaker is process-local, TTL 300s,
    and does not suppress SKIPPED_*/NOT_FOUND (DB remains authoritative).
    Live production behaviour must still be confirmed after controlled deploy.


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
    High

Status:
    Open

Component:
    Database framework function
    `mubasher_oms.cron_to_interval_or_next_run(text)`

Detected:
    Phase 0 hardening review.

Description:
    Scheduled execution depends on `cron_to_interval_or_next_run`, but the
    authoritative function body is not stored in this repository (bootstrap
    explicitly requires import from a reference DB). Local tests cannot fully
    validate its production semantics.

Evidence:
    `sql/bootstrap_partition_job_framework.sql` documents the function as
    missing/import-only; no CREATE FUNCTION body exists under `sql/`.

Impact:
    Next-run validation can fail closed if the helper returns invalid values,
    but operators cannot review the helper’s source in Git. Deployment must
    inspect the live definition before starting the scheduler.

Root Cause:
    Historical framework functions were imported from a reference database and
    never checked into this repo.

Fix:
    Future work should capture/version the authoritative definition safely
    (without inventing a new implementation in this task).

Tests:
    N/A in-repo (cannot execute live helper here).

Deployment Notes:
    Before scheduler start, verify
    `to_regprocedure('mubasher_oms.cron_to_interval_or_next_run(text)')`
    and review the live function definition in a controlled DBA session.

Remaining Risk:
    Helper behaviour drift between environments remains possible until versioned.
