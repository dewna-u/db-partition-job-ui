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


## ISSUE-002 — Edit / Enable-Disable persistence (privilege + update security)

Severity:
    High (operations)

Status:
    Fixed in source — production verification pending (Phase 4)

Component:
    API / Jobs Edit path /
    `mubasher_oms.update_data_to_partition_job_table`

Detected:
    Operators reported Enable → Disable (and Edit) not persisting correctly.
    Deferred through Phases 0–3; addressed in Phase 4.

Description:
    UI Edit / Enable / Disable call `PATCH /api/jobs/{id}` →
    `update_data_to_partition_job_table(...)`. Updates often failed to persist
    under the least-privilege app role.

Root Cause (confirmed from repository SQL):
    Prior migration `20260929_02_update_data_to_partition_job_table.sql`
    defined the function as **SECURITY INVOKER** with:
    - no `OWNER TO mubasher_oms`
    - `GRANT EXECUTE … TO partition_job_ui` **commented out**
    - no fixed `search_path`
    - no `SELECT … FOR UPDATE`
    - trusted client `p_next_run_time` without fail-closed re-enable logic

    Under SECURITY INVOKER, `partition_job_ui` must hold direct `UPDATE` on
    `partitioning_job_table` for the function body to succeed. The intended
    model is EXECUTE-only on a SECURITY DEFINER function owned by
    `mubasher_oms` — not broad table UPDATE.

Fix:
    Migration `20261013_02_secure_update_partition_job.sql` + API returns
    refreshed job row; Jobs page refreshes selected row after save.

Remaining limitation (documented, not a bug):
    Disable prevents future scheduled executions (`SKIPPED_DISABLED`).
    It does **not** forcibly terminate a DDL worker that has already entered
    its DB transaction.

Tests:
    `test_phase4_secure_update.py` + existing Edit API tests.

Deployment Notes:
    Apply Phase 4 SQL; restart API; do not grant table UPDATE to
    `partition_job_ui`.


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


## ISSUE-004 — Local partops_dev bootstrap blocked (auth + missing function bodies)

Severity:
    High (blocks continuous localhost scheduler development)

Status:
    Open — waiting on local Postgres password + missing function sources

Component:
    Local development / `sql/local_dev` / `scripts/bootstrap-local-partops-dev.ps1`

Detected:
    2026-10-09 laptop bootstrap attempt.

Description:
    Local PostgreSQL 16 is running on `127.0.0.1:5432` with
    `scram-sha-256` authentication. No local admin password / `.pgpass` is
    available to the agent, so `partops_dev` could not be created yet.

    Separately, even after auth succeeds, these framework functions still have
    **no version-controlled CREATE bodies** in the repo (same gap as ISSUE-003
    plus workers/insert):

    - `cron_to_interval_or_next_run(text)`
    - `create_any_table_partition(...)`
    - `drop_any_table_partition(...)`
    - `insert_data_to_partition_job_table(...)`

    Local bootstrap must not invent production-authoritative bodies. Phase 0
    live inspection proved cron helper behaviour for one Job 33 expression but
    did not capture its source into Git.

Evidence:
    - `psql -w -h 127.0.0.1 ...` → `fe_sendauth: no password supplied`
    - `pg_hba.conf` uses `scram-sha-256` for localhost
    - `sql/bootstrap_partition_job_framework.sql` documents missing imports

Impact:
    API/UI can run without DB; scheduler and DB-backed API routes cannot be
    exercised end-to-end on the laptop until bootstrap completes.

Fix (operator):
    1. Provide LOCAL PostgreSQL admin password via session env
       `PARTOPS_LOCAL_PG_ADMIN_PASSWORD` (never commit).
    2. Run `.\scripts\bootstrap-local-partops-dev.ps1`.
    3. Supply CREATE FUNCTION bodies from a safe capture **or** explicitly
       approve clearly marked LOCAL-ONLY stubs (not for production).

Deployment Notes:
    N/A — laptop only.

Remaining Risk:
    Using company DB credentials or inventing cron/worker bodies would
    undermine safety; both are refused by design.


## ISSUE-005 — Manual run vs scheduled target-table concurrency

Severity:
    Medium

Status:
    Open — documented; not in Phase 2 scope

Component:
    `mubasher_oms.run_partition_job_manual` vs
    `mubasher_oms.run_partition_job_scheduled`

Detected:
    2026-10-13 Phase 2 design review (local source inspection).

Description:
    Phase 2 adds a transaction-scoped target-table advisory lock
    (`87421032`) inside `run_partition_job_scheduled` so two scheduled jobs
    cannot CREATE/DROP the same `(table_schema, table_name)` concurrently.

    `run_partition_job_manual(numeric)` does **not** acquire that target lock.
    A UI/API manual run against the same table could therefore overlap a
    scheduled worker for that table.

Evidence:
    - Scheduled function (canonical +
      `20261013_01_scheduled_target_advisory_lock.sql`) uses
      `pg_try_advisory_xact_lock(87421032, …)`.
    - Manual migration
      `20260929_03_run_partition_job_manual_duration.sql` has no target
      advisory lock.

Impact:
    Rare in normal operations (manual runs are operator-driven), but DDL
    against the same parent table from manual + scheduled paths is not
    mutually excluded at the DB lock layer.

Fix (future, small scoped change):
    Add the same target-table `pg_try_advisory_xact_lock` / controlled skip
    or wait policy to `run_partition_job_manual`, with migration + tests,
    preserving owner/EXECUTE grants.

Deployment Notes:
    Not required for Phase 2 scheduler concurrency rollout. Prefer
    avoiding manual runs on a table while its scheduled job is due.


## ISSUE-006 — Production Python 3.9 and boto3 support horizon

Severity:
    Low (technical follow-up; not a Datadog defect)

Status:
    Open — track for planned runtime upgrade

Component:
    Production Python runtime / `boto3` (Phase 1 Secrets Manager)

Detected:
    2026-10-13 operational review (production remains on Python 3.9).

Description:
    PartOps production currently runs Python 3.9. Upstream AWS SDK / boto3
    support windows eventually drop older Python versions. This is unrelated
    to Phase 3 Datadog work (stdlib DogStatsD), but should be scheduled so
    Secrets Manager credential resolution remains supported.

Impact:
    Future `boto3` / botocore releases may stop supporting Python 3.9,
    blocking security updates for the AWS credential path.

Fix (future):
    Plan a controlled upgrade of the production venv / system Python
    (e.g. 3.11+) with regression of the full unittest suite and AWS-mode
    smoke checks. Keep PartOps source Python 3.9-compatible until the
    runtime is upgraded.

Deployment Notes:
    No change required for Phase 3 observability rollout.
