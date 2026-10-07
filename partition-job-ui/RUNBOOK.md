# PartOps — Installation Runbook

Audience: teammates installing or redeploying when the owner is unavailable.

Product: **PartOps** — GTN EDB Partition Operations Platform.

This is the **live** stack:

| Layer | Technology | Service | Port |
|---|---|---|---|
| UI | Next.js | `partition-job-ui.service` | `8501` (public to admins) |
| API | FastAPI | `partition-job-api.service` | `127.0.0.1:8001` (localhost only) |
| Scheduler | Python backend | `partition-job-scheduler.service` | `127.0.0.1:8765` (control API) |
| Data | PostgreSQL / EDB | — | as configured in `.env` / `.env.realtime` |

> **Port note:** Another unrelated application on this host uses port **8000**.  
> PartOps FastAPI **must** use **8001**. Do not point PartOps at 8000.

Streamlit is **archived** (not used). Do not start Streamlit.

The UI/API can operate while the scheduler is offline, but the scheduler is
**REQUIRED** for automatic realtime partition execution.

---

## 0. Deploy path on this host

```text
/opt/db-partition-job-ui-github/partition-job-ui
```

All commands below assume that path unless noted.

> If your server uses a different path, update every path in the two systemd unit files
> (`partition-job-api.service`, `partition-job-ui.service`) before enabling them.
> Do **not** mix `/opt/partition-job-ui` and `/opt/db-partition-job-ui-github/...`.

---

## 1. Prerequisites

On the server you need:

- Root (or sudo) access
- Linux user/group: `partitionui` / `partitionui`
- Python **3.9+** (this host uses 3.9)
- Node.js + npm (for building/running Next.js)
- Git access to the repo
- Network access restricted so only admins can reach port `8501`
- Database role for the UI (non-superuser) with required privileges
- (Optional but normal) scheduler role + `.env.realtime` for the realtime scheduler

Check tools:

```bash
python3 --version
node --version
npm --version
id partitionui
```

---

## 2. Get the code

```bash
cd /opt/db-partition-job-ui-github
# If already cloned:
GIT_SSH_COMMAND='ssh -i /root/.ssh/partition_job_ui_github -o IdentitiesOnly=yes' \
  git -c safe.directory=/opt/db-partition-job-ui-github pull --ff-only origin main

# Prefer owning application code as partitionui, then restore env file owners.
chown -R partitionui:partitionui /opt/db-partition-job-ui-github/partition-job-ui
cd /opt/db-partition-job-ui-github/partition-job-ui

# CRITICAL: recursive chown must not leave .env.realtime owned by partitionui.
if [[ -f .env ]]; then
  chown partitionui:partitionui .env
  chmod 600 .env
fi
if [[ -f .env.realtime ]]; then
  chown enterprisedb:enterprisedb .env.realtime
  chmod 600 .env.realtime
fi
```

Confirm layout:

```bash
pwd
# expect: /opt/db-partition-job-ui-github/partition-job-ui

ls -la \
  api/main.py \
  frontend/package.json \
  database.py \
  .venv/bin/python
```

---

## 3. Configure environment files

### 3.1 UI / API — `.env`

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui
cp -n .env.example .env
chmod 600 .env
chown partitionui:partitionui .env
vi .env
```

Minimum required values:

```bash
DB_HOST=127.0.0.1
DB_PORT=5444
DB_NAME=<your_database>
DB_USER=partition_job_ui
DB_PASSWORD=<secret>
DB_SSLMODE=prefer
DB_CONNECT_TIMEOUT=5
```

Optional pgAgent DB (leave blank to reuse main DB):

```bash
PGAGENT_DB_HOST=
PGAGENT_DB_PORT=
PGAGENT_DB_NAME=
PGAGENT_DB_USER=
PGAGENT_DB_PASSWORD=
PGAGENT_DB_SSLMODE=
```

Recommended scheduler signal URLs (defaults are fine if scheduler binds localhost:8765):

```bash
PARTITION_SCHEDULER_REFRESH_URL=http://127.0.0.1:8765/internal/scheduler/refresh
PARTITION_SCHEDULER_STATUS_URL=http://127.0.0.1:8765/internal/scheduler/status
PARTITION_SCHEDULER_REFRESH_TIMEOUT_SECONDS=2
```

Never commit `.env`.

### 3.2 Scheduler — `.env.realtime` (if running realtime scheduler)

```bash
cp -n .env.realtime.example .env.realtime
chmod 600 .env.realtime
chown enterprisedb:enterprisedb .env.realtime
vi .env.realtime
```

`REALTIME_DB_EXPECTED_NAME` **must** equal `current_database()` on that DB.

Required concepts in `.env.realtime`:

```bash
REALTIME_DB_EXPECTED_NAME=<database>
PARTITION_SCHEDULER_BIND_HOST=127.0.0.1
PARTITION_SCHEDULER_BIND_PORT=8765
```

The scheduler process loads **only** `.env.realtime` (never UI `.env`).

Env ownership must remain:

```text
.env            partitionui:partitionui      mode 600
.env.realtime   enterprisedb:enterprisedb   mode 600
```

Do **not** `chmod 644/666/777` env files to “fix” Permission denied.

Never commit `.env.realtime`.

---

## 4. Python virtualenv + API deps

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui

# Create venv once if missing
if [[ ! -x .venv/bin/python ]]; then
  sudo -u partitionui python3 -m venv .venv
fi

sudo -u partitionui .venv/bin/pip install --upgrade pip
sudo -u partitionui .venv/bin/pip install -r requirements.txt

# Smoke import (must succeed on Python 3.9)
sudo -u partitionui .venv/bin/python -c "import fastapi, uvicorn, api.main; print('API imports OK')"
```

---

## 5. Build the Next.js frontend

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui/frontend

# Prefer ci when lockfile exists — run as partitionui to avoid root-owned .next
if [[ -f package-lock.json ]]; then
  sudo -u partitionui npm ci
else
  sudo -u partitionui npm install
fi

sudo -u partitionui npm run build
test -d .next
test -x node_modules/.bin/next

cd ..
chown -R partitionui:partitionui frontend
# Restore env owners if a prior recursive chown touched them
[[ -f .env ]] && chown partitionui:partitionui .env && chmod 600 .env
[[ -f .env.realtime ]] && chown enterprisedb:enterprisedb .env.realtime && chmod 600 .env.realtime
```

> If `.next` / `node_modules` stay owned by `root`, the UI service (`User=partitionui`) will fail later.

---

## 6. Install systemd units

Units belong in:

```text
/usr/lib/systemd/system/
```

### 6.1 Copy / refresh units from the repo

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui

cp systemd/partition-job-api.service /usr/lib/systemd/system/partition-job-api.service
cp partition-job-ui.service /usr/lib/systemd/system/partition-job-ui.service

# Optional: scheduler (separate)
# cp systemd/partition-job-scheduler.service /usr/lib/systemd/system/partition-job-scheduler.service
# IMPORTANT: edit scheduler unit WorkingDirectory / EnvironmentFile / ExecStart
# to match THIS host's real paths and OS user before enabling.
```

### 6.2 Remove stale `/etc` overrides (important)

Older installs may have wrong units under `/etc/systemd/system/` that override `/usr/lib`.

```bash
rm -f /etc/systemd/system/partition-job-api.service
rm -f /etc/systemd/system/partition-job-ui.service
```

### 6.3 Verify unit paths before starting

```bash
grep -E 'WorkingDirectory|ExecStart|EnvironmentFile' \
  /usr/lib/systemd/system/partition-job-api.service \
  /usr/lib/systemd/system/partition-job-ui.service
```

Both must reference:

```text
/opt/db-partition-job-ui-github/partition-job-ui
```

API ExecStart must look like:

```text
.../.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8001
```

UI ExecStart must look like:

```text
.../frontend/node_modules/.bin/next start --hostname 0.0.0.0 --port 8501
```

UI unit must also set:

```text
Environment=PARTITION_API_ORIGIN=http://127.0.0.1:8001
```

### 6.4 Enable and start

```bash
systemctl daemon-reload
systemctl enable partition-job-api.service partition-job-ui.service
systemctl restart partition-job-api.service
sleep 2
systemctl restart partition-job-ui.service
systemctl status partition-job-api.service partition-job-ui.service --no-pager -l
```

Optional helper (if present and up to date):

```bash
bash scripts/fix-systemd-web-ui.sh
```

---

## 7. Verification checklist

```bash
# Services
systemctl is-active partition-job-api.service
systemctl is-active partition-job-ui.service

# API health (wait a second after restart)
sleep 2
curl -sS http://127.0.0.1:8001/api/health; echo
# expect: {"ok":true,"service":"partops-api"}

# UI responds
curl -sS -o /dev/null -w "ui_http=%{http_code}\n" http://127.0.0.1:8501/
# expect: ui_http=200

# Optional: OpenAPI reachable
curl -sS -o /dev/null -w "openapi=%{http_code}\n" http://127.0.0.1:8001/openapi.json
```

Browser smoke test (from an allowed admin network):

1. Open `http://<server-ip>:8501`
2. Sidebar shows Overview / Convert / Create / Jobs / History / System readiness
3. Overview loads metrics (or a clean empty/DB-permission message)
4. Configured Jobs lists rows (if any exist)
5. Scheduler chip shows Online/Idle/Offline based on scheduler process

---

## 8. Day-2 operations

### Redeploy after a code pull

```bash
cd /opt/db-partition-job-ui-github
GIT_SSH_COMMAND='ssh -i /root/.ssh/partition_job_ui_github -o IdentitiesOnly=yes' \
  git -c safe.directory=/opt/db-partition-job-ui-github pull --ff-only origin main

cd partition-job-ui
# Own application tree as partitionui, then restore env file owners immediately.
chown -R partitionui:partitionui .
[[ -f .env ]] && chown partitionui:partitionui .env && chmod 600 .env
[[ -f .env.realtime ]] && chown enterprisedb:enterprisedb .env.realtime && chmod 600 .env.realtime

sudo -u partitionui .venv/bin/pip install -r requirements.txt
cd frontend && sudo -u partitionui npm ci && sudo -u partitionui npm run build && cd ..
chown -R partitionui:partitionui frontend

cp systemd/partition-job-api.service /usr/lib/systemd/system/
cp partition-job-ui.service /usr/lib/systemd/system/
cp systemd/partition-job-scheduler.service /usr/lib/systemd/system/
systemctl daemon-reload
systemctl restart partition-job-api.service partition-job-ui.service
systemctl status partition-job-api.service partition-job-ui.service --no-pager -l
```

### Useful logs

```bash
journalctl -u partition-job-api.service -n 80 --no-pager
journalctl -u partition-job-ui.service -n 80 --no-pager
journalctl -u partition-job-scheduler.service -n 80 --no-pager
```

### Restart only one layer

```bash
systemctl restart partition-job-api.service
systemctl restart partition-job-ui.service
systemctl restart partition-job-scheduler.service   # if used
```

---

## 9. Scheduler backend (required for automatic realtime execution)

The UI/API can operate while the scheduler is offline, but the scheduler is
**REQUIRED** for automatic realtime partition execution.

### 9.1 Install / update the unit (do not start yet)

```bash
cp /opt/db-partition-job-ui-github/partition-job-ui/systemd/partition-job-scheduler.service \
  /usr/lib/systemd/system/
systemctl daemon-reload
```

Confirm unit paths:

```bash
grep -E 'User=|WorkingDirectory|EnvironmentFile|ExecStart' \
  /usr/lib/systemd/system/partition-job-scheduler.service
```

Expect:

```text
User=enterprisedb
WorkingDirectory=/opt/db-partition-job-ui-github/partition-job-ui
EnvironmentFile=/opt/db-partition-job-ui-github/partition-job-ui/.env.realtime
ExecStart=.../.venv/bin/python -m scheduler_backend.main --log-level INFO
```

### 9.2 Verify `.env.realtime`

```bash
ls -la /opt/db-partition-job-ui-github/partition-job-ui/.env.realtime
# expect: enterprisedb:enterprisedb  mode 600
```

### 9.3 Verify required realtime DB functions (DBA / psql)

Both must return a non-NULL procedure identity:

```sql
SELECT to_regprocedure(
  'mubasher_oms.get_upcoming_partition_jobs(interval)'
);

SELECT to_regprocedure(
  'mubasher_oms.run_partition_job_scheduled(numeric,timestamp without time zone)'
);
```

Safe read-only smoke (does **not** execute partition jobs):

```sql
SELECT *
FROM mubasher_oms.get_upcoming_partition_jobs(interval '120 seconds');
```

Do **not** call `run_partition_job_scheduled(...)` merely as a health test.

### 9.4 Legacy runner safety

Old functions such as `run_partition_create_jobs()` / `run_partition_drop_jobs()`
may remain for rollback. Their **existence alone is fine**.

Do **not** run the realtime scheduler at the same time as active cron/pgAgent/systemd
jobs that still call those legacy broad runners — that can double-execute work.

Check PartOps **System readiness → Legacy automation safety**, or inspect enabled
pgAgent steps for those routine names. Do not auto-disable DB objects from the UI.

### 9.5 Start, verify, then enable

```bash
systemctl start partition-job-scheduler.service
systemctl status partition-job-scheduler.service --no-pager -l
journalctl -u partition-job-scheduler.service -n 80 --no-pager

curl -sS http://127.0.0.1:8765/health || true
curl -sS http://127.0.0.1:8765/internal/scheduler/status || true

# Only after healthy:
systemctl enable partition-job-scheduler.service
```

Note: `systemctl is-active` can be `active` while the process is still non-functional
if required DB functions are missing — always complete §9.3 first.

If the scheduler is down:

- Job **configuration** still works
- Overview / System readiness show scheduler offline
- Jobs will not fire until the scheduler is healthy again

---

## 10. Troubleshooting (known real failures)

| Symptom | Likely cause | Fix |
|---|---|---|
| API `203/EXEC` | Wrong path or missing uvicorn | Paths must be `/opt/db-partition-job-ui-github/partition-job-ui`; `pip install -r requirements.txt`; use `python -m uvicorn` |
| UI `200/CHDIR` | Wrong `WorkingDirectory` | Must be `.../partition-job-ui/frontend` |
| API crash: `ManualRunBody \| None` | Python 3.9 + modern union syntax | Use `Optional[ManualRunBody]` in `api/routers/jobs.py` (already fixed in current tree) |
| UI starts then permission errors | `frontend/.next` owned by root | Rebuild/run npm as `partitionui`; `chown -R partitionui:partitionui frontend` |
| API `active` but curl `/api/health` = Not Found immediately | Curled during startup | `sleep 2` then retry |
| Units ignore repo files | Stale unit in `/etc/systemd/system/` | Delete `/etc` copies; keep `/usr/lib/systemd/system/` |
| Overview shows DB errors | Bad `.env` or missing grants | Fix `.env`; ask DBA for least-privilege grants (UI never grants) |
| Scheduler `Permission denied: '.env'` | Scheduler tried to load UI `.env` | Scheduler must load **only** `.env.realtime`; restore owners (partitionui vs enterprisedb) |
| Scheduler always offline | Scheduler service down / wrong status URL | Start scheduler; check `PARTITION_SCHEDULER_STATUS_URL` |
| Scheduler repeated same occurrence | Occurrence `T` not advanced / stale SQL | See §10.1; keep scheduler STOPPED until SQL+Python Phase 0 fix deployed |
| API bind conflict on 8000 | Unrelated app owns 8000 | PartOps must use **8001** |

### 10.1 Scheduler repeated-execution emergency

If journal shows the same `job_id` + `expected_run_time` executing repeatedly:

1. **Immediately stop** (do not bounce-restart):
   ```bash
   sudo systemctl stop partition-job-scheduler.service
   ```
2. Confirm API/UI separately (`systemctl status partition-job-api`,
   `partition-job-ui`). Configuration UI may stay up; scheduled DDL must not.
3. Inspect scheduler journal for `job_id`, `expected_run_time`, result, and
   `returned_next_run_time` / circuit-breaker CRITICAL lines:
   ```bash
   sudo journalctl -u partition-job-scheduler.service -n 200 --no-pager
   ```
4. With a DBA read-only session, identify for the suspect job:
   - `next_run_time`, `is_enabled`, `job_schedule`, `frequency`
   - recent `partitioning_job_table_log` rows (`last_run_status`,
     `job_runtime`, `execution_duration_ms`)
5. Verify scheduler functions exist:
   ```sql
   SELECT to_regprocedure(
     'mubasher_oms.run_partition_job_scheduled(numeric,timestamp without time zone)'
   );
   SELECT to_regprocedure(
     'mubasher_oms.get_upcoming_partition_jobs(interval)'
   );
   ```
6. Verify deployed SQL includes fail-closed occurrence safety and that Python
   includes the short-lived rapid-loop breaker in
   `scheduler_backend/scheduler.py` (TTL 300s; caches EXECUTED / FAILED /
   FAILED_INVALID_SCHEDULE only — not ordinary SKIPPED_* / NOT_FOUND).
7. Restart the scheduler **only after** root cause is understood and corrected
   SQL + Python are both live.

### 10.2 Scheduler pre-start safety checklist

Before `systemctl start partition-job-scheduler.service`:

- [ ] Scheduler remains stopped until checks pass
- [ ] `.env.realtime` is `enterprisedb:enterprisedb` mode `600`
- [ ] API `.env` is `partitionui:partitionui` mode `600` (API must not read `.env.realtime`)
- [ ] Both realtime functions resolve via `to_regprocedure`
- [ ] `mubasher_oms.cron_to_interval_or_next_run(text)` resolves via
      `to_regprocedure` (source not currently versioned in this repo — review
      the **live** definition during controlled deploy; see ISSUE-003)
- [ ] Phase 0 fail-closed occurrence-safety SQL is deployed (body matches
      `sql/realtime_scheduler_v1.sql`; no invented `+1 day` fallback)
- [ ] Deployed Python includes rapid-loop breaker (TTL 300s, max 512;
      caches only EXECUTED / FAILED / FAILED_INVALID_SCHEDULE) +
      `expected_run_time` logging
- [ ] Jobs with `next_run_time IS NULL` after fail-closed are reviewed by an
      operator before re-enabling a schedule
- [ ] Watch journal after start for first executions and any CRITICAL breaker lines

Manual API start (for debugging):

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui
sudo -u partitionui .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8001
```

Manual UI start (for debugging):

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui/frontend
sudo -u partitionui ./node_modules/.bin/next start --hostname 0.0.0.0 --port 8501
```

---

## 11. Security reminders

- Do not run services as root.
- `.env` → `partitionui:partitionui` mode `600`.
- `.env.realtime` → `enterprisedb:enterprisedb` mode `600`.
- Do not expose FastAPI (`8001`) or scheduler control (`8765`) publicly; keep localhost.
- Restrict `8501` to administrator networks.
- UI DB user must not be a superuser and must not write pgAgent catalogs.
- Never commit secrets.
- Never “fix” env access with world-readable permissions.

---

## 12. Architecture reminder (do not “fix” by rewriting)

```text
Browser
  -> Next.js :8501
       proxies /api/*  (PARTITION_API_ORIGIN=http://127.0.0.1:8001)
  -> FastAPI 127.0.0.1:8001
       calls database.py / validators / job_autofill / scheduler_client
  -> PostgreSQL functions/tables

Separate process:
  scheduler_backend 127.0.0.1:8765
       loads .env.realtime only
       calls get_upcoming_partition_jobs / run_partition_job_scheduled
```

Safe write paths:

- Create job → `insert_data_to_partition_job_table(...)`
- Edit job → `update_data_to_partition_job_table(...)` (after migration)
- Manual run → `run_partition_job_manual(...)`
- Scheduled run → `run_partition_job_scheduled(...)` (scheduler only)

Short-lived DB connections only. No connection pools in the UI/API.

---

## 13. Rollback note (Streamlit)

Streamlit UI is archived under `archives/streamlit-ui-*.zip`.

Do **not** unzip over the live tree unless you intentionally restore Streamlit and rewrite the systemd unit back to Streamlit. Prefer fixing the Next.js/API stack.

---

## 14. Quick “green” definition

Install is successful when all of these are true:

1. `partition-job-api.service` is `active`
2. `partition-job-ui.service` is `active`
3. `curl http://127.0.0.1:8001/api/health` returns `ok: true`
4. `curl http://127.0.0.1:8501/` returns HTTP 200
5. Browser opens the PartOps sidebar UI on port 8501
6. (For automatic execution) scheduler service is `active`, required DB functions exist, and status URL responds

If any step fails, capture:

```bash
systemctl status partition-job-api.service partition-job-ui.service --no-pager -l
journalctl -u partition-job-api.service -u partition-job-ui.service -n 100 --no-pager
pwd
ls -la api/main.py frontend/package.json frontend/.next .venv/bin/python .env .env.realtime
```

and escalate with that output.