# PartOps Project Context

> Re-read this at the start of every coding session. Verified against the
> repository at commit `8c17103` (2026-11-09 onboarding audit), updated for the
> **Phase 5A** local changes (2026-11-10), Phase 5B (framework model) and
> **Phase 5C** (hub-local multi-database foundation) and **Phase 5D** (safe
> remote execution, 2026-11-17) — all
> uncommitted at time of writing; SQL migrations prepared, not applied anywhere
> except throwaway disposable clusters. The repository is the source of
> truth: if code and this file disagree, trust the code and update this file.
>
> Sections are labelled **CURRENT** (implemented in source) or **PLANNED**
> (required, not yet implemented). Do not mix them.

---

## Product

**PartOps — GTN EDB Partition Operations Platform.** It is an internal console
for DBAs to configure, schedule, run, and monitor PostgreSQL/EDB partition
maintenance jobs. Each job is a row in `mubasher_oms.partitioning_job_table`
that either CREATEs future partitions or DROPs old ones on one
`(table_schema, table_name)`.

**CURRENT capabilities**

- List jobs.
- Create a job (also from a pgAgent job via the Convert page).
- Edit a job; enable and disable are fields in the edit form.
- Manual run ("Run now"; DROP requires confirmation).
- Execution history.
- CSV export.
- Cron preview.
- System readiness report.
- Dashboard KPIs.
- Realtime scheduler with bounded concurrency.
- AWS Secrets Manager credentials.
- DogStatsD metrics.
- **Phase 5A (source):** missed-run policy (overdue occurrences are recorded
  for DBA review, never auto-executed), bounded retry/backoff, DBA review
  workflow (run missed occurrence / acknowledge), manual-run target lock,
  db_config_para allowlist.

There is **no delete**, no in-app authentication, and no multi-database support.

## Current architecture

All application code lives in `partition-job-ui/`.

```text
Browser (admin network only; no in-app auth)
   │  http://<host>:8501
   ▼
Next.js 15 UI  (frontend/, `next start` on 0.0.0.0:8501, user partitionui)
   │  rewrites /api/*  →  PARTITION_API_ORIGIN (default http://127.0.0.1:8001)
   ▼
FastAPI  (api/main.py + api/routers/*, uvicorn 127.0.0.1:8001, loads .env)
   │  database.py / validators.py / job_autofill.py / dashboard_metrics.py
   │  system_readiness.py / scheduler_client.py (HTTP signal → :8765)
   ▼
PostgreSQL/EDB  mubasher_oms.*  (short-lived psycopg3 connection per operation)
   ▲
   │  short-lived connections; get_upcoming_partition_jobs / run_partition_job_scheduled
Scheduler  (scheduler_backend/, `python -m scheduler_backend.main`, user enterprisedb,
            loads ONLY .env.realtime; asyncio loop + heap queue + semaphore;
            control API on 127.0.0.1:8765: GET /health, GET /internal/scheduler/status,
            POST /internal/scheduler/refresh)
```

Shared modules:

- `db_credentials.py` resolves credentials (env or aws).
- `observability.py` sends DogStatsD metrics over UDP.

There are no connection pools, no Redis/Celery/queues, and no LISTEN/NOTIFY.
PostgreSQL is authoritative; scheduler memory is ephemeral.

## Deployment model

**CURRENT**

- One Linux host with deploy path `/opt/db-partition-job-ui-github/partition-job-ui`.
- Three systemd units, all `Restart=on-failure`:

  | Unit | File | Runs as |
  |---|---|---|
  | `partition-job-ui.service` | `partition-job-ui.service` | partitionui |
  | `partition-job-api.service` | `systemd/` | partitionui |
  | `partition-job-scheduler.service` | `systemd/` | enterprisedb |

- `.env` is `partitionui:600`; `.env.realtime` is `enterprisedb:600`. The scheduler
  must never read `.env`, and the API must never read `.env.realtime` (both are
  enforced in code).
- Production runs **Python 3.9**, so source must stay 3.9-compatible: use
  `Optional[...]`, not `X | None`, in runtime-evaluated annotations. The README
  says "3.11+"; that is doc drift.
- `README.md` and `RUNBOOK.md` hold the operator procedures. SQL migrations are
  applied manually by a DBA and never by the app.

**PLANNED (hub-local model)**

- One independent PartOps deployment **per hub**. Each manages about **two**
  database hosts/databases in its own hub.
- PartOps is **not** a global control plane across hubs.

## Frontend

**CURRENT** stack:

- Next.js 15.2.4 App Router, React 19, Tailwind 3.4, TypeScript.
- Radix UI: only Dialog and Slot are used. Six other Radix packages are installed but unused.
- lucide-react icons.

| Route | Purpose |
|---|---|
| `/` | Dashboard: health banner, KPIs, first 12 jobs, inline detail/edit panel, recent logs, insights |
| `/jobs` | Full table with filters; side Sheet opens `JobDetailPanel` (view / edit / Run now) |
| `/jobs/new` | `JobForm` create |
| `/history` | Last 100 log rows; error-detail sheet |
| `/convert` | pgAgent job → detected config → prefilled `JobForm` |
| `/system` | Readiness report (secret-looking keys filtered client-side) |

- **Edit, Enable and Disable** have no dedicated route or toggle. They go through
  `JobForm mode="edit"` with the full `PATCH /api/jobs/{id}`; `is_enabled` is a
  switch in the form.
- **Polling:** only `app-shell.tsx`, every 30 s. It calls GET
  `/api/scheduler/status` and GET `/api/readiness`; the readiness call opens one
  DB connection per open tab.
- **Theme:**
  - Light, dark and system modes, stored in localStorage key `partops-theme`.
  - `.dark` is applied to `<html>` before hydration.
  - Tokens are in `globals.css`: `--client-primary #00519f` (GTN blue), plus
    semantic success/warning/error/info/neutral tokens. Legacy `--pj-*` aliases remain.
- **Fonts:** Manrope (UI) and JetBrains Mono (identifiers), loaded with `next/font/google`.
- **Branding:**
  - `public/branding/partops-logo-transparent.png` appears in both header and sidebar.
  - `partops-loader.png` is used by `PartopsLoader`; it is about 407 KB and
    rendered unoptimised at 16–44 px.
  - The favicon `partops-mark.svg` still uses the old blue/lime palette.
  - `partops-logo.png.png` is unreferenced.
- **Known frontend defects (unfixed):**
  - Convert browse-row click loads the previous job id (stale state).
  - The dashboard detail panel can show a stale job after a run.
  - A background reload of the `JobForm` `initial` prop overwrites in-progress edits.
  - Edit mode starts with `auto_next_run=true`, so the cron preview overwrites `next_run_time`.
  - `/history` shows the "no rows" message while loading.
  - There are no `error.tsx` or `loading.tsx` route files.
- **Doc drift:** `DESIGN.md` and `PRODUCT.md` still describe the old
  Geist/cream/lime design.

## API

**CURRENT** routes. All are under `/api` except `/health`. There is no auth;
CORS allows localhost:3000 and localhost:8501.

| Frontend action | Route | Python | DB |
|---|---|---|---|
| List jobs | GET `/jobs` | `database.get_partition_jobs` | SELECT job table + per-job last/avg duration subqueries on log |
| Job detail | GET `/jobs/{id}` | `get_partition_job` | SELECT by id |
| CSV export | GET `/jobs/export.csv` | `dashboard_metrics.jobs_to_csv` | same as list |
| Create | POST `/jobs` | `validators.validate_form_data` → `create_partition_job` → `scheduler_client.notify_scheduler_refresh` | `insert_data_to_partition_job_table(...)` (named args) |
| Edit/Enable/Disable | PATCH `/jobs/{id}` | `_prepare_job_update` (dangerous-change confirm; Python next-run recalc) → `update_partition_job` → refresh signal → re-read | `update_data_to_partition_job_table(...)` |
| Run now | POST `/jobs/{id}/run` `{confirm_drop}` | readiness check → `run_partition_job_manual` | `SELECT status,message,execution_duration_ms FROM run_partition_job_manual(id)`; commits, then MANUAL_FAIL → HTTP 502 |
| History | GET `/logs?limit` (1–1000) | `get_partition_job_logs` | SELECT log ORDER BY job_runtime DESC |
| Dashboard | GET `/dashboard/summary` | jobs + 200 logs + readiness + scheduler status | 3 connections |
| Readiness | GET `/readiness` | `get_database_readiness` | privilege introspection (`has_*_privilege`) |
| System | GET `/system/readiness?refresh` | `system_readiness.build_system_readiness` | 10 s in-process cache |
| Scheduler | GET `/scheduler/status`, POST `/scheduler/refresh` | `scheduler_client` (urllib, 2 s timeout) | none |
| Cron | POST `/cron/preview` | `job_autofill.validate_six_field_cron` / `calculate_next_run` | none |
| pgAgent | GET `/pgagent/jobs`, `/pgagent/jobs/{id}` | `get_pgagent_jobs` / `get_pgagent_job_details` | pgAgent catalogs (optional `PGAGENT_DB_*`) |
| Missed runs (5A) | GET `/attention?status=pending\|acknowledged\|manually_rerun\|all` | `get_partition_job_attention` | attention ⟕ job (current `next_run_time`); table absent → `available:false` |
| Recover missed (5A) | POST `/attention/{id}/recover` `{confirm, confirm_drop, confirm_target}` | `recover_partition_job_attention` | `run_partition_job_attention_recovery(id)`; MANUAL_FAIL → 502 after commit; busy → 409 |
| Acknowledge (5A) | POST `/attention/{id}/acknowledge` `{confirm, note}` | `acknowledge_partition_job_attention` | `acknowledge_partition_job_attention(id, note)` |

Phase 5A error mapping: `DatabaseConflictError` → 409 (busy target, already
reviewed, concurrent NOWAIT, config drift) and `DatabaseNotFoundError` → 404.
Manual "Run now" on a busy target → 409.

Error handling and request rules:

- `ValidationError` → 400; `PgAgentNotInstalledError` → 503; `DatabaseError` → 502.
- `_map_psycopg_error` maps SQLSTATEs to safe messages.
- Values are always bound parameters. Only schema and function identifiers,
  which come from env and are regex-validated, are interpolated into SQL.
- Create and update use a single `conn.transaction()` on a fresh connection.
- Edits that switch CREATE↔DROP or change the target table require
  `confirm_dangerous=true`.

## Scheduler

**CURRENT** flow (`scheduler_backend/scheduler.py`):

```text
startup: load .env.realtime → OS singleton lock file → control API :8765 → run()
refresh_queue(reason):
   thread: open conn → SELECT current_database() == REALTIME_DB_EXPECTED_NAME → commit
           → get_upcoming_partition_jobs(lookahead=120s)  [includes OVERDUE rows]
   filter out keys in _active_keys and RecentOccurrenceCache (TTL 300 s, 512 max)
   queue.replace_from_jobs (heap by monotonic due time)
   heartbeat gauge; then count_failed_jobs (2nd connection) → jobs.failed_count gauge
loop:
   _spawn_due_jobs: pop due → create task (_active_keys guards duplicates)
   if refresh_event: refresh → spawn → continue          (NO wait on this path)
   if now >= next_reconcile (30 s): refresh → spawn → continue
   else wait on refresh_event with timeout = max(0.05, min(next due, reconcile, backoff if last refresh errored))
task: async with Semaphore(MAX_CONCURRENT_JOBS) → to_thread(execute_scheduled_job)
   → new conn → identity check → commit → conn.transaction(): run_partition_job_scheduled(job_id, expected)
   → finally: discard key; request_refresh()   (every completion triggers a refresh)
shutdown: SIGTERM → stop spawning → drain active tasks up to 120 s → cancel rest
```

**Committed ("handled") results** are cached by the rapid-loop breaker
(TTL 300 s) and clear retry state: EXECUTED, FAILED (SQL-handled worker
failure), FAILED_INVALID_SCHEDULE, MISSED_RESCHEDULED, MISSED_INVALID_SCHEDULE.

**Uncommitted results go through `scheduler_backend/retry_policy.py`**
(`OccurrenceRetryTracker`, keyed by `(job_id, expected_run_time)`; Phase 5A):

| Outcome | Behaviour |
|---|---|
| `FAILED_DATABASE` / `FAILED_CONNECTION` | `RETRY_MAX_ATTEMPTS` (4) attempts, `RETRY_DELAYS_SECONDS` between them (15, 60, 180 s; last repeats), then **exhausted** |
| `SKIPPED_TARGET_LOCKED` / `SKIPPED_LOCKED` | not a failure: cooldown `TARGET_LOCK_COOLDOWN_SECONDS` (5/10/20/30 s, last repeats) for up to `TARGET_LOCK_MAX_WAIT_SECONDS` (3600), then exhausted |
| any other non-committed status | ≥ 1 s cooldown; cap 20 |

- An exhausted occurrence is never executed again. The next dispatch calls
  `reconcile_missed_partition_job(..., 'RETRY_EXHAUSTED')`, which records an
  attention item and advances `next_run_time`. If that call fails, it backs off
  60 s doubling to 600 s.
- `refresh_queue` passes each occurrence's `not_before` time into
  `ScheduleQueue.replace_from_jobs`, so a refresh can never make a backed-off
  occurrence due early.
- Signal-driven refreshes are coalesced to at most one per second; the first
  signal after a quiet period is handled immediately.
- State is pruned after each successful refresh: occurrences no longer in the
  DB snapshot are dropped, plus a 24 h TTL and a 1024-entry cap.

`_backoff` (5 s doubling to 60 s) still applies when a *refresh* fails.

**Overdue handling (Phase 5A hardening): ownership, not a fixed window.**
`OccurrenceOwnership` (in `retry_policy.py`) classifies each occurrence ONCE,
at first sighting in a refresh, from the DB-clock `delay_seconds`. The
classification is sticky and lives in process memory only.

- **OWNED:** first seen before it was due, or no more than
  `PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS` (default 60 s; timer and
  refresh jitter only) after it.
  - Dispatched to `run_partition_job_scheduled`, which does full DB
    revalidation.
  - May run late after waiting for a slot, a busy table or a backoff.
  - Never classified "missed" just because the wall clock advanced.
- **HISTORICAL:** first seen later than the tolerance (start after downtime, or
  refreshes failing during an outage).
  - Never executed.
  - `reconcile_missed_partition_job(..., 'MISSED_SCHEDULED_RUN')` revalidates
    under the job lock, inserts an attention row and moves `next_run_time` to
    the first future occurrence.
  - No DDL runs and `last_run_*` is untouched.
- **DB safety ceiling:** `partition_job_max_execution_lateness()` (default
  6 h, DBA-tunable). The DB never executes beyond it and reconciles as missed
  instead. This is defence in depth, not the primary rule.
- **Restart:** ownership is forgotten, so occurrences already overdue at
  startup are HISTORICAL by design.
- **Startup check:** requires `LOOKAHEAD_SECONDS >= RECONCILE_SECONDS`.

## Database model

**CURRENT** tables in `mubasher_oms` (from `sql/bootstrap_partition_job_framework.sql`):

- `partitioning_job_table`:
  - `job_id numeric PK`, `job_name`, `is_enabled`, `table_schema`, `table_name`
  - `db_config_para jsonb`, `frequency interval`
  - `last_run_time`, `next_run_time`, `last_run_status` (`SUCCESS`/`FAIL`)
  - `partition_unit`, `partition_period`, `is_create`, `create_drop_interval`, `job_schedule` (6-field cron)
- `partitioning_job_table_log`:
  - `job_log_id`, `job_id`, `job_name`, `last_run_status`, `job_runtime`, `job_error`
  - `execution_duration_ms bigint` (migration 01)
  - Status values: SUCCESS, FAIL, MANUAL_SUCCESS, MANUAL_FAIL.
- Sequences: `seq_partitioning_job_id` and `seq_partitioning_job_log_id`.
- Type: `cron_schedule_result(schedule_interval, next_run_time)`.
- Trigger: `trg_set_default_db_config_para` (NULL → `{}`).
- Indexes: `(is_enabled, next_run_time, job_id)` and `log(job_id, job_runtime DESC)`.

There is no target/host column.

**Phase 5A** (migration `20261110_01`) adds `partition_job_attention`. It holds
not-executed scheduled occurrences and their DBA review outcome:

- **Identity:** `attention_id` is an identity column; `job_id` is an FK to the job table.
- **Detection snapshot:** `job_name`, `table_schema`, `table_name`, `is_create`.
- **Detection evidence:**
  - `reason`: `MISSED_SCHEDULED_RUN` or `RETRY_EXHAUSTED`
  - `missed_run_time`, `detected_at`
  - `rescheduled_to` (NULL means fail closed)
  - `detail`
- **Review lifecycle:**
  - `status`: `PENDING_REVIEW` → `MANUALLY_RERUN` or `ACKNOWLEDGED`
  - `reviewed_at`, `resolution`
  - `manual_execution_result`, `manual_execution_message`,
    `manual_execution_duration_ms`, `manual_executed_at`
- **Constraints and indexes:**
  - UNIQUE `(job_id, missed_run_time)`
  - partial index on PENDING rows
  - guard trigger: evidence columns are immutable, and terminal states are final.

## SQL functions

| Function | Source in repo | Security | Notes |
|---|---|---|---|
| `insert_data_to_partition_job_table(12 args)` | **NOT in repo** (import from reference DB) | unknown | create path |
| `update_data_to_partition_job_table(13 args) → void` | `migrations/20261013_02` | **SECURITY DEFINER**, owner `mubasher_oms`, `search_path=pg_catalog,mubasher_oms,pg_temp`, PUBLIC revoked, EXECUTE → `partition_job_ui` | FOR UPDATE; see "Enable and disable" below |
| `run_partition_job_manual(numeric) → (status,message,execution_duration_ms)` | `migrations/20261110_02` (prev. 20260929_03) | INVOKER, owner `mubasher_oms`, EXECUTE → `partition_job_ui` only | 5A: target lock 87421032 (busy → `MANUAL_TARGET_LOCKED`, no log); allowlist; never changes next/last_run_*; does not check `is_enabled` |
| `get_upcoming_partition_jobs(interval)` | `realtime_scheduler_v1.sql` | INVOKER, STABLE | includes overdue |
| `run_partition_job_scheduled(numeric, timestamp)` | `realtime_scheduler_v1.sql` (= `migrations/20261110_02` body; history: 20261007_01, 20261013_01) | INVOKER | job lock 87421031 → FOR UPDATE → disabled/stale/not-due checks → **5A: safety ceiling — beyond `partition_job_max_execution_lateness()` (6 h) → reconcile as missed (no DDL)** → target lock 87421032 → validate future next → allowlisted `set_config` → worker → UPDATE + log |
| `reconcile_missed_partition_job(numeric, timestamp, text, text)` | `realtime_scheduler_v1.sql` / `migrations/20261110_02` | INVOKER, EXECUTE → scheduler | 5A: attention row + next_run_time → first future occurrence; no DDL; no last_run_* change |
| `run_partition_job_attention_recovery(bigint)` | `migrations/20261110_02` | INVOKER, EXECUTE → `partition_job_ui` | 5A: lock item NOWAIT, require PENDING + unchanged target/op, run via `run_partition_job_manual`, record result; never changes next_run_time |
| `acknowledge_partition_job_attention(bigint, text)` | `migrations/20261110_02` | INVOKER, EXECUTE → `partition_job_ui` | 5A: ACKNOWLEDGED, no DDL |
| `partition_job_max_execution_lateness()` / `partition_job_config_param_allowed(text,text)` | `migrations/20261110_01` | SQL, IMMUTABLE | 5A: DB safety ceiling (6 h); db_config allowlist (mirrored in `validators.py`) |
| `cron_to_interval_or_next_run(text)` | **NOT in repo** (ISSUE-003) | unknown | interval result ⇒ next = now + interval (drifts with execution time) |
| `create_any_table_partition(...)`, `drop_any_table_partition(...)` | **NOT in repo** | unknown | the real DDL workers |
| `run_partition_create_jobs()` / `run_partition_drop_jobs()` | NOT in repo | — | legacy polling runners; must NOT run against the same DB as the realtime scheduler |

**Enable and disable** in `update_data_to_partition_job_table`:

- **Disable** keeps `next_run_time`, `last_run_*` and history.
- **Re-enable**, a schedule change, or a stale/NULL `next_run_time`
  recalculates one future `next_run_time`. If that fails, the update fails
  closed with SQLSTATE 22023.

Statuses returned by `run_partition_job_scheduled`:

- EXECUTED, FAILED, FAILED_INVALID_SCHEDULE (these set `next_run_time = NULL`)
- SKIPPED_LOCKED, SKIPPED_TARGET_LOCKED, SKIPPED_DISABLED, SKIPPED_RESCHEDULED, SKIPPED_NOT_DUE
- NOT_FOUND

## Security model

**CURRENT**

- **DB roles:**
  - `partition_job_ui` (API): USAGE on schema, SELECT on the job and log tables,
    EXECUTE on the insert, update and manual functions.
  - `partition_job_scheduler` (scheduler): SELECT on the job table, column
    UPDATE on `(last_run_time, last_run_status, next_run_time)`, INSERT on the
    log, EXECUTE on the realtime functions and the workers/cron helper.
  - `mubasher_oms` owns the DEFINER function.
- **No broad table UPDATE** for the UI role. Edit goes through the DEFINER function only.
- The manual run is SECURITY INVOKER as `partition_job_ui`, so the UI role's
  effective DDL rights on target tables depend on the worker functions, which
  are not in the repo. **Verify.**
- **Identifiers:** validated by regex in Python (`validators.py`) and as plain
  identifiers for env-configured object names. All values are bound parameters.
- **`db_config_para`** must be a JSON object, and since Phase 5A (source) its
  keys are **allowlisted**:
  - **Allowed keys:** `lock_timeout`, `statement_timeout`,
    `idle_in_transaction_session_timeout`, `work_mem`, `maintenance_work_mem`,
    `effective_io_concurrency`, `random_page_cost`,
    `max_parallel_maintenance_workers`, and `datestyle` with ISO output only.
  - **Where it is enforced:** in `validators.py` on create and edit, in
    `update_data_to_partition_job_table`, and before every `set_config` in the
    scheduled and manual functions.
  - **Rejected:** `role`, `session_authorization`, `search_path` and any other key.
- **`partition_job_attention` grants:** the UI role has SELECT plus
  column-level UPDATE of the review columns only (enforced by the guard
  trigger). The scheduler has SELECT and INSERT. Nobody has DELETE.
- **No authentication or authorization** in the UI or API. Security relies on
  network restriction: 8501 open to the admin network only; 8001 and 8765 on
  localhost.
- **Secrets:** none in source; `.env*` is gitignored. Errors and readiness
  output are scrubbed of secret-like keys. Metrics have forbidden tag keys.

## AWS Secrets Manager

**CURRENT** (`db_credentials.py`, shared by API and scheduler):

- **Providers:** `PARTITION_DB_SECRET_PROVIDER=env` (default; `DB_USER`/`DB_PASSWORD`)
  or `aws`.
- **AWS mode:**
  - Uses the boto3 default chain (EC2 instance role; no keys in config).
  - `GetSecretValue(PARTITION_DB_SECRET_ID)` must return a JSON SecretString:
    `{"password": "...", "username"?: "..."}`. If `username` is absent, `DB_USER` is used.
- **Cache:** process-global, one entry, TTL `PARTITION_DB_SECRET_CACHE_TTL_SECONDS`
  (default 300 s, minimum 1). It is not single-flight, so concurrent misses can
  fetch twice.
- **Auth failure** (SQLSTATE 28000/28P01) invalidates the cache and retries
  exactly once.
- **Fail closed:** AWS mode never falls back to `DB_PASSWORD`. Errors are mapped
  to safe messages.
- **Single secret, single target.** There is no per-target secret support.
- `PGAGENT_DB_PASSWORD` is still a plain env value if a separate pgAgent DB is used.

## Datadog / observability

**CURRENT** (`observability.py`):

- Stdlib UDP DogStatsD to `PARTOPS_DOGSTATSD_HOST:PORT` (127.0.0.1:8125).
- Off unless `PARTOPS_DATADOG_ENABLED=true`. The Agent owns the API key.
- The only base tag is `env`.

Metrics actually emitted (namespace `partops`):

| Metric | Type | Tags | Emitted |
|---|---|---|---|
| `scheduler.heartbeat` | gauge=1 | env | each successful refresh |
| `scheduler.active_jobs` / `max_concurrent_jobs` / `queue_depth` / `available_slots` / `overdue_jobs` | gauge | env | every concurrency-status update (refresh, task start/end) |
| `scheduler.refresh_failure` | counter | env | refresh exception |
| `scheduler.execution_exception` | counter | env | Python exception in execute |
| `jobs.failed_count` | gauge | env | after successful refresh: enabled jobs with `last_run_status='FAIL'` **excluding those whose latest later manual run is MANUAL_SUCCESS** (5A hardening; falls back to the plain FAIL count if the scheduler lacks SELECT on the log); not emitted on query error |
| `job.execution` | counter | status, operation(create/drop) | every scheduled attempt (event counter; can be huge in a loop) |
| `job.duration_ms` | timing | status, operation | when DB returned a duration |
| `db.connection_failure` | counter | component(api/scheduler) | connect failure |
| `secrets_manager.refresh` | counter | env | real AWS fetch only |
| `secrets_manager.retrieval_failure` | counter | reason(access_denied/not_found/invalid_secret/aws_error) | fetch failure |
| `api.request` / `api.error` (≥500) | counter | method, status_class | every API request |
| `jobs.pending_review_count` | gauge | env | 5A: PENDING_REVIEW attention items; throttled ≤ 1/60 s unless a miss occurred; not emitted if table absent |
| `missed_run.detected` / `missed_run.reconciled` | counter | reason (missed_scheduled_run/retry_exhausted), outcome (rescheduled/invalid_schedule) | 5A: MISSED_* outcome |
| `scheduler.retry_scheduled` / `scheduler.retry_exhausted` | counter | status | 5A: backed-off retry scheduled / budget exhausted |

Cardinality and logging:

- Tags are low-cardinality only. `job_id`, `job_name`, table, host, secret and
  error keys are blocked.
- Logs go to stdout → journald as `event=... key=value` lines. `job_id` is allowed in logs.
- Manual runs emit **no** job metrics.

## Current scheduler safety rules

**CURRENT** safeguards in the code:

- OS singleton lock file.
- `REALTIME_DB_EXPECTED_NAME` identity check on every connection (fail closed).
- The identity-check transaction is committed before the business
  `conn.transaction()`. This was the root-cause fix for the Job 33 loop (ISSUE-001).
- SQL rejects disabled, stale (`expected ≠ next_run_time`) and not-due
  occurrences under FOR UPDATE.
- The next occurrence is validated as future and ≠ T **before** the DDL. If it
  is invalid, `next_run_time = NULL` and the job fails closed.
- Per-job and per-target advisory transaction locks.
- A bounded TTL cache for committed transitions.
- Bounded shutdown drain.

**Phase 5A safeguards (source; ISSUE-007, production verification pending):**

- **Retries:** bounded, delayed retries for every uncommitted outcome (see
  Scheduler above).
- **Zero-delay paths:** none remain.
  - Every non-committed outcome waits at least 1 s.
  - The queue honours `not_before`.
  - Signal refreshes are coalesced.
- **Exhausted occurrences** go to DB reconciliation and are never executed again.
- **Result timestamps** are rendered with `to_char` (`scheduler_database.py`).
  This makes them DateStyle-independent; a non-ISO transaction DateStyle used to
  break psycopg *after* the work ran.
- **Regression tests:** `test_phase5a_missed_run_and_retry.py`
  (`SchedulerLoopRegressionTests`) runs the real loop with an always-overdue
  occurrence and asserts exactly one execution.

Rules for any future scheduler change:

- Never busy-loop.
- Every retry must be delayed and bounded, and must be tracked per
  `(job_id, expected_run_time)`.
- Do not cache uncommitted outcomes as "handled".

## Overdue/missed-run policy

**CURRENT (Phase 5A source; migrations prepared, not applied):** implemented as
specified below.

- **Missed rule:** ownership-based (see Scheduler); the DB has a 6 h safety
  ceiling. There is no fixed 10-minute window.
- **Review surfaces:**
  - an amber banner and row highlight on the Jobs page;
  - a *Needs review* card in the job detail panel;
  - an Overview banner.
- **Recovery API:**
  - `POST /api/attention/{id}/recover` requires `confirm`. A DROP also
    requires `confirm_drop` plus the typed `schema.table`.
  - `POST /api/attention/{id}/acknowledge` requires `confirm`.
- **Locking and side effects:** recovery runs through `run_partition_job_manual`,
  so it takes the target lock and writes MANUAL_* logs. It never changes
  `next_run_time`.
- **Recovery that is refused by config drift:** recovery is refused if the
  job's operation or target changed since detection.
- **Open choices:**
  - recovery does not update `last_run_status`, which keeps its meaning of
    "latest *scheduled* outcome". Instead, the failed-jobs gauge ignores a FAIL
    that was followed by a successful manual run;
  - there is no "reopen" for acknowledged items.

The specification (all points implemented in source):

- **Detection:** on discovery of an overdue occurrence, do **not** auto-run it.
  Record it as a missed occurrence with the scheduled time and the detection time.
- **Rescheduling:** advance `next_run_time` to the first legitimate **future**
  occurrence and continue normal scheduling.
- **DBA review:** the DBA reviews each missed occurrence and chooses
  **Run missed occurrence manually** or **Acknowledge / do not execute**.
- **Manual recovery** must not shift the already calculated normal schedule.
  Example: missed 20 Oct 02:00, detected 26 Oct, next run 27 Oct 02:00. A manual
  run on 26 Oct 14:00 keeps the next run at 27 Oct 02:00. Today
  `run_partition_job_manual` already leaves `next_run_time` alone.
- **Display:** missed runs show as warning / needs-review, **not** as an
  execution FAIL.
- **Historical vs late-but-owned:** decided — ownership at first sighting
  plus a 60 s jitter tolerance; DB ceiling 6 h.

## Database framework (Phase 5B)

**CURRENT (in source; nothing applied to any real database):**

- **Single source of truth:** `partition-job-ui/sql/framework/framework_manifest.json`
  lists every required object with its signature, purpose, callers, scope
  (api, scheduler, manual_run, migration), owner, security mode, search_path,
  grants and its status in Git.
  - `sql/framework/INVENTORY.md` is generated from it by
    `scripts/framework_inventory.py`. A test fails if it is stale.
  - `framework_manifest.py` loads and validates the manifest, takes a
    READ-ONLY catalog snapshot (`SET TRANSACTION READ ONLY`, then rollback)
    and evaluates PASS/WARNING/FAIL.
- **PARTOPS-OWNED objects (in Git):**
  - functions: `run_partition_job_scheduled`, `get_upcoming_partition_jobs`,
    `reconcile_missed_partition_job`, `run_partition_job_manual`,
    `update_data_to_partition_job_table`, `run_partition_job_attention_recovery`,
    `acknowledge_partition_job_attention`, `partition_job_max_execution_lateness`,
    `partition_job_config_param_allowed`, `fn_partition_job_attention_guard`;
  - tables and trigger: `partition_job_attention` + trigger,
    `partops_framework_version`.
- **EXISTING / LEGACY framework objects:**
  - Reconstructed by `bootstrap_partition_job_framework.sql`: job/log tables,
    sequences, `cron_schedule_result`, the `fn_set_default_db_config_para`
    trigger.
  - **Authoritative definition NOT in Git** (status `capture_required`;
    placeholders in `sql/framework/captured/`):
    - `cron_to_interval_or_next_run(text)`
    - `create_any_table_partition(varchar, varchar, varchar, integer, interval)`
    - `drop_any_table_partition(varchar, varchar, interval)`
    - `insert_data_to_partition_job_table(12 args)`
    - `cron_field_to_int_array` (existence and signature unverified)
  - Tracked as ISSUE-003 and ISSUE-008, both OPEN.
  - Legacy runners `run_partition_create_jobs` / `run_partition_drop_jobs` are
    never called. Preflight flags enabled pgAgent steps that call them.
  - `get_upcoming_partition_jobs` is PartOps-owned and in Git
    (`realtime_scheduler_v1.sql`). It is NOT a missing object, but v1's
    dependency gate stops it being installed until the legacy functions exist.
- **Capture:** `sql/capture_reference_framework.sql` is READ ONLY
  (`BEGIN TRANSACTION READ ONLY` … `ROLLBACK`; a unit test checks for write
  keywords). A DBA runs it against the approved reference DB. The workflow is
  in `sql/framework/README.md` and RUNBOOK §16. Every captured body needs
  `SECURITY_REVIEW_CHECKLIST.md`. Never invent a body; a test fails if one
  appears in `sql/`.
- **Framework version:**
  - Table `mubasher_oms.partops_framework_version` (migration `20261110_03`,
    SELECT-only for app roles); the database version is `max(migration_id)`.
  - The application expects the manifest's `framework_version` = `20261110_03`.
  - Equal → PASS; older or missing → FAIL; newer → WARNING.
  - Only reviewed migrations write the table; there is no auto-upgrade.
- **Preflight:** `scripts/preflight_framework.py` (DBA, READ ONLY) checks:
  - presence and signatures;
  - owner, security mode and search_path;
  - EXECUTE grants and PUBLIC EXECUTE;
  - tables and columns, table and sequence privileges;
  - framework version and legacy pgAgent runners.

  It supports `--snapshot-out` / `--snapshot-in` and exits 2 on FAIL.
- **Scheduler start gate:** the scheduler checks the scheduler scope as its
  own role at startup.
  - While anything FAILs it dispatches nothing (no execution, no reconcile)
    and re-checks every `PARTITION_SCHEDULER_FRAMEWORK_RECHECK_SECONDS` (300).
  - This is always on via `load_scheduler_config`. Configs built directly in
    tests default it off.
  - Visible as status `framework_status` and metric
    `partops.scheduler.framework_ready`.
- **System page:** readiness check "Database framework (manifest)", API scope.
- **Integration-test status:**
  - `scripts/disposable_db/run_disposable_db_test.sh` uses existing PG
    binaries and a throwaway `initdb` cluster, and destroys it afterwards.
  - On PostgreSQL 16 all runnable stages pass, and the preflight FAILs exactly
    on the uncaptured objects.
  - The full plan (`INTEGRATION_TEST_PLAN.md`, `40_integration_full.sql`,
    `test_framework_integration.py`) SKIPS with
    "FRAMEWORK DEFINITION NOT AVAILABLE".
  - EDB has not been tested (`EDB_COMPATIBILITY.md`, ISSUE-009). There is a
    potential EDB `sys` search_path risk in the DEFINER edit function
    (ISSUE-010, unproven).

**PLANNED:**

1. A DBA captures the legacy definitions from the approved reference DB and
   reviews them.
2. Commit the reviewed captures and update the manifest.
3. Run the full integration on a disposable PostgreSQL, then on EDB.

## Hub-local multi-database architecture (Phase 5C foundation)

**ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.** Each hub's PartOps knows only its own
hub's databases.

**Decision (Phase 5C):** job configuration is **centralised in the control
database** — the existing PartOps database. It holds the registry, all job
rows, logs, attention items and the framework version, and is itself the
default target `db01` (`is_control_database = TRUE`).

**IMPLEMENTED in source (Phase 5C, 2026-11-17; not deployed):**

- Registry `mubasher_oms.partops_database_target`
  (migration `20261117_01`): key, display name, host/port/database, credential
  provider (`inherit`/`env`/`aws`), secret *identifier* only, region, sslmode,
  environment, enabled, control flag, `max_concurrent_jobs` (default 1).
  No passwords, keys or secret values. App roles: SELECT only (DBA writes).
- `partitioning_job_table.database_target_id`: backfilled to `db01`, DEFAULT
  `partops_control_target_id()`, NOT NULL, FK RESTRICT. `db_config_para`
  stays session configuration only.
- `database_targets.py`: `DatabaseTarget` model (fail-closed validation,
  secret-safe `repr`/public dict), per-target credentials and per-secret cache
  (auth failure invalidates only that target's entry), target connection
  factory with `current_database()` verification, read-only per-target health
  (`HEALTHY`, `HEALTHY_WITH_WARNINGS`, `DISABLED`, `CONFIG_ERROR`,
  `UNREACHABLE`, `AUTH_FAILED`, `WRONG_DATABASE`, `FRAMEWORK_INCOMPATIBLE`)
  reusing the Phase 5B manifest (`target_execution` scope for managed targets).
- API: `GET /api/targets`, `/api/targets/{id}`, `/api/targets/{id}/health`
  (read-only). Job create/edit accept `database_target_id`; a change is a
  dangerous change (Phase 5C refused it with 409; lifted by Phase 5D); non-control targets
  are refused for new jobs.
- SQL executors (migration `20261117_02`, framework version `20261117_02`)
  skip jobs of non-control targets: `SKIPPED_REMOTE_TARGET` /
  `MANUAL_REMOTE_TARGET`; `get_upcoming` returns control-database jobs only.
- Scheduler: gate also requires exactly one enabled control target matching
  the connected database (else blocked); occurrences carry
  `database_target_id` (`occurrence_id` = target, job, expected time); target
  lock resource key includes the target id; metrics carry a low-cardinality
  `target` tag (registry key).
- Readiness: "Database targets" category, one item per target, isolated.
- Disposable validation: PG16 throwaway cluster with a second database
  (`partops_target2`) proves backfill, guards, routing and health isolation.

**NOT implemented (later phases):**

- ~~Phase 5D~~ — implemented, see below.
- **Phase 5E:** multi-target UI (target selector, filters).
- **Phase 5B.1 — DEFERRED:** importing the authoritative legacy definitions.
  Production is the only source and production capture is not permitted.
  The four legacy definitions are not in Git and were not invented;
  ISSUE-003 / ISSUE-008 / ISSUE-009 / ISSUE-010 stay OPEN.

## Remote execution (Phase 5D, 2026-11-17; in source, not deployed)

Design reference: `partition-job-ui/REMOTE_EXECUTION.md`.

- **Control DB** (= db01) is authoritative for job definitions, registry,
  schedule, last status, history, attention, the execution-attempt ledger
  `partops_execution_attempt` and scheduler coordination. **Targets** only run
  the legacy workers; no PartOps tables there.
- db01 jobs → unchanged `run_partition_job_scheduled`. db02+ jobs → remote
  orchestrator: `partops_remote_claim` (PREPARED) → target txn (identity,
  advisory 87421032, allowlisted settings, worker, COMMIT) with
  `partops_remote_mark_dispatching` before the worker → `record_success`
  (TARGET_SUCCEEDED) → `partops_remote_finalize` (FINALIZED).
- **No distributed transaction.** Unprovable outcome → OUTCOME_UNKNOWN: never
  retried, `REMOTE_OUTCOME_UNKNOWN` attention, job paused (next NULL), edits /
  re-enable refused (55006) until `partops_resolve_remote_outcome`. Proven
  failures → Phase 5A bounded retry → RETRY_EXHAUSTED. Confirmed commit →
  finalization-only retries. Restart: DISPATCHING → UNKNOWN; TARGET_SUCCEEDED
  → finalize.
- Concurrency: global + per-target (`max_concurrent_jobs`), target slot first
  then global; db01 honours its limit too (default 1 — raise it to keep Phase 2
  parallelism). Per-target cooldown 15→300 s; per-target preflight cache 300 s.
- Manual runs / missed-run recovery of db02+ jobs: API → scheduler
  `/internal/scheduler/manual-run` (same engine).
- API: `/api/execution-attempts` (+ resolve); job create/edit accept healthy
  managed targets; history has target/attempt columns.
- Migrations `20261117_03`, `20261117_04`; framework version `20261117_04`.
- Validated: unit tests + disposable PG16 multi-DB run with TEST-ONLY stub
  workers (orchestration only; never production logic).

## Multi-target operations UI (Phase 5E, 2026-11-17; in source)

- One hub only (no hub selector). Shared `HubProvider`
  (`frontend/src/components/hub/hub-context.tsx`): scheduler status every 30 s,
  target registry every 5 min, target health on demand (cached 60 s).
- `/targets` (read-only), target column / filter on Jobs and History, target
  section + warnings in the job drawer, target selector with separate
  target-change confirmation, remote manual-run messages (503 / 504 / unknown),
  attempts list + detail, OUTCOME_UNKNOWN resolution workflow, System hub
  status (DEGRADED vs DOWN) with per-target scheduler concurrency.
- Vocabulary centralised in `frontend/src/lib/targets.ts`; components in
  `frontend/src/components/targets/` and `components/attempts/`.
- Backend touched only to fix the stale `executable` field of
  `DatabaseTarget.to_public_dict()` (now `is_enabled`, Phase 5D semantics).
- Never in the UI: target administration, retry / force for unknown outcomes.

## Phase 5E.1 — integrated UI/API verification (2026-11-18)

- The Phase 5E UI was exercised end-to-end (real Next.js build → FastAPI →
  scheduler → disposable control DB + db02 + incompatible db03, test-only
  stubs, headless Chrome via DevTools). RUNBOOK §18.5 holds the executed
  matrix (28/28 PASS, several after fixes; CHANGE-019).
- Contract notes: scheduler `started_at` / `last_refresh_at` and target health
  `checked_at` are naive UTC (UI parses them as UTC); DB timestamps
  (next_run_time, history, attempts) are DB-local and shown as-is.
- 503 from a remote manual run now means "scheduler refused / unreachable,
  nothing started"; any other transport failure is the ambiguous 504 path.
- Authentication remains absent (ISSUE-014): PartOps must stay
  network-restricted until an approved authentication architecture exists.

## Concurrency

**CURRENT**

- `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS`: default 1 when unset. 0, negative,
  float, empty or non-integer values are rejected at startup.
  Rollout guidance: 1 → 2 → 3 only.
- Uses `asyncio.Semaphore(N)`. Blocking DB work runs in `asyncio.to_thread`,
  with one connection per active job.
- "Active" means holding a semaphore slot. "Queued" means the in-memory heap.
  Tasks waiting on the semaphore are in neither count but are in `_active_keys`.
- Duplicate guard: `_active_keys` in-process, plus the DB job advisory lock.
- Target guard: DB target advisory lock (non-blocking → `SKIPPED_TARGET_LOCKED`).
  Since 5A this gets a contention cooldown bounded by
  `TARGET_LOCK_MAX_WAIT_SECONDS` (1 h). The occurrence stays owned and is not
  turned into a missed run.
- **Manual run:** takes the same target lock (5A; ISSUE-005 fixed in source).
  A busy target returns `MANUAL_TARGET_LOCKED`, which the API returns as HTTP 409.
- **PLANNED:** per-target concurrency.

## Important environment/config names

**API `.env`**

- Connection: `DB_HOST`, `DB_PORT` (code default **5444**), `DB_NAME`, `DB_USER`,
  `DB_PASSWORD`, `DB_SSLMODE`, `DB_CONNECT_TIMEOUT`.
- Credentials: `PARTITION_DB_SECRET_PROVIDER`, `PARTITION_DB_SECRET_ID`,
  `AWS_REGION` / `AWS_DEFAULT_REGION`, `PARTITION_DB_SECRET_CACHE_TTL_SECONDS`.
- pgAgent: `PGAGENT_DB_*`.
- Object names: `PARTITION_JOB_SCHEMA`, `PARTITION_JOB_FUNCTION`, …
- Scheduler signal: `PARTITION_SCHEDULER_REFRESH_URL`,
  `PARTITION_SCHEDULER_STATUS_URL`, `PARTITION_SCHEDULER_REFRESH_TIMEOUT_SECONDS`.
- Frontend proxy: `PARTITION_API_ORIGIN`.
- Datadog: `PARTOPS_DATADOG_ENABLED`, `PARTOPS_DOGSTATSD_HOST`,
  `PARTOPS_DOGSTATSD_PORT`, `PARTOPS_ENVIRONMENT`, `PARTOPS_METRICS_NAMESPACE`.

**Scheduler `.env.realtime`**

- Identity: `REALTIME_DB_EXPECTED_NAME` (required).
- Connection: same `DB_*`; scheduler code defaults `DB_PORT` to **5432**.
- Credentials: same secret vars as the API.
- Timing: `PARTITION_SCHEDULER_LOOKAHEAD_SECONDS` (120), `_RECONCILE_SECONDS` (30),
  `_CONNECT_RETRY_SECONDS` (5), `_MAX_CONNECT_RETRY_SECONDS` (60).
- Concurrency: `_MAX_CONCURRENT_JOBS` (1).
- Control API: `_BIND_HOST` (127.0.0.1), `_BIND_PORT` (8765).
- Process: `_LOCK_FILE`, `_LOG_LEVEL`.
- Object names: `PARTITION_UPCOMING_JOBS_FUNCTION`, `PARTITION_SCHEDULED_RUN_FUNCTION`,
  `PARTITION_JOB_TABLE`.
- Tooling only: `PARTITION_REALTIME_ALLOW_MIGRATION`.
- Datadog: same `PARTOPS_*`.
- Phase 5A retry (optional):
  - `PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS` (60; 0–600)
  - `PARTITION_SCHEDULER_RETRY_MAX_ATTEMPTS` (4; 1–10)
  - `PARTITION_SCHEDULER_RETRY_DELAYS_SECONDS` (15,60,180)
  - `PARTITION_SCHEDULER_TARGET_LOCK_COOLDOWN_SECONDS` (5,10,20,30)
  - `PARTITION_SCHEDULER_TARGET_LOCK_MAX_WAIT_SECONDS` (3600; 30–21600)
  - `PARTITION_SCHEDULER_EXHAUSTED_RETRY_SECONDS` (60)
  - `PARTITION_SCHEDULER_FRAMEWORK_RECHECK_SECONDS` (300; 30–3600; Phase 5B gate)
  - Invalid values stop the scheduler at startup.
  - `PARTITION_RECONCILE_MISSED_FUNCTION`
  - `PARTITION_JOB_ATTENTION_TABLE`
- The DB safety ceiling is NOT an env var. It is the DB function
  `partition_job_max_execution_lateness()`.

**API attention objects (optional overrides):**
`PARTITION_JOB_ATTENTION_TABLE`, `PARTITION_ATTENTION_RECOVERY_FUNCTION`,
`PARTITION_ATTENTION_ACKNOWLEDGE_FUNCTION`.

## Important ports

| Port | Owner |
|---|---|
| 8501 | Next.js UI (0.0.0.0; admin network only) |
| 8001 | FastAPI (127.0.0.1) |
| 8765 | Scheduler control API (127.0.0.1) |
| 8125/udp | DogStatsD (Agent) |
| 5444 / 5432 | EDB / PG |
| **8000** | **Unrelated application. PartOps must never use or touch it.** |

## Current development status

| Area | Status |
|---|---|
| Core job management (list/create/convert/export) | Complete |
| Manual run | Complete (shared target lock since 5A; no metrics) |
| Scheduler (realtime, Phase 0 safety) | Complete in source; prod verification pending |
| AWS Secrets Manager | Complete (single target) |
| Parallel execution (Phase 2) | Complete in source; default 1 |
| Datadog (Phase 3 + failed_count gauge) | Complete in source |
| Edit/Enable/Disable (Phase 4) | Complete in source; migration 20261013_02 prod pending |
| Frontend GTN redesign + logo/loader | Complete (minor defects/drift) |
| Overdue reconciliation (missed-run policy) + DBA review | Complete in source (Phase 5A); migrations 20261110_01/02 not applied; prod verification pending |
| Retry/backoff | Complete in source (Phase 5A); prod verification pending |
| Manual-run target lock / db_config allowlist | Complete in source (Phase 5A) |
| DB framework model / capture / preflight / version gate (Phase 5B) | Complete in source; legacy definitions NOT captured (ISSUE-003/008 open) |
| Full integration tests | Written; SKIPPED until captures exist |
| EDB validation | Not started (ISSUE-009) |
| Hub-local multi-database foundation (Phase 5C) | Complete in source; migrations 20261117_01/02 not applied; disposable-validated |
| Per-target remote execution (Phase 5D) | Complete in source; migrations 20261117_03/04 not applied; disposable-validated with test-only stubs |
| Multi-target UI (Phase 5E) | Complete in source (frontend; ISSUE-012 closed) |
| Legacy definition import (Phase 5B.1) | DEFERRED — production is the only source; capture not permitted |
| Optimization | Not started |
| Runbook | Partial (good ops runbook; README has Streamlit-era drift) |
| Final handover | Not started |

Tests: see the latest report in `changes.md` (Phase 5A:
`test_phase5a_missed_run_and_retry.py`; Phase 5B: `test_framework_phase5b.py`,
`test_framework_integration.py`, where the integration suite SKIPS until
captures exist). There is no frontend test suite (lint and build only).

The Phase 5A migrations were smoke-tested on a **disposable PostgreSQL 16**
cluster (throwaway `initdb`, destroyed afterwards). That run did not include
the unversioned framework functions, so the real CREATE/DROP and cron-helper
paths are not covered. It has never run on EDB.

## Known critical issues

**Fixed in source by Phase 5A** (not yet deployed or verified in production):

- the scheduler immediate-retry loop (ISSUE-007);
- the missing missed-run policy;
- `db_config_para` keys not being allowlisted;
- the manual-run target lock (ISSUE-005).

**Still open:**

1. **No in-app authentication or authorization.** Anyone who can reach :8501
   can create, edit, DROP-run, recover or acknowledge.
2. **Core framework functions are not version-controlled:** cron helper,
   workers and insert (ISSUE-003, ISSUE-008).
   - Phase 5B added the capture tooling, manifest, preflight and scheduler
     gate.
   - The definitions themselves must still be captured by a DBA and reviewed.
   - Until then the Phase 5B scheduler refuses to dispatch on any database
     missing them.
3. **Phase 5A SQL has not run with the real framework functions or on EDB.**
   - It passed a disposable PostgreSQL 16 smoke test without the unversioned
     cron and worker functions.
   - Before production, validate it on a disposable copy that has the
     authoritative functions.

Important but not critical:

- `last_run_status` still shows FAIL after a successful manual fix. This is the
  established meaning (latest scheduled outcome). `failed_count` no longer
  counts such jobs; the UI "Last status" still shows the scheduled outcome.
- Existing jobs whose `db_config_para` holds non-allowlisted keys will FAIL at
  their next run until edited. Run the RUNBOOK §10.6 pre-check before
  deploying.
- An interval-type schedule drifts (next = now + interval).
- `DB_PORT` defaults differ (5444 API vs 5432 scheduler).
- The API has no expected-DB check.
- Python 3.9 / boto3 support horizon (ISSUE-006).

## Current TODO / next phases (PLANNED, not started)

1. Phase 5B follow-up:
   1. DBA capture and security review of the legacy definitions (RUNBOOK §16).
   2. Full disposable integration run.
   3. EDB validation.
2. Phase 5A/5B rollout:
   1. Run the RUNBOOK §10.6 db_config pre-check.
   2. Apply migrations `20261110_01`, `_02`, `_03`.
   3. Run the preflight (§16.6).
   4. Deploy, then verify in production.
3. Phase 5C/5D rollout: `20261117_01`…`_04`, preflight, deploy (RUNBOOK
   §10.7, §10.8). Phase 5E: multi-target UI — done in source. Phase 5F: optimization.
   Phase 5G: multi-host installation runbook. Phase 5B.1 stays deferred
   (ISSUE-003/008/009/010/013 open).
4. Resource optimization.
5. Docs cleanup and handover.

## Things that must never be changed casually

- The transaction contract in `scheduler_database.open_connection`: identity
  check, then `commit()`, then the business `conn.transaction()`.
- The `_HANDLED_OCCURRENCE_STATUSES` semantics. Only DB-committed transitions may be cached.
- Retry rules (Phase 5A): every non-committed outcome must get a delayed,
  bounded retry through `OccurrenceRetryTracker`.
  - Never re-dispatch an occurrence immediately.
  - Never execute an exhausted occurrence.
  - Never use `time.sleep` in the event loop.
- Missed-run decision (Phase 5A hardening):
  - It is classified by ownership at first sighting (DB-clock delay plus a
    jitter tolerance). Never use a fixed wall-clock lateness window.
  - Historical occurrences are only reconciled, never executed.
  - Every execution or reconcile is revalidated in SQL under locks, and the DB
    keeps its own max-lateness ceiling.
  - Recovery and acknowledge must never change `next_run_time`.
- Framework manifest (Phase 5B):
  - Any new or changed DB object must be added to `framework_manifest.json`
    and `INVENTORY.md` must be regenerated.
  - Never put an invented body for a `capture_required` object in Git.
  - Never weaken or disable the scheduler framework gate.
  - Capture and preflight scripts stay READ ONLY.
- The `db_config_para` allowlist must stay identical in `validators.py` and
  `partition_job_config_param_allowed` (unit-tested).
  - Never add identity- or resolution-changing GUCs.
- The SQL order in `run_partition_job_scheduled`:
  1. job lock
  2. FOR UPDATE
  3. checks
  4. target lock
  5. validate next
  6. worker
  7. UPDATE + log
- Fail-closed behaviour: no invented +1 day or frequency fallbacks.
- Advisory lock namespaces 87421031 (job) and 87421032 (target).
- DEFINER function hardening: owner `mubasher_oms`, fixed search_path,
  PUBLIC revoked, narrow EXECUTE. Never grant broad table UPDATE or DDL to app roles.
- `DROP FUNCTION` loses owner and grants. Prefer `CREATE OR REPLACE`; when a
  DROP is unavoidable, restore the owner and EXECUTE.
- Env file separation (`.env` vs `.env.realtime`) and file ownership. The API
  must stay on port 8001, never 8000.
- AWS fail-closed (no plaintext fallback). No secrets in logs, metrics or errors.
- Low-cardinality metric tags. `jobs.failed_count` semantics: enabled + FAIL
  rows, minus jobs whose latest later manual run succeeded. It counts one row
  per job, never attempts.
- Next.js + FastAPI + scheduler architecture. No pools, queues or brokers
  without explicit approval.
- Python 3.9 compatibility.

## Project principles (apply to all future work)

**Security**

- Security first. Never use broad DB grants as shortcuts.
- Preserve function ownership and search_path safety.
- No secrets or passwords anywhere, including Datadog. Keep metrics low-cardinality.

**Scheduler safety**

- The scheduler must never busy-loop.
- A DB failure must never create unlimited immediate retries.
- Overdue jobs must not cause catch-up storms.
- Missed work must be DBA-reviewable, and manual recovery must be deliberate.

**Multi-target**

- One failed DB target must not break healthy targets.
- One PartOps deployment per hub, about 2 DB targets each. Not a global control plane.

**Resources and scope**

- Minimise CPU, memory, DB sessions, AWS calls and scheduler wakeups.
- Do not touch unrelated services (port 8000 app, other units).

## Deployment workflow

1. Source changes are made **locally**.
2. Run the checks:
   - `python -m unittest discover -s . -p "test_*.py" -v` from `partition-job-ui/`
   - `npm run lint && npm run build` in `frontend/`
3. The user reviews.
4. The user commits and pushes.
5. On the server (operator, see RUNBOOK §8, §9, §10):
   1. `git pull --ff-only`
   2. Restore file owners.
   3. `pip install -r requirements.txt`
   4. `npm ci && npm run build` as partitionui
   5. A DBA applies the SQL migrations manually, in order (`sql/migrations/README.md`).
   6. Restart the units.
6. The scheduler stays stopped until SQL and Python match. Use the
   pre-start checklist in RUNBOOK §10.2.

## Git workflow

- Branch `main` tracks `origin/main`.
- **Never commit or push automatically.** The user reviews first.
- Never reset, stash or discard user work.
- Untracked backup ZIPs in the repo root (`partition-job-ui*.zip`, about 250 MB
  in total) are **not source**. Leave them alone.
- `partition-job-ui/archives/streamlit-ui-*.zip` is a tracked archive.
- Record issues in `partition-job-ui/issues.md` (ISSUE-NNN) and changes in
  `partition-job-ui/changes.md` (CHANGE-NNN).
