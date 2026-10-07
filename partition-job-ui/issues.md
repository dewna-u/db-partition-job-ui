# PartOps Issues

## ISSUE-001 — Scheduler repeatedly executes same overdue occurrence

Severity:
    Critical

Status:
    Fixed (source + tests; not yet deployed)

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
    Proven in source (without assuming production next_run_time row values):

    1. Primary — `run_partition_job_scheduled` accepted whatever
       `cron_to_interval_or_next_run` returned for `next_run_time` without
       requiring the new value to be:
       - distinct from `p_expected_run_time` (occurrence T), and
       - strictly in the future relative to post-lock `v_now`.
       If the helper returns T again, or another non-future timestamp, UPDATE
       can leave the job immediately rediscoverable by
       `get_upcoming_partition_jobs`, so post-execution refresh requeues the
       same occurrence.

    2. Secondary gap — the Python scheduler trusted refresh results and had no
       process-local guard against re-executing an occurrence it had already
       handled in this process lifetime.

Fix:
    - SQL: after cron helper calculation, reject NULL / same-as-T / `<= T` /
      `<= v_now` next times; advance using `frequency` or `now + 1 day`.
    - Python: secondary circuit breaker suppresses rediscovery/re-execution of
      already-handled `(job_id, expected_run_time)` for the process lifetime.
    - Logging: record expected_run_time and returned next_run_time on results.

Tests:
    `test_scheduler_phase0_safety.py` plus SQL contract updates.

Deployment Notes:
    Deploy updated `scheduler_backend` Python AND redeploy
    `run_partition_job_scheduled` via
    `sql/migrations/20261007_01_run_partition_job_scheduled_occurrence_safety.sql`
    (or equivalent body from `sql/realtime_scheduler_v1.sql`) BEFORE restarting
    `partition-job-scheduler.service`. Keep scheduler STOPPED until both are
    applied.

Remaining Risk:
    Authoritative body of `cron_to_interval_or_next_run` is not in this repo;
    fallback advancement may differ from ideal cron “next future tick” but is
    intentionally safe. Circuit breaker is process-local only (DB remains
    primary). Production verification still required after controlled deploy.


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
