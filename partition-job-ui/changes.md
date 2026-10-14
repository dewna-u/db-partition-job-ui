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
