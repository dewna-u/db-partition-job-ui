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

Update (2026-11-10, Phase 5A):
    The Job 33 *committed-success* loop above was fixed in Phase 0. A related
    but different loop for UNcommitted outcomes (FAILED_DATABASE,
    FAILED_CONNECTION, SKIPPED_TARGET_LOCKED) remained — see ISSUE-007.


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


Update 2026-11-17 (Phase 5C):
    Phase 5B.1 (import of the authoritative captured definitions) is
    DEFERRED. The only authoritative source is production, and production
    capture is not permitted. No definition was reconstructed or invented;
    this issue stays OPEN. Phase 5C does not depend on the bodies (its SQL
    guards return before any worker is called).

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
    Fixed in source (Phase 5A, 2026-11-10) — production verification pending.
    `run_partition_job_manual` now takes the same `pg_try_advisory_xact_lock(
    87421032, …)` with an identical key; a busy target returns
    `MANUAL_TARGET_LOCKED` (nothing executed, no log row; API HTTP 409).
    Migration `20261110_02_missed_run_reconcile_retry_and_manual_lock.sql`.

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


## ISSUE-007 — Scheduler immediate retry loop on uncommitted outcomes (CPU)

Severity:
    Critical

Status:
    Fixed in source (Phase 5A, 2026-11-10) — production verification pending.

Component:
    Realtime Scheduler (`scheduler_backend/scheduler.py`,
    `scheduler_backend/retry_policy.py`, `run_partition_job_scheduled`)

Detected:
    2026-11-09 onboarding audit (source inspection) — matches the historical
    high-CPU / ~19k `partops.job.execution` events observation and the live
    `psycopg.InterfaceError: unexpected DateStyle: Redwood, SHOW_TIME` trigger.

Description:
    For an overdue occurrence whose outcome the database did NOT commit
    (`FAILED_DATABASE`, `FAILED_CONNECTION`) or that was skipped because the
    target table was busy (`SKIPPED_TARGET_LOCKED`), `next_run_time` stayed
    unchanged and the outcome was deliberately not cached. Task completion
    requested a refresh, the refresh rediscovered the same overdue occurrence
    with `delay <= 0`, and it was dispatched again immediately — no delay, no
    attempt limit. Each cycle opened ~3 connections and, for FAILED_DATABASE,
    re-ran the partition DDL and rolled it back. The refresh-failure backoff
    only applied when the refresh itself failed.

Root Cause:
    No per-occurrence retry state; refresh path had no minimum interval;
    overdue occurrences were always executed immediately.
    DateStyle trigger: a transaction-local DateStyle change (db_config_para or
    a worker) made psycopg's text timestamp loader reject the RESULT row after
    the work ran → exception → rollback → FAILED_DATABASE → loop.

Fix (source):
    - `OccurrenceRetryTracker` keyed by `(job_id, expected_run_time)`:
      FAILED_DATABASE/FAILED_CONNECTION → 5 s, 15 s, 30 s, then exhausted
      (4 attempts total); exhausted occurrences are never executed again — the
      DB `reconcile_missed_partition_job(..., 'RETRY_EXHAUSTED')` records a
      review item and advances `next_run_time`.
    - SKIPPED_TARGET_LOCKED / SKIPPED_LOCKED → contention cooldown
      5/10/20/30 s (not a failure), bounded by the DB missed-run grace window
      and a hard cap.
    - Every other non-committed outcome → minimum 1 s cooldown.
    - Queue rebuild honours per-occurrence `not_before`; refresh signals are
      coalesced (≤ 1 signal-driven refresh per second).
    - Scheduled result timestamps are rendered with `to_char` (DateStyle
      independent); db_config_para allowlist restricts DateStyle to ISO.
    - Overdue occurrences beyond the grace window are no longer executed at
      all (missed-run policy).

Tests:
    `test_phase5a_missed_run_and_retry.py` (`SchedulerLoopRegressionTests`
    run the real loop with an always-overdue occurrence: exactly one execution
    in the observation window for each of FAILED_DATABASE, FAILED_CONNECTION,
    SKIPPED_TARGET_LOCKED; bounded attempts; shutdown during backoff).

Deployment Notes:
    Apply 20261110_01 then 20261110_02, then deploy Python; scheduler stays
    STOPPED in between. See RUNBOOK §10.6.

Remaining Risk:
    SQL bodies parsed with libpg_query but not executed against PostgreSQL/EDB
    in this repo (no safe local DB — ISSUE-004).

Update (2026-11-10, Phase 5A hardening — CHANGE-014):
    The fixed 10-minute "grace" rule from the first 5A pass was replaced by
    ownership-based classification (historical vs scheduler-owned occurrence)
    plus a 6-hour DB safety ceiling. Retry defaults are now 4 attempts at
    15/60/180 s; target-lock contention cools down 5–30 s for up to 1 h; both
    configurable. Migrations smoke-tested on a disposable PostgreSQL 16
    cluster (framework worker/cron functions not available — not versioned).

Update (2026-11-10, Phase 5B) — ISSUE-003 remains OPEN:
    Capture tooling now exists (`sql/capture_reference_framework.sql`,
    READ-ONLY) and `cron_to_interval_or_next_run(text)` is listed in
    `sql/framework/framework_manifest.json` with status `capture_required`
    (placeholder `sql/framework/captured/cron_to_interval_or_next_run.CAPTURE_REQUIRED.md`).
    `cron_field_to_int_array` is tracked under this issue too (existence and
    signature unverified). Closes only when the reviewed captured definition
    is committed.

Update (2026-11-10, Phase 5B) — ISSUE-004:
    Local end-to-end testing no longer depends on the laptop's PostgreSQL
    service password: `scripts/disposable_db/run_disposable_db_test.sh` builds a
    throwaway `initdb` cluster from existing binaries. Still blocked on the
    missing authoritative definitions (ISSUE-003, ISSUE-008) for the full path.


## ISSUE-008 — Partition workers and insert function not version-controlled

Severity:
    High (blocks reproducible hub installation, full integration tests, handover)

Status:
    OPEN — capture required from the approved reference database.

Component:
    `mubasher_oms.create_any_table_partition(character varying, character varying, character varying, integer, interval)`
    `mubasher_oms.drop_any_table_partition(character varying, character varying, interval)`
    `mubasher_oms.insert_data_to_partition_job_table(<12 args>)`

Description:
    PartOps executes partition DDL only through these legacy functions and
    creates jobs only through the insert function, but their authoritative
    definitions are not in Git. Their security mode, owner, grants, dynamic SQL
    and DROP semantics are unreviewed. `realtime_scheduler_v1.sql` cannot be
    applied to a fresh database without them (dependency gate), so
    `get_upcoming_partition_jobs` is also absent in disposable tests.

Evidence:
    `sql/framework/framework_manifest.json` (status `capture_required`);
    disposable PostgreSQL 16 run: preflight FAIL for exactly these objects;
    `test_framework_phase5b.ManifestRepositoryConsistencyTests` guards that no
    body for them appears in `sql/` until captured.

Fix:
    DBA capture per RUNBOOK §16 → `sql/framework/captured/<name>.sql` →
    SECURITY_REVIEW_CHECKLIST.md → manifest update → disposable integration run.

Do not close:
    on placeholders, capture scripts or partial tests — only when the reviewed
    definitions are committed and the integration plan passes.


Update 2026-11-17 (Phase 5C):
    Phase 5B.1 (import of the authoritative captured definitions) is
    DEFERRED. The only authoritative source is production, and production
    capture is not permitted. No definition was reconstructed or invented;
    this issue stays OPEN. Phase 5C does not depend on the bodies (its SQL
    guards return before any worker is called).

## ISSUE-009 — EDB Postgres Advanced Server not tested

Severity:
    Medium

Status:
    OPEN

Description:
    All SQL validation so far ran on PostgreSQL 16 (disposable cluster). No
    PartOps SQL has executed on EDB. See `sql/framework/EDB_COMPATIBILITY.md`
    (DateStyle default, `varchar2` reporting, `edb_redwood_*` settings, pgAgent
    vs DBMS_SCHEDULER, identity columns / `prokind` version requirements).

Fix:
    Run the disposable harness and preflight on a disposable EDB instance of
    the production version; record results.


## ISSUE-010 — Possible EDB `sys` search_path conflict in the DEFINER edit function

Severity:
    Medium (potential; unproven)

Status:
    OPEN — verify after capture

Description:
    `update_data_to_partition_job_table` is SECURITY DEFINER with
    `search_path = pg_catalog, mubasher_oms, pg_temp` and calls
    `cron_to_interval_or_next_run`. On EDB, Oracle-compatible built-ins live
    in schema `sys`. If the captured cron helper uses them unqualified, edits
    that need a schedule recalculation would fail on EDB. Not a proven
    incompatibility — Phase 5A semantics were NOT changed.

Fix (only if confirmed):
    A separate, reviewed change to the pinned search_path (e.g. adding `sys`
    before `pg_temp`) with a security review.

## ISSUE-011 — Jobs cannot yet execute on additional database targets (Phase 5D)

Severity:
    Medium (functional gap; safe by design)

Status:
    CLOSED — 2026-11-17 (Phase 5D, in source; not deployed)

Resolution:
    Remote orchestrator with a control-database execution-attempt ledger
    (claim → target transaction → finalization), conservative unknown-outcome
    handling (never auto-retried), restart recovery, global + per-target
    concurrency, target cooldowns, manual remote runs through the same engine,
    target-aware discovery. Validated on throwaway databases with TEST-ONLY
    stub workers (orchestration only). See REMOTE_EXECUTION.md, CHANGE-017.

Description:
    Phase 5C introduced the hub-local target registry and `database_target_id`
    on every job. The existing SQL executors run DDL in the database they are
    called in, so PartOps can only execute jobs on the control database
    (target `db01`). For any other target, the executors return
    `SKIPPED_REMOTE_TARGET` / `MANUAL_REMOTE_TARGET` without executing or
    logging, `get_upcoming_partition_jobs` ignores such jobs, and the API
    refuses (409) creating or moving jobs to non-control targets.

Required for Phase 5D:
    - Per-target dispatch: execute the worker over a connection to the target
      database (control DB keeps job metadata, history and attention).
    - Workers installed on each target from CAPTURED definitions only
      (blocked by Phase 5B.1 / ISSUE-008).
    - Per-target concurrency (`max_concurrent_jobs`), backoff and health gating
      so one failed target never blocks the others.
    - Cross-database outcome recording (DDL on target, log in control DB).


## ISSUE-012 — Remote execution review is API-only (multi-target UI pending)

Severity:
    Low

Status:
    CLOSED — 2026-11-17 (Phase 5E, in source; not deployed)

Resolution:
    Database targets page, target-aware jobs / history / review, attempts
    list and detail, deliberate OUTCOME_UNKNOWN resolution workflow (APPLIED /
    NOT_APPLIED + note + typed table for DROP), hub status on System. The API
    is still unauthenticated (separate existing risk, see PROJECT_CONTEXT).

Description:
    Execution attempts and the resolution of OUTCOME_UNKNOWN attempts
    (APPLIED / NOT_APPLIED + note) are available through
    `/api/execution-attempts`; the Needs-review UI lists REMOTE_OUTCOME_UNKNOWN
    items but has no target filter, attempt view or resolve dialog yet. The API
    remains unauthenticated (existing risk).

## ISSUE-013 — Rollback classification depends on unverified legacy worker behaviour

Severity:
    Medium (potential)

Status:
    OPEN — blocked by Phase 5B.1 (definitions only in production)

Description:
    Phase 5D treats "worker raised + ROLLBACK succeeded" as proven not applied.
    That holds for ordinary transactional PL/pgSQL. If a captured worker uses
    EDB autonomous transactions (PRAGMA AUTONOMOUS_TRANSACTION), dblink or other
    non-transactional side effects, partial changes could survive a rollback
    and a retry could repeat them.

Fix (after capture):
    Review the captured worker bodies; if any non-transactional behaviour is
    found, classify worker errors as OUTCOME_UNKNOWN (no retry) for that worker.

## ISSUE-014 — No application authentication / authorization (access-control dependency)

Severity:
    High (operational risk; unchanged by Phase 5E/5E.1)

Status:
    OPEN — requires an approved authentication architecture (not homemade)

Description:
    The UI (:8501), API (:8001) and the scheduler's internal endpoints (:8765,
    localhost) have no authentication. Anyone who can reach them can create /
    edit / run jobs, run DROP jobs (with the UI confirmation only), recover or
    acknowledge review items and record OUTCOME_UNKNOWN resolutions.

Mitigation until fixed:
    PartOps must be network-restricted / protected by approved access controls
    (admin network only, host firewall, no public exposure, scheduler bound to
    127.0.0.1) until an authentication architecture is approved. Do not add an
    ad-hoc login.

## ISSUE-015 — History log table has no index for global newest-first paging

Severity:
    Low (performance; grows with history volume)

Status:
    OPEN — recommendation; decide once production log volume is known

Description:
    `partitioning_job_table_log` has only the primary key and
    `(job_id, job_runtime DESC)`. The History page (no job filter), its status
    / target / date filters and the dashboard's newest-200 query sort the
    whole table: a parallel seq scan of ≈138 ms per page at 500 000 rows
    (disposable PostgreSQL 16, Phase 5F.1), growing linearly. Per-job queries
    (drawer latest manual run, job filter) already use the existing index.

    Measured with `CREATE INDEX ... (job_runtime DESC, job_log_id DESC)`: first
    page 0.3 ms, offset 10000 1.2 ms, status filter 2.8 ms; index size 15 MB
    per 500k rows; one extra index entry per history insert.

Why not added in Phase 5F.1:
    - Production row count is unknown (no production access); at a few
      thousand rows the scan is negligible.
    - Any migration bumps the framework version, so the scheduler version gate
      requires it before the code deploys.
    - On a large live table it must be built with `CREATE INDEX CONCURRENTLY`,
      which cannot run inside the transactional migration format.

Fix (when justified):
    DBA checks `SELECT count(*) FROM mubasher_oms.partitioning_job_table_log`.
    If large (≥ ~100k rows) or History is slow, add a migration (with version
    row + manifest bump) or a DBA-run script:
    `CREATE INDEX CONCURRENTLY IF NOT EXISTS partitioning_job_table_log_runtime_idx
     ON mubasher_oms.partitioning_job_table_log (job_runtime DESC, job_log_id DESC);`
    Validate on EDB first (ISSUE-009).
