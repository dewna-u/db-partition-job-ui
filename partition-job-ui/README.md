
cd /opt/db-partition-job-ui-github

GIT_SSH_COMMAND='ssh -i /root/.ssh/partition_job_ui_github -o IdentitiesOnly=yes' \
git -c safe.directory=/opt/db-partition-job-ui-github pull --ff-only origin main

chown -R partitionui:partitionui /opt/db-partition-job-ui-github

# Install Python API deps + build Next.js UI
sudo -u partitionui /opt/db-partition-job-ui-github/.venv/bin/pip install -r /opt/db-partition-job-ui-github/requirements.txt
cd /opt/db-partition-job-ui-github/frontend && sudo -u partitionui npm ci && sudo -u partitionui npm run build

systemctl restart partition-job-api.service partition-job-ui.service

systemctl status partition-job-api.service partition-job-ui.service --no-pager -l





# Partition Job Configuration UI

Next.js + FastAPI operations console for configuring PostgreSQL/EDB partition jobs
through the existing database function and listing pgAgent jobs with their job IDs.

> Streamlit UI was archived to `archives/streamlit-ui-*.zip` (not deleted permanently).
> See `WEB.md` for the current run/deploy instructions.
> **Teammate install/redeploy guide:** [`RUNBOOK.md`](./RUNBOOK.md)

## 1. Purpose

Administrators use this UI to:

* View existing pgAgent jobs (including **Job ID**)
* Submit a new partition-job configuration by calling  
  `mubasher_oms.insert_data_to_partition_job_table(...)`, which stores the row in
  `mubasher_oms.partitioning_job_table`

The UI never inserts into the partition job table directly — the function is the
only write path.

The browser never connects directly to PostgreSQL. All database access happens from the FastAPI process on the database server (Next.js proxies `/api/*`).

## 2. Lightweight architecture

```text
Administrator’s Browser
        |
        v
Next.js UI on Database Server (:8501)
        |
        +--> FastAPI (127.0.0.1:8001) --> PostgreSQL/EDB
        |
        +--> scheduler_backend (separate systemd unit, 127.0.0.1:8765)

Note: Port 8000 is reserved by another unrelated application on the deploy host.
PartOps FastAPI must use 8001.
```

Design goals and current runbook: see `WEB.md`.

## 3. Project structure

```text
partition-job-ui/
├── frontend/                 # Next.js UI
├── api/                      # FastAPI facade
├── archives/                 # Streamlit UI zip (archived)
├── database.py
├── validators.py
├── job_autofill.py
├── scheduler_client.py
├── dashboard_metrics.py
├── scheduler_backend/
├── sql/
├── scripts/
│   └── check_migration_gate.py
├── systemd/
│   ├── partition-job-api.service
│   └── partition-job-scheduler.service
├── partition-job-ui.service  # Next.js UI unit
├── requirements.txt
├── .env.example
├── .env.realtime.example
├── WEB.md
└── README.md
```

## 4. Prerequisites

* Python 3.11 or newer
* PostgreSQL or EDB Postgres Advanced Server with the partition function installed
* pgAgent schema available in the same or a separate database (for job listing)
* A non-superuser database role for the application (see below)
* Linux host for production systemd deployment (Rocky Linux / RHEL family recommended)

## 5. Database permissions

**Do not connect as a database superuser.**

Create a dedicated role:

```sql
CREATE ROLE partition_job_ui
LOGIN
PASSWORD 'replace_with_a_strong_password';

GRANT CONNECT ON DATABASE your_database TO partition_job_ui;
GRANT USAGE ON SCHEMA mubasher_oms TO partition_job_ui;
```

### Execute on the partition function

`GRANT EXECUTE ON FUNCTION` requires the function’s exact signature. Discover it first (section 6), then:

```sql
REVOKE ALL ON FUNCTION
mubasher_oms.insert_data_to_partition_job_table(EXACT_PARAMETER_TYPES)
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
mubasher_oms.insert_data_to_partition_job_table(EXACT_PARAMETER_TYPES)
TO partition_job_ui;
```

Replace `EXACT_PARAMETER_TYPES` with the argument types returned by the discovery query.

### If the deployed object names differ

The application calls `mubasher_oms.insert_data_to_partition_job_table` by default.
If your deployment uses different names, set these optional environment
variables instead of editing code. Both are validated as plain PostgreSQL
identifiers and are never taken from user input:

```bash
PARTITION_JOB_SCHEMA=mubasher_oms
PARTITION_JOB_FUNCTION=insert_data_to_partition_job_table
```

The application calls the function using **named notation** (`p_job_name => ...`),
so the deployed parameter *names* must match. A mismatch surfaces as a clear
"parameter names and types do not match" error rather than silently binding
values to the wrong columns.

### SECURITY INVOKER vs SECURITY DEFINER

* If the function is **SECURITY INVOKER**, the UI role may also need the minimum necessary permissions on the destination table(s) the function writes to. Grant only what is required.
* If the function is **SECURITY DEFINER**:
  * Do **not** grant direct table permissions to `partition_job_ui` unless genuinely required.
  * A DBA must review the function owner.
  * The function should use a safe fixed `search_path`.
  * Untrusted users must not have `CREATE` permission on schemas included in that search path.

### Do not grant the UI role

* PostgreSQL superuser
* `INSERT` / `UPDATE` / `DELETE` / `TRUNCATE` / `TRIGGER` on pgAgent tables
* Ownership of the function
* Ownership of the destination table

## 6. Finding the exact function signature

Run this as a DBA:

```sql
SELECT
    p.oid::regprocedure AS function_signature,
    pg_get_function_identity_arguments(p.oid) AS argument_types,
    pg_get_function_result(p.oid) AS return_type,
    p.prosecdef AS is_security_definer
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'mubasher_oms'
  AND p.proname = 'insert_data_to_partition_job_table';
```

Use `argument_types` (or the full `function_signature`) when issuing `GRANT EXECUTE` / `REVOKE`.

## 7. pgAgent database configuration

For listing jobs:

```sql
GRANT CONNECT ON DATABASE pgagent_database TO partition_job_ui;
GRANT USAGE ON SCHEMA pgagent TO partition_job_ui;
GRANT SELECT ON TABLE pgagent.pga_job TO partition_job_ui;
GRANT SELECT ON TABLE pgagent.pga_jobstep TO partition_job_ui;
GRANT SELECT ON TABLE pgagent.pga_schedule TO partition_job_ui;
```

Job-detail auto-fill inspects the routine the pgAgent step calls, in the **main** database. The step may use `SELECT schema.function(...)` or `CALL [schema.]procedure(...)`, so the routine is resolved against `pg_proc` / `pg_namespace` by name, kind (`prokind`) and argument count, and its definition is read with `pg_get_functiondef`. An unqualified name is resolved only when exactly one non-system routine matches; otherwise the UI reports ambiguity instead of guessing. Reading the catalog needs `SELECT` on it, which `PUBLIC` already has. The UI never executes `jstcode` or the discovered routine.

An unqualified table referenced by the routine body is resolved the same way, through `pg_class` / `pg_namespace`, and the routine's own schema is used only to break a tie between identically named tables.

If the partition function and pgAgent live in the **same** database, leave the optional `PGAGENT_DB_*` variables blank in `.env` — the app reuses the main database settings.

If they live in **different** databases, set the `PGAGENT_DB_*` values accordingly.

### Restricted read-only view (optional)

If company policy does not permit direct `SELECT` on `pgagent.pga_job`, a DBA can create a restricted read-only view containing only:

* `jobid`
* `jobname`
* `jobenabled`
* `jobhostagent`
* `jobnextrun`
* `joblastrun`
* `jobdesc`

The application does **not** create that view. If you use a view, you would need a corresponding code/query change or an identical column layout exposed as `pgagent.pga_job`.

## 8. Installation

> For the **current live host**, use deploy path  
> `/opt/db-partition-job-ui-github/partition-job-ui` and follow **`RUNBOOK.md`**  
> (API **8001**, UI **8501**, scheduler **8765**).
>
> The `/opt/partition-job-ui` path below is a generic alternate layout only.
> Do not mix both trees on one server.

```bash
sudo mkdir -p /opt/partition-job-ui
sudo useradd --system \
  --home-dir /opt/partition-job-ui \
  --shell /sbin/nologin \
  partitionui

sudo cp -r partition-job-ui/. /opt/partition-job-ui/
sudo chown -R partitionui:partitionui /opt/partition-job-ui

sudo -u partitionui python3 -m venv /opt/partition-job-ui/.venv

sudo -u partitionui \
  /opt/partition-job-ui/.venv/bin/python \
  -m pip install --upgrade pip

sudo -u partitionui \
  /opt/partition-job-ui/.venv/bin/pip \
  install -r /opt/partition-job-ui/requirements.txt
```

## 9. Environment setup

```bash
sudo cp /opt/partition-job-ui/.env.example /opt/partition-job-ui/.env
sudo chown partitionui:partitionui /opt/partition-job-ui/.env
sudo chmod 600 /opt/partition-job-ui/.env
sudo vi /opt/partition-job-ui/.env
```

Replace at least:

| Variable | Placeholder to replace |
|---|---|
| `DB_NAME` | `your_database` |
| `DB_USER` | keep or change `partition_job_ui` |
| `DB_PASSWORD` | `replace_with_strong_password` (env provider) |
| `DB_HOST` / `DB_PORT` | if not local defaults |
| `PGAGENT_DB_*` | only if pgAgent is in another database |
| `PARTITION_DB_SECRET_PROVIDER` | `env` (default) or `aws` |

### Database credential providers

| Provider | Use | Credentials |
|---|---|---|
| `env` (default) | Local / rollback / first deploy | `DB_USER` + `DB_PASSWORD` |
| `aws` | Production (recommended) | AWS Secrets Manager JSON via EC2 IAM role |

AWS mode architecture:

```text
EC2 IAM instance role
        ↓
boto3 default credential chain
        ↓
Secrets Manager GetSecretValue
        ↓
JSON { "username"?, "password" }
        ↓
in-process cache (TTL, default 300s)
        ↓
psycopg connection
```

AWS mode variables: `PARTITION_DB_SECRET_ID`, optional `AWS_REGION`,
`PARTITION_DB_SECRET_CACHE_TTL_SECONDS`. No AWS access keys in PartOps config.
AWS mode **never** falls back to `DB_PASSWORD`. After verification, remove
`DB_PASSWORD` from `.env` / `.env.realtime`.

Never commit `.env`. It is listed in `.gitignore`.

## 10. Manual testing

See `WEB.md`. Quick production-style check after build:

```bash
sudo systemctl start partition-job-api.service partition-job-ui.service
curl -sS http://127.0.0.1:8501/ | head
curl -sS http://127.0.0.1:8001/api/health
```

Open:

```text
http://127.0.0.1:8501
```

Confirm:

* The page title is **Partition Job Configuration**
* **Existing pgAgent Jobs** loads once (or shows a clear pgAgent message)
* Form validation rejects empty/invalid fields without a traceback
* Successful create shows a success message and refreshes the job list once

## 11. Systemd setup

```bash
sudo cp /opt/partition-job-ui/systemd/partition-job-api.service \
  /usr/lib/systemd/system/partition-job-api.service
sudo cp /opt/partition-job-ui/partition-job-ui.service \
  /usr/lib/systemd/system/partition-job-ui.service

sudo systemctl daemon-reload
sudo systemctl enable --now partition-job-api.service partition-job-ui.service
sudo systemctl status partition-job-api.service partition-job-ui.service --no-pager -l
```

Units live under `/usr/lib/systemd/system/`. They run as `partitionui` / `partitionui`, not as root, and restart only on failure.

## 12. Internal network and firewall restriction

Access URL:

```text
http://DATABASE_SERVER_IP:8501
```

**Do not expose this UI to the public internet.** Port `8501` must be reachable only from the internal administrator network.

Rocky Linux firewall example (replace the placeholder CIDR):

```bash
sudo firewall-cmd --permanent \
  --add-rich-rule='rule family="ipv4" source address="ADMIN_NETWORK_CIDR" port port="8501" protocol="tcp" accept'

sudo firewall-cmd --reload
```

Also configure AWS/Azure security groups (or equivalent) so all other sources are rejected.

## 13. How to use the UI

1. Open the URL from an administrator workstation.
2. Review **Existing pgAgent Jobs** (Job ID is the first column).
3. Optionally enter a **pgAgent Job ID** and click **Load Job Details** to auto-fill the form. Review warnings and edit any field before submitting. Loading never creates or updates records.
4. If multiple SQL steps call different routines, select a step and click **Apply Selected Step**.
5. Fill or adjust **Create Partition Job**.
6. Click **Create Partition Job**.
7. Read the success/error message. On success the job list refreshes once.

There is no login page inside the app — rely on network controls and OS/database credentials.

## 14. How job IDs are displayed

* The jobs table always shows **Job ID** as the first column.
* After a successful create, the app compares job IDs before and after one refresh.
* If a new ID appears, it is shown explicitly.
* If none appears, the UI explains that the pgAgent job may be created later by the partition-job processor.

The function return value is displayed if present, but is **not** assumed to be a pgAgent job ID unless your database function actually returns one.

## 15. How to refresh the pgAgent job list

* Jobs load once when the Streamlit session starts.
* Click **Refresh Jobs** to reload manually.
* After a successful partition-job create, the list refreshes once automatically.
* There is **no** automatic polling or timed refresh.

## 16. How to view logs

```bash
sudo journalctl -u partition-job-ui.service -f
```

Full exceptions are logged on the server. The browser shows only short, safe messages (no passwords, connection strings, or tracebacks).

## 17. How to restart or stop the service

```bash
sudo systemctl restart partition-job-ui.service
sudo systemctl stop partition-job-ui.service
```

## 18. Troubleshooting

| Symptom | What to check |
|---|---|
| Unable to connect | `DB_HOST`, `DB_PORT`, firewall, Postgres listening address, `.env` credentials |
| Function not found | Schema name, function installed, `search_path`, `GRANT EXECUTE` with exact signature |
| Permission denied | Role grants on schema/function/table; SECURITY INVOKER needs |
| Duplicate / unique violation | Existing row with the same unique key |
| pgAgent not installed message | Objects in another database — set `PGAGENT_DB_*`; or install/enable pgAgent |
| Empty job list | No rows in `pgagent.pga_job`; use Refresh Jobs after the processor creates jobs |
| Service fails to start | `journalctl -u partition-job-ui.service`, venv path, `.env` permissions (`600`), user `partitionui` |

## 19. Deploying to additional database servers

Repeat installation on each server that should host the UI:

1. Copy the project to `/opt/partition-job-ui`
2. Create the `partitionui` system user and virtualenv
3. Create a server-specific `.env` pointing at that server’s databases (`127.0.0.1` is typical)
4. Install and enable the systemd unit
5. Restrict port `8501` to the administrator network

Do not share one `.env` across environments — each server has its own secrets.

## 20. Security notes

* No hardcoded credentials in source code.
* `.env` is git-ignored and should be mode `600`, owned by `partitionui`.
* All SQL uses Psycopg parameter binding; user input is never concatenated into SQL.
* Schema/table names are validated as unquoted PostgreSQL identifiers before use as bound parameters.
* Next.js proxies `/api/*` to localhost FastAPI; API is not exposed publicly by default.
* Error details from the API are returned as structured JSON messages without SQL secrets.
* Systemd hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectHome`, and related options.
* The UI role must not be a superuser and must not receive write privileges on pgAgent tables.
* Restrict network access to trusted administrators only.

---

## Local development (Windows / workstation)

Laptop workflow: edit → run → test locally. Do **not** point the local
scheduler at production / production-copy databases (`172.20.224.68`,
`ukbodev`, or any other real GTN DB) unless explicitly approved.

Ports (same as PartOps architecture; never use 8000 for PartOps):

| Service | URL |
|---|---|
| UI (Next.js) | `http://localhost:8501` |
| API (FastAPI) | `http://127.0.0.1:8001` |
| Scheduler control | `http://127.0.0.1:8765` (only with a safe local DB) |

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# Optional: copy .env.example -> .env with SAFE local/test DB settings only
# Optional: copy .env.realtime.example -> .env.realtime for local scheduler
# Ensure Node.js is on PATH (e.g. C:\Program Files\nodejs)

# Terminal 1 — API
.\.venv\Scripts\python.exe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8001

# Terminal 2 — UI (API origin defaults to http://127.0.0.1:8001 via next.config)
cd frontend
npm ci
npm run dev -- -H 127.0.0.1 -p 8501

# Terminal 3 — Scheduler (ONLY after safe local DB is confirmed)
# .\.venv\Scripts\python.exe -m scheduler_backend.main --log-level INFO
```

Convenience launcher (API + UI; scheduler opt-in after safety checks):

```powershell
.\scripts\dev-local.ps1
# .\scripts\dev-local.ps1 -IncludeScheduler   # localhost DB only
```

Verify: `curl http://127.0.0.1:8001/api/health` and open `http://localhost:8501`.
Unit tests: `.\.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py" -v`

See also `WEB.md` / `RUNBOOK.md`.

---

## 21. Realtime scheduler architecture (first major redesign)

### Old model (polling scanner)

```text
external cron ~every few minutes
        ↓
run_partition_create_jobs() / run_partition_drop_jobs()
        ↓
SELECT jobs where next_run_time <= now()
        ↓
execute already-due/overdue jobs
```

Jobs could run late because execution waited for the next scan.

### New model (schedule-driven near-real-time)

```text
Streamlit UI (config only)          Scheduler backend (persistent process)
        |                                      |
        | short DB open/work/close             | short DB open → upcoming jobs → close
        |                                      | in-memory heapq timers (no DB held)
        | POST /internal/scheduler/refresh  →  | wake + reconcile
        |                                      | at due time: NEW short DB connection
        └──────────────┬───────────────────────┘
                       ▼
                  PostgreSQL (source of truth)
                       ▼
         get_upcoming_partition_jobs(lookahead)
         run_partition_job_scheduled(job_id, expected_run_time)
                       ▼
         create_any_table_partition / drop_any_table_partition
                       ▼
         status / history / next_run_time (DB cron helpers)
```

Important properties:

* PostgreSQL is authoritative; memory is temporary/ephemeral.
* Occurrence identity for an attempt is `(job_id, expected_run_time)` where
  `expected_run_time` is the job row’s current `next_run_time` at discovery.
* After locking the row (`FOR UPDATE`), stale timers
  (`expected_run_time ≠ next_run_time`) and disabled jobs are rejected before
  CREATE/DROP workers run.
* After locking, next occurrence is validated **before** CREATE/DROP.
  A valid next `N` must be future and distinct from occurrence `T`.
* If the next occurrence cannot be validated, PartOps **fails closed**:
  no worker runs, `next_run_time` is cleared (`NULL`), and an operator must
  correct the schedule. PartOps does **not** invent arbitrary dates
  (no automatic `+1 day` / frequency fallback for invalid cron results).
* Overdue semantics (Phase 5A): an occurrence the scheduler first discovers
  **already overdue** (beyond a small jitter tolerance — e.g. after downtime)
  is **never executed automatically**. It is recorded in
  `partition_job_attention` for DBA review and the job advances to its first
  legitimate future occurrence. An occurrence the scheduler discovered on time
  ("owned") may still run late after waiting for a slot / busy table / retry.
  See "Phase 5A" below.
* Streamlit/Next.js UI is **not** the scheduler.
* No Redis, Celery, Kafka, RabbitMQ, LISTEN/NOTIFY, or connection pools.
* DB connections are open → one logical operation → close immediately.
* Scheduler transaction contract:
  1. connect
  2. `SELECT current_database()` + identity assert
  3. end identity-check transaction (`commit` of that read-only check)
  4. connection idle
  5. `BEGIN` via `conn.transaction()` for the scheduled occurrence
  6. `run_partition_job_scheduled` (workers / updates / log)
  7. COMMIT (handled) or ROLLBACK (not handled — e.g. `FAILED_DATABASE`)
* Idle timer waits hold **zero** database sessions.
* UI refresh HTTP is a wake-up signal only; the backend always rereads PostgreSQL.
* Periodic reconciliation (`PARTITION_SCHEDULER_RECONCILE_SECONDS`) catches direct SQL edits.
* Lookahead window: `PARTITION_SCHEDULER_LOOKAHEAD_SECONDS` (default 120).
* Overdue jobs remain discoverable after downtime (`delay_seconds <= 0`); the
  scheduler classifies them as historical and has the database reconcile them
  as missed — no historical catch-up.
* Same-time jobs are ordered by `next_run_time`, then `job_id`, then executed
  under a bounded concurrency limit (see Phase 2 below).
* OS singleton lock prevents two backends; DB uses `FOR UPDATE` + two
  transaction advisory locks (per-job and per-target-table).
* Secondary Python rapid-loop breaker is a short-lived process-local cache
  (TTL 300s, max 512) of recently **committed** occurrence transitions
  (`EXECUTED` / SQL `FAILED` / `FAILED_INVALID_SCHEDULE` /
  `MISSED_RESCHEDULED` / `MISSED_INVALID_SCHEDULE`).
  `FAILED_DATABASE`, `FAILED_CONNECTION`, ordinary skips (including
  `SKIPPED_TARGET_LOCKED`), and exceptions are not cached; instead they go
  through the bounded per-occurrence retry policy (Phase 5A) — never an
  immediate re-run. The database remains the scheduling source of truth.

### Phase 2 — controlled parallel scheduled execution

```text
Scheduler (asyncio event loop)
        ↓
asyncio.Semaphore (PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS, default 1)
        ├── Job A → asyncio.to_thread → short-lived DB connection A
        ├── Job B → asyncio.to_thread → short-lived DB connection B
        └── Job C → asyncio.to_thread → short-lived DB connection C
                       (Job D waits for a free slot)
```

* Default concurrency is **1** (serial behaviour unchanged when unset).
* Blocking psycopg work never runs on the event loop (`asyncio.to_thread`).
* Each active job opens its own short-lived connection; connections are never
  shared between parallel jobs. No connection pool in this phase.
* Different target tables may run concurrently (up to N).
* Same `(table_schema, table_name)` is serialized in PostgreSQL via
  `pg_try_advisory_xact_lock(87421032, …)`. If the target lock is busy, SQL
  returns `SKIPPED_TARGET_LOCKED` (non-blocking; occurrence stays eligible).
* Per-job lock namespace remains `87421031` (`SKIPPED_LOCKED`).
* Status fields: `max_concurrent_jobs`, `active_job_count`, `queued_job_count`,
  `available_slots`. Active jobs are healthy.
* Rollout recommendation: deploy with `1`, verify, then `2`, then `3` only if
  DB capacity allows. Do not default production to `3`.

### Phase 3 — Datadog observability

```text
PartOps (API + scheduler)
  ├─ logs  → stdout/stderr → systemd/journald → Datadog Agent
  └─ metrics → DogStatsD UDP 127.0.0.1:8125 → Datadog Agent → Datadog
```

* Implementation: stdlib UDP in `observability.py` (no Datadog Python SDK,
  no application Datadog API key, no direct Datadog HTTPS API calls).
* The **Datadog Agent** owns the API key.
* Default: `PARTOPS_DATADOG_ENABLED=false` (metrics are no-ops).
* Metrics failure never breaks API, scheduler, DB, or Secrets Manager paths.
* Low-cardinality tags only — never `job_id`, `job_name`, table names, secret
  IDs/ARNs, passwords, or raw exception text as metric tags.
* Job IDs remain acceptable in structured logs (`event=… job_id=…`).

| Variable | Default | Purpose |
|---|---|---|
| `PARTOPS_DATADOG_ENABLED` | `false` | Master switch |
| `PARTOPS_DOGSTATSD_HOST` | `127.0.0.1` | DogStatsD host |
| `PARTOPS_DOGSTATSD_PORT` | `8125` | DogStatsD UDP port |
| `PARTOPS_ENVIRONMENT` | `unknown` | `env` tag (set `production` on server) |
| `PARTOPS_METRICS_NAMESPACE` | `partops` | Metric prefix |

Key metrics: `partops.scheduler.heartbeat`, `queue_depth`, `active_jobs`,
`max_concurrent_jobs`, `available_slots`, `overdue_jobs`,
`partops.job.execution`, `partops.job.duration_ms`,
`partops.jobs.failed_count`,
`partops.db.connection_failure`, `partops.secrets_manager.*`,
`partops.api.request` / `partops.api.error`.

Metric semantics (do not confuse):

* `partops.job.execution` — **counter** of scheduled execution **events**
  (attempts/outcomes). Useful for troubleshooting; a single bad job can emit
  many failure events.
* `partops.jobs.failed_count` — **gauge** of the current number of **enabled**
  rows in `partitioning_job_table` with `last_run_status = 'FAIL'`.
  Disabled FAIL rows are excluded. Prefer this for “Failed Partition Jobs” KPIs
  (`max:partops.jobs.failed_count{…}`).

See RUNBOOK §10.4 for Agent setup, dashboard, and monitor recommendations.

### Phase 4 — Edit / Enable / Disable (secure update)

Configuration changes persist only through:

```text
UI → FastAPI PATCH /api/jobs/{id}
   → mubasher_oms.update_data_to_partition_job_table(...)
      (SECURITY DEFINER, owner mubasher_oms, fixed search_path)
   → partitioning_job_table
```

* `partition_job_ui` needs **EXECUTE** on the function — not broad table `UPDATE`.
* **Disable:** sets `is_enabled=false`; keeps `job_id`, history, logs,
  `last_run_time`, `last_run_status`, and `next_run_time`. Stops **future**
  scheduled runs (`SKIPPED_DISABLED`). Does not kill an already-running DDL
  transaction.
* **Re-enable / schedule edit:** database recalculates **one** future
  `next_run_time` via `cron_to_interval_or_next_run` when the stored next is
  stale/NULL or the schedule changes. Invalid schedules fail closed (rollback).
* After a successful save the API returns the refreshed job row; the Jobs page
  reloads so Enabled/Disabled and next run update without a fake success.

### Phase 5A — missed runs, bounded retry, manual target lock, db_config allowlist

**Missed-run policy (no historical catch-up) — ownership, not a fixed window.**

The scheduler classifies every occurrence `(job_id, expected_run_time)` once,
at its first sighting, using the database clock (`delay_seconds`):

* **Owned** — first seen before it was due (lookahead) or at most
  `PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS` (default 60 s, timer/refresh
  jitter only) after it. The scheduler is responsible for it: it may execute
  late after waiting for a concurrency slot, a busy target table or a retry
  backoff — it does **not** become "missed" because the wall clock passed some
  threshold.
* **Historical** — first seen later than that (scheduler start after downtime,
  refreshes failing during a DB outage). It is **never executed**.

```text
refresh → classify new occurrences (sticky)
historical → reconcile_missed_partition_job(..., 'MISSED_SCHEDULED_RUN'):
               job lock → FOR UPDATE → enabled / still-current / due checks
               INSERT partition_job_attention (PENDING_REVIEW, missed_run_time = T)
               next_run_time := first future occurrence (cron helper; NULL = fail closed)
               NO CREATE/DROP; last_run_time / last_run_status untouched
               → MISSED_RESCHEDULED / MISSED_INVALID_SCHEDULE
owned      → run_partition_job_scheduled: full revalidation, target lock,
               future-next validation, worker, log.
               DB safety ceiling: if now > T + partition_job_max_execution_lateness()
               (default 6 h, DBA-tunable) it reconciles as missed instead —
               defence against stale/buggy callers and unbounded waits.
```

Ownership is process memory: after a scheduler restart, occurrences that are
already overdue are historical by design. Startup requires
`LOOKAHEAD_SECONDS >= RECONCILE_SECONDS` so normal occurrences are always seen
before they are due.

A missed run is not an execution failure: `last_run_status` keeps the result
of the last real execution. The UI shows it as amber **Needs review**.

**DBA review** (Jobs page banner / job detail panel):

* All settings are `PARTITION_SCHEDULER_*` in `.env.realtime` (see
  `.env.realtime.example`); invalid values stop the scheduler at startup.
* **Run missed occurrence** → `POST /api/attention/{id}/recover`
  → `run_partition_job_attention_recovery(id)` → `run_partition_job_manual`
  (same privileges, target lock and MANUAL_* logging as "Run now").
  `next_run_time` is **never** changed: missed 20 Oct 02:00, next normal run
  27 Oct 02:00, manual recovery on 26 Oct 14:00 → next run stays 27 Oct 02:00.
  Success → `MANUALLY_RERUN`; failure/busy target → stays `PENDING_REVIEW`
  with the attempt recorded. DROP recovery requires `confirm_drop` **and**
  typing the exact `schema.table`.
* **Acknowledge without running** → `POST /api/attention/{id}/acknowledge`
  (`ACKNOWLEDGED`, optional note, no DDL).
* `GET /api/attention?status=pending|acknowledged|manually_rerun|all`.

**Bounded retry/backoff** (per `(job_id, expected_run_time)`, scheduler memory):

| Outcome | Behaviour |
|---|---|
| EXECUTED / FAILED / FAILED_INVALID_SCHEDULE / MISSED_* | committed; done (retry state cleared) |
| FAILED_DATABASE / FAILED_CONNECTION | `RETRY_MAX_ATTEMPTS` (default 4) execution attempts, `RETRY_DELAYS_SECONDS` between them (default 15, 60, 180 s ≈ 4 min — rides out a DB restart/failover), then **exhausted** → DB `reconcile_missed_partition_job(..., 'RETRY_EXHAUSTED')` (review item; next_run_time advanced); never executed again |
| SKIPPED_TARGET_LOCKED / SKIPPED_LOCKED | table busy, not a failure: cooldown `TARGET_LOCK_COOLDOWN_SECONDS` (default 5/10/20/30 s, last repeats) for up to `TARGET_LOCK_MAX_WAIT_SECONDS` (default 1 h), then RETRY_EXHAUSTED review item. The occurrence stays owned meanwhile. |
| any other non-committed status | ≥ 1 s cooldown |

No zero-delay path remains: the queue rebuild honours each occurrence's
`not_before`, and signal-driven refreshes are coalesced to at most one per
second. Waits are `asyncio` timeouts (no `time.sleep`), so shutdown and HTTP
refresh stay responsive during backoff.

**Manual run lock.** `run_partition_job_manual` takes the same target-table
advisory lock (`87421032`, identical key) as scheduled execution. Busy →
`MANUAL_TARGET_LOCKED`, nothing executed, API HTTP 409.

**db_config_para allowlist** (Python `validators.py` + SQL
`partition_job_config_param_allowed`, enforced on create/edit and before every
`set_config`): `lock_timeout`, `statement_timeout`,
`idle_in_transaction_session_timeout`, `work_mem`, `maintenance_work_mem`,
`effective_io_concurrency`, `random_page_cost`,
`max_parallel_maintenance_workers`, and `datestyle` with ISO output only
(`ISO`, `ISO, DMY|MDY|YMD`). Anything else (e.g. `role`,
`session_authorization`, `search_path`) is rejected.

**Failed-jobs gauge.** `last_run_status` keeps its established meaning (latest
*scheduled* execution; manual runs never change it). `partops.jobs.failed_count`
counts enabled jobs with `last_run_status = 'FAIL'` **unless a later manual run
(e.g. a DBA fix via Run now) succeeded** — so a successful recovery clears the
gauge without rewriting execution history. A later `MANUAL_FAIL` counts again.

**Metrics** (low-cardinality): `partops.jobs.pending_review_count` (gauge),
`partops.missed_run.detected` {reason}, `partops.missed_run.reconciled`
{reason, outcome}, `partops.scheduler.retry_scheduled` {status},
`partops.scheduler.retry_exhausted` {status}.

### Phase 5B — database framework (version-controlled model)

* Every database object PartOps needs is listed in
  `sql/framework/framework_manifest.json` (rendered as
  `sql/framework/INVENTORY.md`), split into **PartOps-owned** objects (in Git)
  and **existing/legacy framework** objects.
* **Not yet in Git (capture required, never invented):**
  `cron_to_interval_or_next_run`, `create_any_table_partition`,
  `drop_any_table_partition`, `insert_data_to_partition_job_table`
  (+ `cron_field_to_int_array`, unverified). Capture with the READ-ONLY
  `sql/capture_reference_framework.sql` — workflow in `sql/framework/README.md`
  and RUNBOOK §16.
* Framework version: `mubasher_oms.partops_framework_version` (migration
  `20261110_03`); the application expects the manifest's `framework_version`.
* READ-ONLY preflight: `python scripts/preflight_framework.py --expect-database <db>`
  (PASS / WARNING / FAIL). The scheduler runs the same check for its scope at
  startup and dispatches nothing while it fails.
* Disposable end-to-end test:
  `scripts/disposable_db/run_disposable_db_test.sh --i-understand-this-is-disposable`
  (PostgreSQL 16 tested; **EDB not yet tested**).

### Phase 5C — hub-local database targets (foundation)

Architecture: ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.

* Target registry `mubasher_oms.partops_database_target` in the existing
  PartOps (control) database; the control database itself is target `db01`.
  No passwords or secret values are stored — only a Secrets Manager secret
  identifier for `aws` targets.
* Every job has `database_target_id` (migration `20261117_01` backfills
  existing jobs to `db01`). `db_config_para` stays session configuration only.
* `database_targets.py`: validated `DatabaseTarget` model, per-target
  credentials (`inherit` / `aws` / `env`), per-secret credential cache,
  short-lived target connections verified with `current_database()`, and a
  read-only per-target health check that reuses the Phase 5B manifest
  (`target_execution` scope for managed targets).
* Read-only API: `GET /api/targets`, `/api/targets/{id}`,
  `/api/targets/{id}/health`. Job create/edit accept `database_target_id`;
  changing it is a dangerous change and is refused (409) until Phase 5D.
* Scheduler: blocked unless exactly one enabled control target matches the
  connected database; occurrences carry the target; metrics get a
  low-cardinality `target` tag (registry key, e.g. `db01`).
* **Not in 5C:** executing jobs on additional targets (Phase 5D), multi-target
  UI (Phase 5E). Phase 5B.1 (capturing the legacy definitions) stays deferred:
  production is the only source and production capture is not permitted.

See RUNBOOK §10.7 (deploy/rollback) and §17 (targets).

### Phase 5D — target-aware scheduler and safe remote execution

Architecture: ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.
Design: `REMOTE_EXECUTION.md`.

* The scheduler discovers due jobs of **all enabled targets** from the control
  database (`partops_get_upcoming_jobs`) and runs db01 jobs with the existing
  executor and db02+ jobs with a remote orchestrator: control claim → target
  transaction (advisory lock, allowlisted settings, legacy worker, COMMIT) →
  control finalization, recorded in `partops_execution_attempt`.
* No distributed transaction is assumed. Unprovable target outcomes become
  `OUTCOME_UNKNOWN` and are never retried automatically (review item, job
  paused). Known failures use the Phase 5A bounded retry; a confirmed commit
  is only finalized, never re-executed. Restart recovery classifies leftovers.
* Global + per-target concurrency (target slot first, then global slot),
  per-target availability cooldown, cached per-target preflight.
* Manual runs / missed-run recovery of db02+ jobs go through the scheduler's
  engine. API: job create/edit accept healthy managed targets; target moves
  are dangerous and refused while an attempt is open;
  `GET /api/execution-attempts`, `POST /api/execution-attempts/{id}/resolve`;
  history rows carry `database_target_id` / `target_key` / `execution_attempt_id`.
* Migrations `20261117_03` (ledger, guards) and `20261117_04` (control
  functions); framework version `20261117_04`. RUNBOOK §10.8.
* Not in 5D: multi-target UI (Phase 5E). Phase 5B.1 stays deferred.

### Phase 5E — hub-local multi-target operations UI

* New read-only **Database targets** page (`/targets`): target, database,
  environment, role, health (Healthy / with warnings / Disabled / Unreachable /
  Authentication failed / Wrong database / Framework incompatible /
  Configuration error), framework problems vs warnings, concurrency, waiting,
  backoff. No target administration in the UI (DBA-only, RUNBOOK §17).
* Jobs: Database column + target filter; job drawer shows the target, its
  health and contextual isolation warnings; "Last scheduled status" and the
  latest manual result are shown separately.
* Create / edit: database target selector (only usable targets selectable);
  a target change has its own explicit confirmation block.
* Remote manual runs go through the scheduler; 503 / 504 / unknown outcomes
  have specific operator messages.
* History: Target column + filter, attempt links, remote execution attempts.
* OUTCOME_UNKNOWN: red banners everywhere it matters; resolution only through
  a deliberate APPLIED / NOT_APPLIED workflow with note (+ typed table for
  DROP). No retry action exists.
* System: hub status OPERATIONAL / DEGRADED / DOWN (a managed target failing
  is DEGRADED; only the control database connection is DOWN) and per-target
  scheduler concurrency. Overview: Jobs, Targets healthy, Needs review,
  Running, Waiting, Next execution.
* RUNBOOK §18 (UI guide, message guide, manual verification matrix).

### Phase 5F.1 — performance / resource optimization

* Readiness: concurrent check groups, one shared control-DB connection,
  bounded probes (`PARTITION_READINESS_CONNECT_TIMEOUT_SECONDS`, default 3),
  30 s cache (`?refresh=true` bypasses). Control DB down: ~3 s (was ~27 s).
* `GET /api/logs?limit=&offset=&target_id=&status=&job=&operation=&since=&until=`
  (limit ≤ 500) returns `logs`, `has_more`, `next_offset`, `previous_offset`.
* `GET /api/execution-attempts?open=&state=&job_id=&target_id=&limit=&offset=`
  also returns `unknown_count` (all unresolved unknown outcomes).
* `GET /api/jobs/{id}/runtime-context` (latest manual result of that job, open
  attempt state, pending review items) and `GET /api/jobs/{id}/attention`.
* UI: History pages on the server; nothing polls while the tab is hidden.
  No migration (ISSUE-015 documents an optional history index). CHANGE-020.

### New database configuration (production-copy / sensitive)

The NEW database is a **backup/copy of production**. It already contains application
data, but it does **not** currently contain this partition-job framework.

Do **not** overwrite production `.env`. Use:

* `.env.realtime.example` → copy to `.env.realtime` (git-ignored)
* Set `DB_*` to the NEW database
* Set `REALTIME_DB_EXPECTED_NAME` to the same database name (fail-closed check)
* Never auto-onboard existing business tables into `partitioning_job_table`

### SQL layers (NEW DB only — apply only after explicit approval)

| Order | File | Purpose |
|---|---|---|
| 0 | `sql/preflight_realtime_database.sql` | READ-ONLY preflight |
| 1 | `sql/bootstrap_partition_job_framework.sql` | Tables/sequences/type/trigger only |
| 1b | `sql/capture_reference_framework_functions.sql` | READ-ONLY capture from REFERENCE DB |
| 1c | *(imported reviewed defs)* | Workers/cron/insert/runners — **not invented** |
| 2 | `sql/realtime_scheduler_v1.sql` | Upcoming + scheduled executor |
| later | `sql/integration_test_disposable.sql` | Disposable test schema only |

Migration session gate (after approval):

```sql
CREATE TEMP TABLE _partition_realtime_migration_approved AS
SELECT now() AS approved_at, current_database() AS approved_database;
```

Optional tooling gate (does not run SQL by itself):

```bash
# Requires PARTITION_REALTIME_ALLOW_MIGRATION=true and REALTIME_DB_EXPECTED_NAME
python scripts/check_migration_gate.py
```

### Exact future deployment order (manual)

1. Configure `.env.realtime` for the NEW DB (`REALTIME_DB_EXPECTED_NAME` + `DB_*`).
2. Confirm expected name equals `current_database()`.
3. Run READ-ONLY `sql/preflight_realtime_database.sql`.
4. Review preflight; stop on unexpected conflicts.
5. Optional DBA restore point for the NEW DB.
6. Create approval TEMP TABLE; apply `bootstrap_partition_job_framework.sql`.
7. Verify baseline tables exist and are **empty**.
8. Capture missing functions from the REFERENCE DB; review; import to NEW DB.
9. Apply `realtime_scheduler_v1.sql`.
10. Verify realtime functions; apply least-privilege grants.
11. Confirm no legacy cron/pgAgent/service calls `run_partition_create_jobs` /
    `run_partition_drop_jobs` against the NEW DB (read-only inspection).
12. Only after a **second** explicit approval: create disposable
    `partition_scheduler_test` objects and test CREATE/DROP there only.
13. Start scheduler backend manually against NEW DB when ready.
14. Clean up only disposable test objects after approval.
15. Install/enable systemd manually; onboard real tables **one by one**.

### Disable / absence of old scanners on the NEW database

Because the NEW DB currently lacks the framework, legacy scanners should not
already target it — **verify**, do not assume. Before enabling the realtime
backend, confirm no competing polling of the legacy runners against the NEW DB.

### Run the scheduler backend locally

```bash
# From partition-job-ui/ with .env.realtime configured
# Do NOT start against NEW DB until migrations are approved and applied.
python -m scheduler_backend --log-level INFO
```

Control API (localhost by default):

* `GET /health`
* `GET /internal/scheduler/status`
* `POST /internal/scheduler/refresh`

### systemd (prepare only — do not auto-install from Cursor)

Unit file: `systemd/partition-job-scheduler.service`  
Install target on this host: `/usr/lib/systemd/system/`

### Troubleshooting (scheduler)

| Symptom | What to check |
|---|---|
| Jobs late after UI edit | Backend up? refresh URL? reconcile interval |
| Duplicate CREATE/DROP | Legacy polling still enabled on NEW DB |
| Second backend exits | Lock file already held |
| Refresh warning in UI | Backend down — config still saved; reconcile will catch up |
| `REALTIME_DB_EXPECTED_NAME` error | `.env.realtime` name ≠ `current_database()` |
| SKIPPED_RESCHEDULED | Expected: memory timer stale after DB reschedule |