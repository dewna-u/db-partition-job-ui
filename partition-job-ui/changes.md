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


## CHANGE-004 — Local laptop development workflow support

Date:
    2026-10-09

Reason:
    Enable continuous local edit/run/test on the developer laptop without
    touching the production server or production databases.

Related Issues:
    None (workflow support)

Files Changed:
    - `scripts/dev-local.ps1` (new)
    - `requirements.txt` (add httpx for TestClient)
    - `api/main.py` (CORS origins for :8501)
    - `frontend/.env.local` (local comment; gitignored)
    - `README.md`
    - `RUNBOOK.md`
    - `changes.md`

Database Changes:
    None.

Behavioural Impact:
    - Documented local ports 8501 / 8001 / 8765.
    - Launcher starts API + UI; scheduler only with `-IncludeScheduler` after
      localhost DB safety checks (refuses 172.20.224.68 / ukbodev).
    - No production credentials added.

Notes:
    Scheduler not started in this change set — no safe configured local
    PartOps DB credentials/schema verified yet.
    No commit/push; no production server access.


## CHANGE-005 — Local partops_dev bootstrap preparation (blocked on auth)

Date:
    2026-10-09

Reason:
    Prepare localhost-only DB bootstrap for continuous development. Actual
    CREATE DATABASE could not run: local Postgres requires a password and none
    was available. Missing non-versioned framework function bodies also block
    a complete scheduler stack without inventing SQL.

Related Issues:
    ISSUE-004, ISSUE-003

Files Changed:
    - `sql/local_dev/README.md` (new)
    - `scripts/bootstrap-local-partops-dev.ps1` (new)
    - `issues.md`
    - `changes.md`

Database Changes:
    None applied (auth blocked).

Notes:
    LOCALHOST only. No production DB/server access. No commit/push.
    Resume: set `PARTOPS_LOCAL_PG_ADMIN_PASSWORD` and re-run bootstrap script.


## CHANGE-006 — AWS Secrets Manager database credential integration

Date:
    2026-10-13

Reason:
    Remove the need to store `DB_PASSWORD` in production `.env` /
    `.env.realtime` by resolving credentials from AWS Secrets Manager via the
    EC2 IAM instance role, while keeping an `env` provider for local/rollback.

Related Issues:
    None (security enhancement)

Files Changed:
    - `db_credentials.py` (new)
    - `database.py`
    - `scheduler_backend/scheduler_database.py`
    - `system_readiness.py`
    - `test_db_credentials.py` (new)
    - `requirements.txt` (+ boto3)
    - `.env.example`
    - `.env.realtime.example`
    - `README.md`
    - `RUNBOOK.md`
    - `changes.md`

Dependency Changes:
    - `boto3>=1.34,<2.0` (lazy import; unused when provider=env)

Environment Variables:
    - `PARTITION_DB_SECRET_PROVIDER` — `env` (default) or `aws`
    - `PARTITION_DB_SECRET_ID` — required in aws mode
    - `AWS_REGION` / `AWS_DEFAULT_REGION` — optional region hint
    - `PARTITION_DB_SECRET_CACHE_TTL_SECONDS` — default 300

Behavioural Impact:
    - Shared resolver; no dotenv loading inside `db_credentials.py`.
    - AWS mode fail-closed (no silent `DB_PASSWORD` fallback).
    - In-process TTL cache; invalidate + single retry on auth SQLSTATE.
    - Phase 0 scheduler transaction boundary unchanged.

Deployment Requirements (NOT executed here):
    1. Deploy source + `pip install -r requirements.txt`.
    2. Keep provider=env until secret/IAM ready.
    3. Switch to aws; restart; verify; remove `DB_PASSWORD`.

Rollback:
    Set provider=env, restore `DB_PASSWORD`, restart.

Notes:
    No commit/push; no production server/DB/AWS secret/IAM changes in this turn.


## CHANGE-007 — Controlled parallel scheduled job execution (Phase 2)

Date:
    2026-10-13

Reason:
    Allow multiple different scheduled partition jobs to execute simultaneously
    with a manually configurable concurrency limit, while serializing same-target
    CREATE/DROP via PostgreSQL advisory locks and keeping the event loop
    responsive.

Related Issues:
    ISSUE-005 (manual vs scheduled target lock — documented, not fixed here)

Files Changed:
    - `scheduler_backend/scheduler.py`
    - `scheduler_backend/scheduler_database.py`
    - `scheduler_backend/models.py`
    - `sql/realtime_scheduler_v1.sql`
    - `sql/migrations/20261013_01_scheduled_target_advisory_lock.sql` (new)
    - `sql/migrations/README.md`
    - `.env.realtime.example`
    - `test_scheduler_phase2_concurrency.py` (new)
    - `test_scheduler_phase0_safety.py` (SKIPPED_TARGET_LOCKED uncached)
    - `README.md`
    - `RUNBOOK.md`
    - `issues.md`
    - `changes.md`

Environment Variables:
    - `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS` — default `1` when unset;
      reject `0` / negative / float / empty / non-integer (no silent coerce).

Architecture:
    - Asyncio scheduler loop + `asyncio.Semaphore(N)`.
    - Blocking `execute_scheduled_job` via `asyncio.to_thread` (Python 3.9).
    - One short-lived DB connection per active job; no pool; no shared conn.
    - Active tasks tracked; exceptions retrieved; coalesced post-exec refresh.
    - Bounded graceful drain on shutdown.

Database Changes:
    `CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_scheduled(...)`
    — preserve signature/SECURITY. Add target advisory lock namespace
    `87421032` after job lock + validation; return `SKIPPED_TARGET_LOCKED`
    when unavailable (non-blocking; next_run_time unchanged).
    `CREATE OR REPLACE` preserves owner and existing EXECUTE grants (no DROP).

Status Endpoint:
    `max_concurrent_jobs`, `active_job_count`, `queued_job_count`,
    `available_slots`.

Tests:
    `test_scheduler_phase2_concurrency.py` plus full suite
    `python -m unittest discover -s . -p "test_*.py" -v`.

Deployment (operator — not executed here):
    See RUNBOOK §10.3. First production value must be `1`.

Rollback:
    Set `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS=1` and restart scheduler
    (serial behaviour without reverting source).

Notes:
    No commit/push; no production server/DB/AWS/Datadog/Edit work; no pool.


## CHANGE-008 — Phase 3 Datadog observability

Date:
    2026-10-13

Reason:
    Add lightweight, optional DogStatsD metrics and searchable structured logs
    for API, scheduler, DB connectivity, Secrets Manager, and job execution —
    without embedding a Datadog API key or hard-depending on the Agent.

Related Issues:
    ISSUE-006 (Python 3.9 / boto3 support horizon — documented follow-up)

Files Changed:
    - `observability.py` (new — stdlib UDP DogStatsD)
    - `scheduler_backend/scheduler.py`
    - `scheduler_backend/scheduler_database.py`
    - `database.py`
    - `db_credentials.py`
    - `api/main.py`
    - `system_readiness.py`
    - `test_observability.py` (new)
    - `.env.example`
    - `.env.realtime.example`
    - `README.md`
    - `RUNBOOK.md`
    - `issues.md`
    - `changes.md`

Dependency Changes:
    None. No `datadog` / `datadog-api-client` package.

Environment Variables:
    - `PARTOPS_DATADOG_ENABLED` — default `false`
    - `PARTOPS_DOGSTATSD_HOST` — default `127.0.0.1`
    - `PARTOPS_DOGSTATSD_PORT` — default `8125`
    - `PARTOPS_ENVIRONMENT` — default `unknown`
    - `PARTOPS_METRICS_NAMESPACE` — default `partops`

Architecture:
    logs → journald → Agent; metrics → DogStatsD localhost:8125 → Agent.
    Agent owns API key. App never calls Datadog HTTPS API.

Metrics (selected):
    scheduler heartbeat / queue / concurrency / overdue; job.execution +
    duration_ms (DB duration only); db.connection_failure; secrets_manager
    refresh (real fetch only) + retrieval_failure; api.request / api.error
    (method + status_class only).

Security / cardinality:
    No job_id / job_name / table / secret ARN / password / exception text as
    metric tags. Metric send failures never break business paths.

Readiness:
    Informational `datadog_observability` check (`not_required`); Agent down
    does not mark PartOps not-ready.

Tests:
    `test_observability.py` + full suite.

Deployment (operator — not executed here):
    See RUNBOOK §10.4. Enable only after Agent is installed.

Rollback:
    `PARTOPS_DATADOG_ENABLED=false` (+ restart). App continues without Agent.

Notes:
    No commit/push; no production server/DB/AWS/Agent install; no Edit work.


## CHANGE-009 — Gauge for current enabled failed jobs

Date:
    2026-10-13

Reason:
    Datadog “Failed Partition Jobs” must not use `partops.job.execution`
    (execution-attempt counter). Add a true current-state gauge of enabled
    job rows with `last_run_status = 'FAIL'`.

Related Issues:
    None (observability correction)

Files Changed:
    - `observability.py` (`emit_jobs_failed_count`)
    - `scheduler_backend/scheduler_database.py` (`count_failed_jobs`)
    - `scheduler_backend/scheduler.py` (emit during successful refresh)
    - `test_observability.py`
    - `README.md`
    - `RUNBOOK.md`
    - `changes.md`

Metric:
    `partops.jobs.failed_count` — DogStatsD **gauge**
    SQL: `COUNT(*)` from `{schema}.partitioning_job_table`
    where `is_enabled IS TRUE AND last_run_status = 'FAIL'`.
    Disabled FAIL / SUCCESS / NULL status excluded. Not from log table.

Behaviour:
    Emitted on successful scheduler refresh/reconcile.
    If the COUNT query fails: log `event=failed_jobs_count_failed`, do not
    emit a fake zero, do not break refresh/scheduling.
    `partops.job.execution` unchanged (still useful for attempt history).

Tests:
    Failed-jobs gauge cases in `test_observability.py` + full suite.

Notes:
    No commit/push; no Datadog dashboard/API changes from Cursor.


## CHANGE-010 — Phase 4 secure Edit / Enable / Disable

Date:
    2026-10-13

Reason:
    Edit/Enable/Disable failed to persist under least-privilege because
    `update_data_to_partition_job_table` was SECURITY INVOKER without hardened
    owner/EXECUTE grants (ISSUE-002).

Related Issues:
    ISSUE-002

Files Changed:
    - `sql/migrations/20261013_02_secure_update_partition_job.sql` (new)
    - `sql/migrations/README.md`
    - `api/routers/jobs.py` (return refreshed job after update)
    - `database.py` (map SQLSTATE 22023 validation failures)
    - `system_readiness.py` (remediation hint)
    - `frontend/src/app/jobs/page.tsx` (refresh selected after save)
    - `frontend/src/lib/api.ts`
    - `test_phase4_secure_update.py` (new)
    - `issues.md`, `changes.md`, `README.md`, `RUNBOOK.md`

Root Cause:
    SECURITY INVOKER + missing EXECUTE grant / no DEFINER owner path required
    direct table UPDATE for `partition_job_ui` (or updates failed).

Database security:
    SECURITY DEFINER; OWNER mubasher_oms;
    `SET search_path = pg_catalog, mubasher_oms, pg_temp`;
    REVOKE ALL FROM PUBLIC; GRANT EXECUTE TO partition_job_ui;
    no table UPDATE grant.

Semantics:
    Disable: is_enabled=false; preserves next_run_time, last_run_*, history.
    Re-enable / schedule edit: one future next_run_time via
    cron_to_interval_or_next_run; fail closed (rollback) if invalid.
    Row locked with FOR UPDATE vs scheduler.

API / UI:
    PATCH still calls the DB function; returns `job` after success;
    scheduler refresh failure does not roll back DB; Jobs page reloads
    selected row so Enabled/Disabled and next_run_time update in UI.

Tests:
    `test_phase4_secure_update.py` + full suite.

Deployment / rollback:
    See RUNBOOK §10.5. Backup with pg_get_functiondef before apply;
    restore prior definition on rollback (do not grant table UPDATE).

Notes:
    No commit/push; no prod DB/server access; Datadog Agent unchanged;
    partops.jobs.failed_count preserved.

## CHANGE-011 — Frontend design language alignment (Central DB visual language)

Date:
    2026-10-14

Reason:
    Align PartOps UI presentation with the GTN Central DB console design
    language (dense operational admin console) while preserving all PartOps
    functionality and the existing Next.js architecture.

Related Issues:
    None (visual/UX alignment only)

Files Changed:
    - `frontend/src/app/globals.css` (semantic design tokens; light/dark)
    - `frontend/tailwind.config.ts` (token aliases, typography scale)
    - `frontend/src/app/layout.tsx` (Manrope + JetBrains Mono)
    - `frontend/src/components/shell/app-shell.tsx` (52px top header shell)
    - `frontend/src/components/shell/sidebar.tsx` (flat chrome side nav)
    - `frontend/src/components/shell/page-header.tsx` (compact page headers)
    - `frontend/src/components/theme/theme-toggle.tsx`
    - `frontend/src/components/ui/button.tsx`, `badge.tsx`, `input.tsx`, `sheet.tsx`
    - `frontend/src/components/ui/panel.tsx` (new Panel / QueryState)
    - `frontend/src/components/jobs/job-form.tsx`, `job-detail-panel.tsx`
    - `frontend/src/app/page.tsx`, `jobs/page.tsx`, `jobs/new/page.tsx`
    - `frontend/src/app/system/page.tsx`, `history/page.tsx`, `convert/page.tsx`
    - `frontend/src/lib/format.ts` (status tone helpers)
    - `changes.md`

Database Changes:
    None

Configuration Changes:
    None. Ports unchanged. `.env` / `.env.realtime` unchanged.
    No Vite/shadcn migration. Next.js stack preserved.

Behavioural Impact:
    Visual/UX only. API contracts, scheduler, SQL, Secrets Manager, Datadog,
    Phase 0–4 behaviours unchanged. Theme modes light/dark/system retained.

Tests:
    `npm run lint` (frontend) — pass
    `npm run build` (frontend) — pass
    No dedicated frontend unit test script configured.

Notes:
    Design reference: design-cdb guide (visual language only).
    Central DB domain features (Users, Policies, Grants, Hubs, etc.) not copied.
    No commit/push; no server/DB/AWS/Datadog access.

## CHANGE-012 — PartOps logo and branded loading indicator

Date:
    2026-10-14

Reason:
    Integrate new transparent PartOps wordmark and branded loader icon into
    the existing Central DB-aligned Next.js UI for clearer product identity
    and consistent loading/refresh feedback.

Files Changed:
    - `frontend/public/branding/partops-logo-transparent.png` (new)
    - `frontend/public/branding/partops-loader.png` (new)
    - `frontend/public/branding/README.md`
    - `frontend/src/app/globals.css` (loader spin + reduced-motion)
    - `frontend/src/components/ui/partops-loader.tsx` (new)
    - `frontend/src/components/ui/panel.tsx` (QueryState uses loader)
    - `frontend/src/components/shell/app-shell.tsx`
    - `frontend/src/components/shell/sidebar.tsx`
    - `frontend/src/components/jobs/job-form.tsx`
    - `frontend/src/components/jobs/job-detail-panel.tsx`
    - `frontend/src/app/jobs/page.tsx`
    - `frontend/src/app/history/page.tsx`
    - `frontend/src/app/system/page.tsx`
    - `frontend/src/app/convert/page.tsx`
    - `changes.md`

Behavioural Impact:
    Visual/UX only. Favicon remains `partops-mark.svg`. Background polling
    does not show a continuous loader. API/scheduler/DB unchanged.

Notes:
    No commit/push; no server/DB/AWS access.

## CHANGE-013 — Phase 5A: missed-run policy, bounded retry/backoff, manual target lock, db_config allowlist

Date:
    2026-11-10

Reason:
    Make the single-database scheduler safe before any multi-host work:
    stop the immediate retry loop (ISSUE-007), stop automatic execution of
    historical overdue occurrences, make missed work reviewable by the DBA,
    close the manual-vs-scheduled target race (ISSUE-005) and restrict
    db_config_para to safe session parameters.

Related Issues:
    ISSUE-007 (fixed in source), ISSUE-005 (fixed in source), ISSUE-001 (note)

Files Changed:
    - `sql/migrations/20261110_01_partition_job_attention.sql` (new)
    - `sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql` (new)
    - `sql/realtime_scheduler_v1.sql` (canonical scheduled body + reconcile)
    - `sql/migrations/README.md`
    - `scheduler_backend/retry_policy.py` (new)
    - `scheduler_backend/scheduler.py`, `scheduler_database.py`,
      `queue_manager.py`, `models.py`
    - `observability.py`
    - `validators.py` (db_config allowlist)
    - `database.py` (attention review, conflict/not-found errors,
      MANUAL_TARGET_LOCKED → 409)
    - `api/deps.py`, `api/main.py`, `api/routers/attention.py` (new)
    - `frontend/src/components/jobs/attention-review.tsx` (new),
      `job-detail-panel.tsx`, `app/jobs/page.tsx`, `app/page.tsx`,
      `lib/api.ts`, `lib/types.ts`, `lib/format.ts`
    - `.env.realtime.example`
    - `test_phase5a_missed_run_and_retry.py` (new);
      `test_scheduler_phase0_safety.py`, `test_scheduler_phase2_concurrency.py`
      (canonical body now tracks the Phase 5A migration; handled-status set)
    - `README.md`, `RUNBOOK.md`, `issues.md`, `changes.md`, `../PROJECT_CONTEXT.md`

Database Changes (prepared, NOT applied):
    - New table `mubasher_oms.partition_job_attention` (+ guard trigger, indexes).
    - New helpers `partition_job_missed_run_grace()` (10 min) and
      `partition_job_config_param_allowed(text, text)`.
    - New `reconcile_missed_partition_job`, `run_partition_job_attention_recovery`,
      `acknowledge_partition_job_attention`.
    - `CREATE OR REPLACE` of `run_partition_job_scheduled`,
      `run_partition_job_manual`, `update_data_to_partition_job_table`.
    - PUBLIC EXECUTE revoked on all touched functions; narrow role grants.

Behavioural Impact:
    - Overdue occurrence later than expected + grace: NOT executed; attention
      item recorded; `next_run_time` → first future occurrence (fail closed to
      NULL if none). `last_run_status` / `last_run_time` untouched.
    - FAILED_DATABASE / FAILED_CONNECTION: retries at 5/15/30 s, then
      RETRY_EXHAUSTED attention item; never immediate, never unbounded.
    - SKIPPED_TARGET_LOCKED: contention cooldown 5/10/20/30 s within grace.
    - Manual run: `MANUAL_TARGET_LOCKED` (HTTP 409) while the table is busy.
    - db_config_para: only allowlisted keys; DateStyle ISO-only. Existing rows
      with other keys fail closed at execution (FAILED, logged) until edited.
    - New UI: amber "Needs review" on Jobs/Overview; detail-panel review card
      with "Run missed occurrence" / "Acknowledge without running".
      Recovery never changes `next_run_time`; DROP recovery requires typing
      `schema.table`.

Environment Variables (optional, scheduler):
    `PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS` (5,15,30),
    `PARTITION_SCHEDULER_CONTENTION_DELAYS_SECONDS` (5,10,20,30),
    `PARTITION_SCHEDULER_EXHAUSTED_RETRY_SECONDS` (60),
    `PARTITION_RECONCILE_MISSED_FUNCTION`, `PARTITION_JOB_ATTENTION_TABLE`.

Metrics (new, low-cardinality):
    `partops.jobs.pending_review_count` (gauge),
    `partops.missed_run.detected` {reason},
    `partops.missed_run.reconciled` {reason, outcome},
    `partops.scheduler.retry_scheduled` {status},
    `partops.scheduler.retry_exhausted` {status}.

Tests:
    470 backend tests OK (`python -m unittest discover -s . -p "test_*.py"`).
    `npm run lint` clean. `npm run build` passes (verified from a copy outside
    OneDrive; the in-place build hit an OneDrive `EINVAL readlink` on the
    generated `.next` cache — environmental, not a code error).
    SQL parsed with libpg_query (pglast 7.22, scratch only) — grammar only.

Deployment / Rollback:
    RUNBOOK §10.6. No commit/push; no server/DB/AWS/Datadog access.

Note:
    The 10-minute grace rule, `partition_job_missed_run_grace()`, the
    5/15/30 s retry defaults and `PARTITION_SCHEDULER_CONTENTION_DELAYS_SECONDS`
    described above were replaced before release by CHANGE-014.

## CHANGE-014 — Phase 5A hardening: ownership-based missed runs, configurable retry, failed-gauge fix

Date:
    2026-11-10

Reason:
    Review of CHANGE-013 before acceptance. A fixed "scheduled time + 10 min"
    rule could turn an occurrence the scheduler had already queued on time —
    and that was only waiting for a concurrency slot or a busy target table —
    into a "missed" run. Retry defaults (≈50 s total) were too short for a
    DB restart/failover. A successful manual recovery left
    `partops.jobs.failed_count` raised until the next scheduled run.

Related Issues:
    ISSUE-007 (hardened), ISSUE-005 (unchanged; fixed in CHANGE-013)

Design:
    - Ownership, decided once at first sighting from the DB clock:
      OWNED = first seen before due or ≤ `PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS`
      (default 60 s, jitter only) after it; HISTORICAL = first seen later.
      HISTORICAL → `reconcile_missed_partition_job(..., 'MISSED_SCHEDULED_RUN')`
      (never executed). OWNED → `run_partition_job_scheduled` (full DB
      revalidation), may run late after waiting for slot / lock / backoff.
    - DB safety ceiling `partition_job_max_execution_lateness()` (default
      6 h) replaces `partition_job_missed_run_grace()` (10 min): the DB never
      executes an occurrence later than that, whatever the caller claims.
    - Startup check: `LOOKAHEAD_SECONDS >= RECONCILE_SECONDS`.
    - Retry: `PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS` (4),
      `PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS` (15,60,180; last repeats),
      `PARTITION_SCHEDULER_TARGET_LOCK_COOLDOWN_SECONDS` (5,10,20,30),
      `PARTITION_SCHEDULER_TARGET_LOCK_MAX_WAIT_SECONDS` (3600). Strictly
      validated; invalid values stop the scheduler at startup.
    - Failed-jobs gauge: `last_run_status` semantics unchanged (latest
      scheduled outcome). `partops.jobs.failed_count` excludes a FAIL row when
      a LATER manual run succeeded (latest manual outcome wins). Scheduler
      role gets read-only SELECT on the log table; falls back to the legacy
      rule if the grant is missing.
    - Attention guard: `MANUALLY_RERUN` only with a recorded `MANUAL_SUCCESS`.
      `acknowledge_partition_job_attention` gets a fixed `search_path`.

Files Changed:
    - `sql/migrations/20261110_01_partition_job_attention.sql`,
      `20261110_02_missed_run_reconcile_retry_and_manual_lock.sql`,
      `sql/realtime_scheduler_v1.sql`, `sql/migrations/README.md`
    - `scheduler_backend/retry_policy.py`, `scheduler.py`,
      `scheduler_database.py`, `models.py`
    - `.env.realtime.example`
    - `test_phase5a_missed_run_and_retry.py` (+ hardening tests),
      `test_observability.py` (failed-gauge contract updated)
    - `README.md`, `RUNBOOK.md`, `issues.md`, `changes.md`, `../PROJECT_CONTEXT.md`

Validation:
    - 493 backend tests OK.
    - Disposable PostgreSQL 16 smoke test (throwaway `initdb` cluster in the
      session scratchpad, 127.0.0.1:55439, destroyed afterwards): repo
      bootstrap + migrations 20260929_01/02/03, 20261013_02, 20261110_01/02
      applied (5A migrations applied twice — idempotent); catalog/security
      matrix, allowlist, historical reconcile, DB ceiling, 30-min-late owned
      occurrence NOT treated as missed, attention immutability and single
      resolution, recovery keeps next_run_time, config-drift refusal,
      manual/scheduled target lock, failed-gauge semantics — all passed.
      Not covered (authoritative bodies not version-controlled):
      `cron_to_interval_or_next_run`, `create_any_table_partition`,
      `drop_any_table_partition`, `insert_data_to_partition_job_table`,
      `get_upcoming_partition_jobs` (realtime_scheduler_v1.sql dependency gate).

Notes:
    No commit/push; no server/production DB/AWS/Datadog access.

## CHANGE-015 — Phase 5B: version-controlled database framework model, capture, preflight, version gate

Date:
    2026-11-10

Reason:
    PartOps depends on legacy database functions whose authoritative source is
    not in Git, which blocks reproducible hub deployment, full integration
    testing and handover. This change makes the framework explicit and
    checkable without inventing any production function body.

Related Issues:
    ISSUE-003 (open), ISSUE-004 (update), ISSUE-008 (new, open),
    ISSUE-009 (new, open), ISSUE-010 (new, open)

Files Added:
    - `sql/framework/framework_manifest.json` (source of truth), `README.md`,
      `INVENTORY.md` (generated), `SECURITY_REVIEW_CHECKLIST.md`,
      `EDB_COMPATIBILITY.md`, `INTEGRATION_TEST_PLAN.md`,
      `captured/README.md`, `captured/*.CAPTURE_REQUIRED.md` (5)
    - `sql/capture_reference_framework.sql` (READ ONLY transaction)
    - `sql/migrations/20261110_03_partops_framework_version.sql`
    - `sql/tests/disposable/00_roles.sql`, `10_post_bootstrap_grants.sql`,
      `20_phase5a_smoke.sql`, `30_target_lock.sql`, `40_integration_full.sql`
    - `framework_manifest.py` (load/validate/read-only snapshot/evaluate)
    - `scripts/preflight_framework.py`, `scripts/framework_inventory.py`,
      `scripts/disposable_db/run_disposable_db_test.sh`
    - `test_framework_phase5b.py`, `test_framework_integration.py`

Files Changed:
    - `scheduler_backend/scheduler.py`, `scheduler_database.py`, `models.py`
      (framework start gate), `observability.py` (`scheduler.framework_ready`)
    - `system_readiness.py` (manifest check)
    - `sql/capture_reference_framework_functions.sql` (marked superseded)
    - `sql/migrations/README.md`, `.env.realtime.example`, `README.md`,
      `RUNBOOK.md` (§10.6 + new §16), `issues.md`, `changes.md`,
      `../PROJECT_CONTEXT.md`

Behavioural Impact:
    - Scheduler: before dispatching anything it runs a READ-ONLY gate
      (scheduler scope of the manifest as its own role). On FAIL/unknown it
      dispatches nothing and re-checks every
      `PARTITION_SCHEDULER_FRAMEWORK_RECHECK_SECONDS` (300). Requires migration
      20261110_03 and the legacy functions to be present.
    - No Phase 5A semantics changed.

Validation:
    - Backend unit tests (see final report); integration tests SKIP with
      "FRAMEWORK DEFINITION NOT AVAILABLE".
    - Disposable PostgreSQL 16 run of the repository harness: all runnable
      stages pass; preflight FAILs exactly on the uncaptured legacy functions
      (+ get_upcoming_partition_jobs, which v1 cannot install without them);
      capture script runs read-only on PostgreSQL 16.

Notes:
    No commit/push; no server, live/reference DB, AWS or Datadog access.

## CHANGE-016 — Phase 5C: hub-local multi-database foundation

Date:
    2026-11-17

Reason:
    ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
    NO GLOBAL MULTI-HUB CONTROL PLANE. Jobs need an explicit database target
    before per-target execution (Phase 5D) can be built safely.

Related Issues:
    ISSUE-011 (new, open: remote-target execution). ISSUE-003 / ISSUE-008 /
    ISSUE-009 / ISSUE-010 stay OPEN — Phase 5B.1 is DEFERRED (production is the
    only source of the legacy definitions; production capture not permitted).

Files Added:
    - `sql/migrations/20261117_01_partops_database_target.sql` (registry,
      default control target db01, job column + backfill + NOT NULL + FK)
    - `sql/migrations/20261117_02_target_aware_executors.sql` (control-target
      guards; framework version 20261117_02)
    - `database_targets.py`, `api/routers/targets.py`
    - `sql/tests/disposable/15_pre5c_jobs.sql`, `25_phase5c_targets.sql`,
      `scripts/disposable_db/target_routing_check.py`
    - `test_phase5c_database_targets.py` (49 tests)

Files Changed:
    - `db_credentials.py` (per-secret keyed cache, per-target AWS resolution)
    - `database.py` (target column in job queries, pre-5C tolerant; registry
      reads; MANUAL_REMOTE_TARGET → 409), `api/main.py`, `api/routers/jobs.py`
      (`database_target_id`, validation, dangerous change, 409 until 5D)
    - `scheduler_backend/models.py`, `scheduler.py`, `scheduler_database.py`,
      `retry_policy.py` (control-target gate, occurrence target, target tag,
      SKIPPED_REMOTE_TARGET clears retry state)
    - `observability.py` (`target` tag, `target.healthy` gauge),
      `system_readiness.py` ("Database targets" category)
    - `framework_manifest.py`, `sql/framework/framework_manifest.json`,
      `INVENTORY.md` (regenerated), `realtime_scheduler_v1.sql` (canonical
      bodies synced), `sql/capture_reference_framework.sql`
    - Tests updated for the new canonical migration/version; harness
      `run_disposable_db_test.sh` (stages 5c/6c), `20_phase5a_smoke.sql` (T9b)
    - Docs: `README.md`, `RUNBOOK.md` (§10.7, §17), `sql/migrations/README.md`,
      `sql/framework/README.md`, `INTEGRATION_TEST_PLAN.md`, `.env*.example`,
      `issues.md`, `../PROJECT_CONTEXT.md`

Behavioural Impact:
    - Existing single-database installations: unchanged behaviour; all jobs
      belong to db01 (the control database).
    - Jobs of non-control targets are never executed in the control DB
      (SKIPPED_REMOTE_TARGET / MANUAL_REMOTE_TARGET); API refuses them (409).
    - Scheduler stays blocked unless exactly one enabled control target
      matches the connected database.
    - Health check of a target that connects but whose role cannot read the
      framework schema reports FRAMEWORK_INCOMPATIBLE (not a crash).
    - No Phase 5A semantics changed; no legacy function body invented or
      modified.

Validation:
    - Backend suite: 590 tests, 589 passed, 0 failed, 1 skipped
      (integration: FRAMEWORK DEFINITION NOT AVAILABLE).
    - Disposable PostgreSQL 16 harness (throwaway initdb, second DB
      partops_target2): all runnable stages pass; 5C migrations idempotent;
      U1–U5 and routing/health checks pass. It found and fixed an invalid
      PostgreSQL regex bound (`{1,512}` > 255) in the registry secret CHECK.

Notes:
    No commit/push; no server, production, AWS or Datadog access.

## CHANGE-017 — Phase 5D: target-aware scheduler + safe remote database execution

Date:
    2026-11-17

Reason:
    Execute partition jobs on the hub's managed target databases (db02, …)
    safely. ONE PARTOPS INSTANCE PER HUB / ~2 TARGET DATABASES PER HUB / NO
    GLOBAL MULTI-HUB CONTROL PLANE. No distributed transaction is assumed.

Related Issues:
    ISSUE-011 CLOSED. New: ISSUE-012 (review UI, Phase 5E), ISSUE-013 (rollback
    classification depends on unverified legacy worker behaviour). ISSUE-003 /
    008 / 009 / 010 stay OPEN (Phase 5B.1 deferred; EDB not validated).

Files Added:
    - `sql/migrations/20261117_03_remote_execution_ledger.sql`
    - `sql/migrations/20261117_04_remote_execution_functions.sql`
    - `scheduler_backend/remote_execution.py` (target transaction + classification)
    - `scheduler_backend/target_runtime.py` (slots, availability, preflight cache)
    - `api/routers/attempts.py`, `api/remote_manual.py`
    - `REMOTE_EXECUTION.md` (architecture / state machine / rules)
    - `sql/tests/disposable/stubs/test_only_stub_workers.sql`,
      `test_only_stub_cron_helper.sql` (DISPOSABLE ONLY)
    - `scripts/disposable_db/remote_execution_check.py`
    - `test_phase5d_remote_execution.py` (62 tests)

Files Changed:
    - `scheduler_backend/scheduler.py` (remote orchestrator, slots, recovery,
      pending finalization, per-target status), `scheduler_database.py`
      (target-aware discovery, ledger calls, config), `models.py`, `main.py`
      (`/internal/scheduler/manual-run`), `retry_policy.py` (remote statuses)
    - `database.py` (target on create/edit, attempts, resolution, history /
      attention target columns, 55006 → 409), `api/routers/jobs.py`,
      `attention.py`, `logs.py`, `api/main.py`, `scheduler_client.py`
    - `observability.py` (target slot / remote attempt / finalization / unknown
      metrics), `framework_manifest.py` (internal helpers), manifest +
      `INVENTORY.md` (version 20261117_04), `realtime_scheduler_v1.sql`
      (canonical reconcile), `sql/capture_reference_framework.sql`
    - Tests adjusted for the new version / handled statuses / lifted 5C 409;
      harness stage 11; `25_phase5c_targets.sql` U3
    - Docs: README, RUNBOOK (§10.8, §17), migrations / framework docs, EDB and
      security checklists, integration plan, env examples, issues, PROJECT_CONTEXT

Behavioural Impact:
    - db01 jobs: unchanged local executor; db01 now also honours its own
      `max_concurrent_jobs` (default 1).
    - db02+ jobs: claim → target transaction → finalization; unknown outcomes
      never retried (job paused + review); proven failures bounded retry;
      confirmed commits finalized only; restart recovery.
    - Manual runs / recovery of db02+ jobs require the scheduler (503 otherwise).
    - Job target change allowed with confirmation, healthy target, no open attempt.

Validation:
    - Backend suite: 652 tests, 651 passed, 0 failed, 1 skipped
      (integration: FRAMEWORK DEFINITION NOT AVAILABLE).
    - Disposable PostgreSQL 16 harness (control + partops_target2; db03
      disabled; db04 unreachable): all stages pass; 5C/5D migrations applied
      twice; stage 11: Part A (A0–A10) and Part B (real scheduler, B1/B2) all
      OK. Found and fixed: un-escaped '%' in the target lock statement.
    - Test-only stub workers validate PartOps orchestration only, never
      production worker logic.

Notes:
    No commit/push; no production, corporate DB, server, AWS or Datadog access.

## CHANGE-018 — Phase 5E: hub-local multi-target operations UI

Date:
    2026-11-17

Reason:
    Make the Phase 5C/5D architecture (one PartOps per hub, ~2 database targets,
    safe remote execution) understandable and operable by DBAs in the existing
    Next.js UI, without weakening any remote-execution rule.

Related Issues:
    ISSUE-012 CLOSED. ISSUE-003 / 008 / 009 / 010 / 013 remain OPEN.

Files Added (frontend/src):
    - `lib/targets.ts` (health / attempt / review vocabulary, hub status, error mapping)
    - `components/hub/hub-context.tsx` (shared scheduler / targets / health state)
    - `components/targets/target-badges.tsx`, `components/targets/scheduler-targets-table.tsx`
    - `components/attempts/attempt-review.tsx` (attempt detail, OUTCOME_UNKNOWN panel + resolution)
    - `app/targets/page.tsx` (read-only Database targets page)

Files Changed (frontend/src):
    - `lib/types.ts`, `lib/api.ts`, `lib/format.ts`
    - `components/shell/app-shell.tsx`, `components/shell/sidebar.tsx`
    - `components/jobs/job-form.tsx`, `job-detail-panel.tsx`, `attention-review.tsx`
    - `app/page.tsx`, `app/jobs/page.tsx`, `app/history/page.tsx`, `app/system/page.tsx`

Backend:
    - `database_targets.py`: `to_public_dict()["executable"]` = `is_enabled`
      (was the stale Phase 5C rule "enabled AND control"); test updated.

Behavioural Impact:
    - UI only. No new endpoints, no target administration, no retry / force
      action for unknown outcomes. Health checks are on demand (cached 60 s);
      no new aggressive polling.

Validation:
    - `npm run lint`: no warnings or errors. `npm run build`: success (11 routes).
    - Backend suite: 652 tests, 651 passed, 0 failed, 1 skipped (integration:
      FRAMEWORK DEFINITION NOT AVAILABLE).
    - Manual UI verification matrix documented (RUNBOOK §18.5); not executed
      against a live API in this change.

Notes:
    No commit/push; no production, server, AWS or Datadog access.

## CHANGE-019 — Phase 5E.1: integrated local UI/API verification and contract hardening

Date:
    2026-11-18

Reason:
    Exercise the Phase 5E UI end-to-end against the real API, scheduler and
    disposable databases before Phase 5F, and fix what that revealed.

Findings and fixes:
    - F1 frontend: scheduler / health timestamps are naive UTC; the sidebar
      showed "Last sync 5h ago" (UTC+05:30). `parseServerDate(..., {naiveUtc})`.
    - F2 frontend: hub headline hid managed-target failures behind a control
      framework failure; now lists every degraded reason.
    - F3 frontend: job-form labels were not associated with their controls.
    - F4 frontend: Run now was enabled on a job with an OUTCOME_UNKNOWN /
      DISPATCHING / TARGET_SUCCEEDED attempt (backend refused with 409);
      now disabled with the reason and a link to History.
    - F5 frontend: after a 504 / unknown outcome Run now stayed clickable
      (duplicate-run risk); now locked for that drawer session.
    - F6 backend (`scheduler_client.py`): a connection dropped AFTER the
      request was sent (RemoteDisconnected / reset / 5xx) was reported as 503
      "nothing was executed"; only a refused connection is now 503, everything
      else is the ambiguous 504 path. Tests added.
    - F7/F8 frontend: review wording for unknown outcomes ("1 item", "since").
    - F10 backend (`system_readiness.py`): with the control DB down a full
      readiness refresh took 47 s (each DB check waited its own connect
      timeout) and the UI proxy returned 500; dependent checks now report
      "not checked" (21 s). Tests added.
    - F11 frontend: job rows were not keyboard-focusable; sheets did not
      restore focus to the opener (`Sheet` now restores it).
    - F12/F13 frontend: History error column clipped the Details column;
      sidebar claimed target availability while the scheduler was down.

Validation:
    - RUNBOOK §18.5: 28/28 required scenarios PASS on the real local stack
      (several after the fixes above), plus 504 / drop / degraded / down /
      keyboard / polling / stability / redaction checks.
    - `npm run lint`: clean. `npm run build`: success.
    - Backend suite: 657 tests, 656 passed, 0 failed, 1 skipped (integration:
      FRAMEWORK DEFINITION NOT AVAILABLE).

Notes:
    No production / corporate DB; disposable cluster destroyed; no commit,
    push, server, AWS or Datadog access. No authentication added (ISSUE-014).

## CHANGE-020 — Phase 5F.1: performance / resource optimization (readiness, pagination, drawer data, polling)

Date:
    2026-11-18

Reason:
    Phase 5E.1 measured a 21–27 s readiness refresh with the control database
    down, unbounded client-side history scans (newest 200 rows, filtered in
    the browser), a job drawer that searched those 200 rows for the job's
    latest manual run (missing older ones), and one DB connection per
    readiness check / dashboard section. Keep it small: no architecture change.

Changes:
    - `system_readiness.py`:
      - Independent check groups run concurrently (thread pool per build).
      - Control-DB checks (tables, functions, manifest, target registry,
        privileges) share ONE connection; if it cannot be opened they are not
        run and report "not checked" (5E.1 behaviour kept).
      - Readiness-only bounds: DB connect `PARTITION_READINESS_CONNECT_TIMEOUT_SECONDS`
        (default 3, clamped 2..10), scheduler HTTP 1.5 s, port probes 1 s
        (parallel), target health connects clamped and run in parallel.
      - Cache: 30 s TTL (was 10 s), successes AND failures, single-flight lock,
        stamped when the build FINISHES (the old cache was stamped at the
        start, so a build slower than 10 s was never served from cache).
        `?refresh=true` bypasses and replaces it. `duration_ms` in payload.
    - `database.py`:
      - `query_partition_job_logs`: paged history (limit ≤500, offset
        ≤100000, filters target_id / status / job / operation / since /
        until, fixed SQL fragments + bound parameters, LIMIT n+1, order
        `job_runtime DESC, job_log_id DESC`). The control target id is
        resolved once per request (a per-row function call made the target
        filter 2.4× slower).
      - `page_execution_attempts`: paged attempts + `unknown_count`.
      - `get_job_runtime_context` / `get_job_pending_attention`: per-job data
        on one connection. `get_dashboard_snapshot`: jobs + logs + privilege
        readiness on one connection, sections fail independently.
      - Recent-logs query (dashboard): `to_jsonb()` only for the returned
        rows (was evaluated for every row before the sort).
      - `get_database_readiness(conn=None)` / `list_database_targets(conn=None)`
        accept an open connection.
    - API: `GET /api/logs` (paged + filters, validated → 422),
      `GET /api/execution-attempts` (+ target_id, offset, unknown_count),
      `GET /api/jobs/{id}/runtime-context`, `GET /api/jobs/{id}/attention`;
      dashboard uses the snapshot.
    - Frontend (appearance unchanged except a Previous/Next pager and an
      operation filter on History):
      - History: server-side filters and pages (100 rows; job filter
        debounced); attempts paged (50); unknown-outcome banner uses
        `unknown_count`.
      - Job drawer: one `runtime-context` request (was `logs(200)` + attempts);
        Overview drawer now shows the job's pending review items.
      - Hub polling: scheduler 30 s, control-DB dot 60 s (was 30 s), targets
        5 min, health on demand; no polling while the tab is hidden, one
        catch-up refresh when it becomes visible.
    - Metrics (DogStatsD, existing no-op when disabled): `readiness.duration_ms`
      (tag `overall`), `history.query_duration_ms`, `attempts.query_duration_ms`.
      No job / target / host tags.

Measurements (local; disposable PostgreSQL 16 on 127.0.0.1, 500 000 log rows,
60 jobs; before = HEAD adad25d extracted with `git archive`):
    - Readiness, control DB + scheduler not answering (blackhole): 27.31 s → 3.12 s
      (in-process); over HTTP 3.11 s, cached 0.010 s, refresh 3.12 s.
      Refused ports: 27.38 s → 3.05 s.
    - Connections per request: readiness refresh 9 → 4; readiness within TTL
      9 → 0; dashboard 3 → 1; job drawer 3 → 1; history page 2 → 2.
    - Dashboard recent logs (200): 1747 ms → 278 ms median.
    - Latest manual run, job 7: old 200-row scan did NOT find it; new query
      finds it (0.16 ms execution, index `job_id, job_runtime`); drawer
      request 1569 ms → 131 ms median.
    - History page (100 rows): 276–318 ms median incl. ~100 ms local connect;
      target filter 833 ms → 345 ms; job filter 98 ms.
    - Global history ordering has no index: parallel seq scan ≈138 ms at
      500k rows. A `(job_runtime DESC, job_log_id DESC)` index made it
      0.3 ms (offset 10000: 1.2 ms; status filter 2.8 ms); 15 MB. Not added —
      see ISSUE-015.

Validation:
    - New `test_phase5f_performance.py` (28) + readiness tests (short-circuit,
      shared connection, connection lost midway, slow DB bounded, timeout
      bounds, cache hit / expiry / refresh / invalidate / single-flight).
    - Backend suite: 694 tests, OK (1 skipped: integration, framework
      definitions not available).
    - `npm run lint`: clean. `npm run build`: success.
    - Multi-DB harness not rerun: scheduler, remote-execution and transaction
      code unchanged.

Notes:
    No migration, framework version unchanged (20261117_04). No connection
    pool, Redis, WebSockets or authentication (ISSUE-014 open). Remote
    execution state machine and OUTCOME_UNKNOWN handling unchanged. Phase
    5B.1 still deferred. No commit, push, server, production, AWS or Datadog
    access. Disposable perf cluster destroyed.
