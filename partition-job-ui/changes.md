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
    Superseded/hardened by CHANGE-002 for fail-closed next-run semantics.


## CHANGE-002 — Phase 0 scheduler safety hardening

Date:
    2026-10-07

Reason:
    Correct Phase 0 after final review: remove invented `+1 day` / frequency
    fallbacks, fail closed on invalid next schedule, bound the Python circuit
    breaker, and align migration/canonical SQL. Prior Phase 0 commit already
    pushed; this is an additive correction (no history rewrite).

Related Issues:
    ISSUE-001, ISSUE-003

Files Changed:
    - `sql/realtime_scheduler_v1.sql`
    - `sql/migrations/20261007_01_run_partition_job_scheduled_occurrence_safety.sql`
    - `sql/migrations/README.md`
    - `scheduler_backend/scheduler.py`
    - `test_scheduler_phase0_safety.py`
    - `test_scheduler_backend.py`
    - `issues.md`
    - `changes.md`
    - `README.md`
    - `RUNBOOK.md`

Database Changes:
    `CREATE OR REPLACE` of `run_partition_job_scheduled` (same signature):
    - Compute/validate next occurrence **before** CREATE/DROP.
    - Accept only future `N` with `N IS DISTINCT FROM T` and `N > NOW`.
    - On invalid/NULL helper result: `FAILED_INVALID_SCHEDULE`,
      `next_run_time = NULL`, log FAIL, **no worker**, no `+1 day` /
      frequency invention.
    - `is_create IS NULL` also fail-closed with `next_run_time = NULL`.

Configuration Changes:
    None.

Security Impact:
    None beyond safer scheduling control (no credential changes).

Behavioural Impact:
    - Invalid schedule stops rediscovery (`get_upcoming` requires non-NULL
      `next_run_time`) until an operator sets a valid next time.
    - Valid schedules still execute CREATE/DROP once, then advance to the
      validated future next.
    - Rapid-loop breaker: OrderedDict + TTL **300s** + max **512** entries.
      Caches only committed occurrence transitions: `EXECUTED`, `FAILED`,
      `FAILED_INVALID_SCHEDULE`. Does **not** cache `FAILED_CONNECTION`,
      `SKIPPED_DISABLED`, `SKIPPED_RESCHEDULED`, `NOT_FOUND`, or connection
      exceptions — DB remains authoritative for ordinary skips.

Tests:
    Full suite; migration body must match canonical function extract.

Deployment Steps (NOT executed here):
    1. Keep scheduler STOPPED.
    2. Deploy Python `scheduler_backend/scheduler.py`.
    3. Apply updated
       `20261007_01_run_partition_job_scheduled_occurrence_safety.sql`.
    4. Verify live `cron_to_interval_or_next_run(text)` exists (ISSUE-003).
    5. Start scheduler only after SQL + Python are both live.

Rollback:
    Restore previous function body + previous `scheduler.py` from Git or
    hardening backup ZIP; keep scheduler stopped until verified.

Notes:
    No production DB access, no migrations executed, no services restarted,
    no Git history rewrite, no commit/push from this agent turn.
    Extended by CHANGE-003 (transaction boundary / FAILED_DATABASE).


## CHANGE-003 — Phase 0 transaction-boundary and FAILED_DATABASE correction

Date:
    2026-10-07

Reason:
    Live read-only inspection + deployed-source review confirmed the Job 33
    tight loop primary cause: DB identity `SELECT current_database()` left an
    open psycopg transaction so scheduled work nested under a savepoint and
    rolled back on connection close. Also stop mapping PsycopgError to
    cacheable `FAILED`.

Related Issues:
    ISSUE-001 (primary), ISSUE-003 (cron helper not the Job 33 cause)

Files Changed:
    - `scheduler_backend/scheduler_database.py`
    - `scheduler_backend/scheduler.py` (cache policy comments / FAILED_DATABASE)
    - `test_scheduler_backend.py`
    - `test_scheduler_phase0_safety.py`
    - `issues.md`
    - `changes.md`
    - `README.md`
    - `RUNBOOK.md`

Database Changes:
    None in this correction (prior Phase 0 SQL hardening retained unchanged).

Configuration Changes:
    None.

Behavioural Impact:
    - After identity check, `open_connection()` commits so the connection is
      idle before yield; `execute_scheduled_job()` `conn.transaction()` is a
      real top-level COMMIT/ROLLBACK for the occurrence.
    - Python DB/transaction errors return `FAILED_DATABASE` (not cached).
    - Rapid-loop cache still: EXECUTED / SQL FAILED / FAILED_INVALID_SCHEDULE
      only; TTL 300s; max 512.
    - Live cron helper for Job 33 schedule correctly returned next Friday;
      not treated as incident root cause.

Tests:
    Identity-check commit-before-yield; PsycopgError → FAILED_DATABASE;
    FAILED_DATABASE / SKIPPED_* / NOT_FOUND uncached; full suite.

Deployment Steps (NOT executed here):
    1. Keep scheduler STOPPED.
    2. Pull/deploy updated Python (`scheduler_database.py`, `scheduler.py`).
    3. Ensure Phase 0 SQL migration body already applied (or apply matching
       `realtime_scheduler_v1.sql` function).
    4. Verify cron helper exists; start scheduler only after Python+SQL match.

Rollback:
    Restore prior `scheduler_database.py` / `scheduler.py` from Git or backup
    ZIP; keep scheduler stopped until verified.

Notes:
    No server pull, no migration execution, no service restart, no commit/push
    from this agent turn.
