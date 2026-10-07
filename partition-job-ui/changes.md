# PartOps Changes

## CHANGE-001 — Scheduler scheduled-occurrence safety fix

Date:
    2026-10-07

Reason:
    Prevent repeated execution of the same scheduled occurrence
    `(job_id, expected_run_time)` after overdue jobs are processed (ISSUE-001).

Related Issues:
    ISSUE-001

Files Changed:
    - `sql/realtime_scheduler_v1.sql`
    - `sql/migrations/20261007_01_run_partition_job_scheduled_occurrence_safety.sql` (new)
    - `sql/migrations/README.md`
    - `scheduler_backend/scheduler.py`
    - `test_scheduler_phase0_safety.py` (new)
    - `issues.md` (new)
    - `changes.md` (new)
    - `README.md`
    - `RUNBOOK.md`

Database Changes:
    `CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone)`
    — same signature/return type; adds post-calculation validation so
    `next_run_time` becomes a future timestamp distinct from
    `p_expected_run_time`. Fallback uses `frequency` when valid, else
    `clock_timestamp() + interval '1 day'`.

    Transaction flow (unchanged structure, clarified):
    1. Python opens short-lived connection; `conn.transaction()` begins.
    2. SQL: advisory xact lock → `SELECT … FOR UPDATE` → validate enabled/stale/due.
    3. Apply `db_config_para` + CREATE/DROP worker in same transaction.
    4. Compute/validate `next_run_time`, UPDATE job row, INSERT log row.
    5. Function returns; Python commits transaction; connection closes.
    Worker success and schedule transition commit together.

Configuration Changes:
    None. Ports unchanged. `.env` / `.env.realtime` unchanged.

Security Impact:
    No credential changes. API still must not load `.env.realtime`.
    Scheduler still loads `.env.realtime` only.

Behavioural Impact:
    - Overdue semantics: execute the discovered occurrence once, then advance to
      a next **future** occurrence (no miss-by-miss catch-up storm).
    - Stale timer (`expected_run_time` ≠ locked `next_run_time`): still
      `SKIPPED_RESCHEDULED`, no worker.
    - Disabled at execution time: still `SKIPPED_DISABLED`, no worker.
    - Python secondary circuit breaker refuses rediscovery/re-execution of
      handled occurrences for the process lifetime.
    - Connection failures do **not** suppress retry (transaction likely aborted).

Tests:
    Full suite via `python -m unittest discover -s . -p "test_*.py" -v`
    including `test_scheduler_phase0_safety.py`.

Deployment Steps (DO NOT execute in this change set):
    1. Keep `partition-job-scheduler.service` STOPPED.
    2. Deploy Python source (at least `scheduler_backend/scheduler.py`).
    3. Apply
       `sql/migrations/20261007_01_run_partition_job_scheduled_occurrence_safety.sql`
       in a DBA window (or redeploy matching body from
       `sql/realtime_scheduler_v1.sql`).
    4. Verify
       `to_regprocedure('mubasher_oms.run_partition_job_scheduled(numeric,timestamp without time zone)')`
       is non-NULL.
    5. Confirm function source contains the non-future next-run rejection clause.
    6. Restart API only if needed for unrelated reasons (not required for this
       scheduler fix).
    7. Start scheduler only after SQL + Python are both live; watch journal for
       first jobs and circuit-breaker CRITICAL lines.

Rollback:
    - Python: restore prior `scheduler_backend/scheduler.py` from Git or from
      pre-Phase-0 ZIP backup
      `partition-job-ui-before-phase0-20261007-132137.zip`.
    - SQL: restore previous
      `run_partition_job_scheduled` body from the last known-good deployment
      artifact / reference DB dump (CREATE OR REPLACE). Keep scheduler STOPPED
      until rollback is verified.

Notes:
    No AWS Secrets Manager, Datadog, or parallel execution changes.
    No `.env` / `.env.realtime` edits.
