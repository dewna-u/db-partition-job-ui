
cd /opt/db-partition-job-ui-github

GIT_SSH_COMMAND='ssh -i /root/.ssh/partition_job_ui_github -o IdentitiesOnly=yes' \
git -c safe.directory=/opt/db-partition-job-ui-github pull --ff-only origin main

chown -R partitionui:partitionui /opt/db-partition-job-ui-github

systemctl restart partition-job-ui.service

systemctl status partition-job-ui.service --no-pager -l





# Partition Job Configuration UI

Lightweight internal Streamlit application for configuring PostgreSQL/EDB partition jobs through an existing database function and listing pgAgent jobs with their job IDs.

## 1. Purpose

Administrators use this UI to:

* View existing pgAgent jobs (including **Job ID**)
* Submit a new partition-job configuration by calling  
  `mubasher_oms.insert_data_to_partition_job_table(...)`, which stores the row in
  `mubasher_oms.partitioning_job_table`

The UI never inserts into the partition job table directly — the function is the
only write path.

The browser never connects directly to PostgreSQL. All database access happens from the Streamlit process running on the database server.

## 2. Lightweight architecture

```text
Administrator’s Browser
        |
        v
Streamlit UI on Database Server
        |
        +--> Local PostgreSQL/EDB database
        |      |
        |      +--> insert_data_to_partition_job_table(...)
        |
        +--> pgAgent database
               |
               +--> Read-only query of pgagent.pga_job
```

Design goals:

* Very low idle CPU and memory (no GPU, no ML, no charts, no polling)
* Short-lived database connections only
* File watching disabled
* No background threads or automatic page refresh
* Partition function and pgAgent may live in the same database or different databases

## 3. Project structure

```text
partition-job-ui/
├── app.py
├── database.py
├── validators.py
├── job_autofill.py
├── scheduler_client.py
├── scheduler_backend/
│   ├── main.py
│   ├── scheduler.py
│   ├── queue_manager.py
│   ├── scheduler_database.py
│   └── models.py
├── sql/
│   ├── preflight_realtime_database.sql
│   ├── bootstrap_partition_job_framework.sql
│   ├── capture_reference_framework_functions.sql
│   ├── realtime_scheduler_v1.sql
│   └── integration_test_disposable.sql
├── systemd/
│   └── partition-job-scheduler.service
├── scripts/
│   └── check_migration_gate.py
├── requirements.txt
├── .env.example
├── .env.realtime.example
├── .gitignore
├── README.md
├── partition-job-ui.service
└── .streamlit/
    └── config.toml
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
| `DB_PASSWORD` | `replace_with_strong_password` |
| `DB_HOST` / `DB_PORT` | if not local defaults |
| `PGAGENT_DB_*` | only if pgAgent is in another database |

Never commit `.env`. It is listed in `.gitignore`.

## 10. Manual testing

Local-only smoke test (bind to localhost):

```bash
sudo -u partitionui \
  /opt/partition-job-ui/.venv/bin/streamlit run \
  /opt/partition-job-ui/app.py \
  --server.address=127.0.0.1 \
  --server.port=8501 \
  --server.headless=true
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
sudo cp \
  /opt/partition-job-ui/partition-job-ui.service \
  /etc/systemd/system/partition-job-ui.service

sudo systemctl daemon-reload
sudo systemctl enable --now partition-job-ui.service
sudo systemctl status partition-job-ui.service
```

The unit runs as `partitionui` / `partitionui`, not as root, and restarts only on failure.

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
* CORS and XSRF protection remain enabled in `.streamlit/config.toml`.
* Error details are hidden from the browser (`showErrorDetails = false`).
* Systemd hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectHome`, and related options.
* The UI role must not be a superuser and must not receive write privileges on pgAgent tables.
* Restrict network access to trusted administrators only.

---

## Local development (Windows / workstation)

From the project directory:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# Edit .env with real connection settings
.venv\Scripts\streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true
```

On Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env
.venv/bin/streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true
```

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

* PostgreSQL is authoritative; memory is temporary.
* Streamlit is **not** the scheduler.
* No Redis, Celery, Kafka, RabbitMQ, LISTEN/NOTIFY, or connection pools.
* DB connections are open → one logical operation → close immediately.
* Idle timer waits hold **zero** database sessions.
* UI refresh HTTP is a wake-up signal only; the backend always rereads PostgreSQL.
* Periodic reconciliation (`PARTITION_SCHEDULER_RECONCILE_SECONDS`) catches direct SQL edits.
* Lookahead window: `PARTITION_SCHEDULER_LOOKAHEAD_SECONDS` (default 120).
* Overdue jobs remain discoverable after downtime (`delay_seconds <= 0` → run immediately after revalidation).
* Same-time jobs execute sequentially ordered by `next_run_time`, then `job_id`.
* OS singleton lock prevents two backends; DB uses `FOR UPDATE` + `pg_try_advisory_xact_lock`.

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

### Troubleshooting (scheduler)

| Symptom | What to check |
|---|---|
| Jobs late after UI edit | Backend up? refresh URL? reconcile interval |
| Duplicate CREATE/DROP | Legacy polling still enabled on NEW DB |
| Second backend exits | Lock file already held |
| Refresh warning in UI | Backend down — config still saved; reconcile will catch up |
| `REALTIME_DB_EXPECTED_NAME` error | `.env.realtime` name ≠ `current_database()` |
| SKIPPED_RESCHEDULED | Expected: memory timer stale after DB reschedule |