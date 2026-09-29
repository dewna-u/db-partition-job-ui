# Partition Manager — Installation Runbook

Audience: teammates installing or redeploying when the owner is unavailable.

This is the **live** stack:

| Layer | Technology | Service | Port |
|---|---|---|---|
| UI | Next.js | `partition-job-ui.service` | `8501` (public to admins) |
| API | FastAPI | `partition-job-api.service` | `127.0.0.1:8000` (localhost only) |
| Scheduler | Python backend | `partition-job-scheduler.service` | `127.0.0.1:8765` (control API) |
| Data | PostgreSQL / EDB | — | as configured in `.env` |

Streamlit is **archived** (not used). Do not start Streamlit.

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

chown -R partitionui:partitionui /opt/db-partition-job-ui-github
cd /opt/db-partition-job-ui-github/partition-job-ui
```

Confirm layout:

```bash
pwd
# expect: /opt/db-partition-job-ui-github/partition-job-ui

ls -la api/main.py frontend package.json database.py .venv/bin/python
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
# owner is usually the scheduler OS user (often enterprisedb) — match your unit file
vi .env.realtime
```

`REALTIME_DB_EXPECTED_NAME` **must** equal `current_database()` on that DB.

---

## 4. Python virtualenv + API deps

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui

# Create venv once if missing
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  chown -R partitionui:partitionui .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Smoke import (must succeed on Python 3.9)
sudo -u partitionui .venv/bin/python -c "import fastapi, uvicorn, api.main; print('API imports OK')"
```

---

## 5. Build the Next.js frontend

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui/frontend

# Prefer ci when lockfile exists
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi

npm run build
test -d .next
test -x node_modules/.bin/next

cd ..
chown -R partitionui:partitionui frontend
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
.../.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

UI ExecStart must look like:

```text
.../frontend/node_modules/.bin/next start --hostname 0.0.0.0 --port 8501
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
curl -sS http://127.0.0.1:8000/api/health; echo
curl -sS http://127.0.0.1:8000/health; echo
# expect: {"ok":true,"service":"partition-manager-api"}

# UI responds
curl -sS -o /dev/null -w "ui_http=%{http_code}\n" http://127.0.0.1:8501/
# expect: ui_http=200

# Optional: OpenAPI reachable
curl -sS -o /dev/null -w "openapi=%{http_code}\n" http://127.0.0.1:8000/openapi.json
```

Browser smoke test (from an allowed admin network):

1. Open `http://<server-ip>:8501`
2. Sidebar shows Overview / Convert / Create / Jobs / History
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
chown -R partitionui:partitionui .

.venv/bin/pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..
chown -R partitionui:partitionui frontend

cp systemd/partition-job-api.service /usr/lib/systemd/system/
cp partition-job-ui.service /usr/lib/systemd/system/
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

## 9. Scheduler backend (optional but production-normal)

The UI/API are **not** the scheduler.

1. Ensure SQL realtime functions are installed on the target DB (`sql/` migrations — DBA step).
2. Configure `.env.realtime`.
3. Install/adjust `systemd/partition-job-scheduler.service` for this host’s paths + OS user.
4. Enable:

```bash
systemctl enable --now partition-job-scheduler.service
curl -sS http://127.0.0.1:8765/health || true
curl -sS http://127.0.0.1:8765/internal/scheduler/status || true
```

If the scheduler is down:

- Job **configuration** still works
- Overview shows scheduler offline
- Jobs will not fire until the scheduler is healthy again

---

## 10. Troubleshooting (known real failures)

| Symptom | Likely cause | Fix |
|---|---|---|
| API `203/EXEC` | Wrong path or missing uvicorn | Paths must be `/opt/db-partition-job-ui-github/partition-job-ui`; `pip install -r requirements.txt`; use `python -m uvicorn` |
| UI `200/CHDIR` | Wrong `WorkingDirectory` | Must be `.../partition-job-ui/frontend` |
| API crash: `ManualRunBody \| None` | Python 3.9 + modern union syntax | Use `Optional[ManualRunBody]` in `api/routers/jobs.py` (already fixed in current tree) |
| UI starts then permission errors | `frontend/.next` owned by root | `chown -R partitionui:partitionui frontend` |
| API `active` but curl `/api/health` = Not Found immediately | Curled during startup | `sleep 2` then retry |
| Units ignore repo files | Stale unit in `/etc/systemd/system/` | Delete `/etc` copies; keep `/usr/lib/systemd/system/` |
| Overview shows DB errors | Bad `.env` or missing grants | Fix `.env`; ask DBA for least-privilege grants (UI never grants) |
| Scheduler always offline | Scheduler service down / wrong status URL | Start scheduler; check `PARTITION_SCHEDULER_STATUS_URL` |

Manual API start (for debugging):

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui
sudo -u partitionui .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Manual UI start (for debugging):

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui/frontend
sudo -u partitionui ./node_modules/.bin/next start --hostname 0.0.0.0 --port 8501
```

---

## 11. Security reminders

- Do not run services as root.
- `.env` / `.env.realtime` mode `600`, correct owner.
- Do not expose FastAPI (`8000`) or scheduler control (`8765`) publicly; keep localhost.
- Restrict `8501` to administrator networks.
- UI DB user must not be a superuser and must not write pgAgent catalogs.
- Never commit secrets.

---

## 12. Architecture reminder (do not “fix” by rewriting)

```text
Browser
  -> Next.js :8501
       proxies /api/* 
  -> FastAPI 127.0.0.1:8000
       calls database.py / validators / job_autofill / scheduler_client
  -> PostgreSQL functions/tables
  -> scheduler_backend (separate process) reads upcoming jobs and fires timers
```

Safe write paths:

- Create job → `insert_data_to_partition_job_table(...)`
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
3. `curl http://127.0.0.1:8000/api/health` returns `ok: true`
4. `curl http://127.0.0.1:8501/` returns HTTP 200
5. Browser opens the Partition Manager sidebar UI on port 8501
6. (If used) scheduler service is `active` and status URL responds

If any step fails, capture:

```bash
systemctl status partition-job-api.service partition-job-ui.service --no-pager -l
journalctl -u partition-job-api.service -u partition-job-ui.service -n 100 --no-pager
pwd
ls -la api/main.py frontend/.next .venv/bin/python .env
```

and escalate with that output.
