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

Minimum required values (env credential provider — default / first rollout):

```bash
PARTITION_DB_SECRET_PROVIDER=env
DB_HOST=127.0.0.1
DB_PORT=5444
DB_NAME=<your_database>
DB_USER=partition_job_ui
DB_PASSWORD=<secret>
DB_SSLMODE=prefer
DB_CONNECT_TIMEOUT=5
# Optional (Phase 5F.1): connect timeout for readiness probes only
# (default 3 s, clamped 2..10). API/scheduler connections keep DB_CONNECT_TIMEOUT.
# PARTITION_READINESS_CONNECT_TIMEOUT_SECONDS=3
```

AWS Secrets Manager provider (after IAM + secret exist):

```bash
PARTITION_DB_SECRET_PROVIDER=aws
PARTITION_DB_SECRET_ID=partops/example/db-credentials
AWS_REGION=<aws-region>
PARTITION_DB_SECRET_CACHE_TTL_SECONDS=300
DB_HOST=127.0.0.1
DB_PORT=5444
DB_NAME=<your_database>
DB_USER=partition_job_ui
# DB_PASSWORD — omit after AWS mode is verified (never used as AWS fallback)
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

### 3.1b AWS Secrets Manager rollout (production)

Do **not** switch to AWS mode until the secret and IAM role exist.

1. Create a Secrets Manager secret whose `SecretString` is JSON:
   `{"username":"...","password":"..."}` (`password` required; `username` optional).
2. Grant the EC2 instance role `secretsmanager:GetSecretValue` on that secret.
3. Deploy PartOps source that includes `db_credentials.py` / boto3.
4. Keep `PARTITION_DB_SECRET_PROVIDER=env` initially; verify API/scheduler healthy.
5. Set `PARTITION_DB_SECRET_PROVIDER=aws`, `PARTITION_DB_SECRET_ID=...`, and
   `AWS_REGION` in `.env` and `.env.realtime` (same provider settings on both
   processes as appropriate).
6. Restart `partition-job-api.service` and `partition-job-scheduler.service`.
7. Verify `/api/health`, readiness credential provider = aws, and a DB-backed page/job.
8. Remove `DB_PASSWORD` from `.env` / `.env.realtime` (mode 600); restart; verify again.

Rollback:

1. Set `PARTITION_DB_SECRET_PROVIDER=env`.
2. Restore `DB_PASSWORD` securely (never commit).
3. Restart API and scheduler; verify.

AWS mode **never** falls back to `DB_PASSWORD` on Secrets Manager failure.

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
PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS=1
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

Phase 5A note: with the 5A Python deployed, an uncommitted outcome
(`FAILED_DATABASE`, `FAILED_CONNECTION`, `SKIPPED_TARGET_LOCKED`) is retried
only on the bounded, delayed schedule (defaults: 4 attempts at 15/60/180 s;
busy table: 5–30 s cooldowns for up to 1 h) and then recorded as a
`RETRY_EXHAUSTED` review item — look for `event=scheduled_occurrence_retry_*`
lines. Repeated *immediate* executions of one occurrence indicate pre-5A
Python is running.

### 10.2 Scheduler pre-start safety checklist

Before `systemctl start partition-job-scheduler.service`:

- [ ] Scheduler remains stopped until checks pass
- [ ] Controlled deploy completed: pulled source matches intended Git revision
- [ ] `.env.realtime` is `enterprisedb:enterprisedb` mode `600`
- [ ] API `.env` is `partitionui:partitionui` mode `600` (API must not read `.env.realtime`)
- [ ] Both realtime functions resolve via `to_regprocedure`
- [ ] `mubasher_oms.cron_to_interval_or_next_run(text)` resolves via
      `to_regprocedure` (source not currently versioned in this repo — review
      the **live** definition during controlled deploy; see ISSUE-003)
- [ ] Phase 0 fail-closed occurrence-safety SQL is deployed (body matches
      `sql/realtime_scheduler_v1.sql`; no invented `+1 day` fallback)
- [ ] Phase 2 target-table advisory lock SQL is deployed when enabling
      concurrency > 1 (migration
      `20261013_01_scheduled_target_advisory_lock.sql`; body matches
      `sql/realtime_scheduler_v1.sql`; contains `87421032` /
      `SKIPPED_TARGET_LOCKED`)
- [ ] `.env.realtime` has `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS=1` on first
      Phase 2 deploy (serial). Increase to `2` only after verification.
- [ ] Deployed Python includes transaction-boundary fix:
      identity check then `conn.commit()` before yield so no open transaction
      remains when business `conn.transaction()` begins
- [ ] Deployed Python maps incomplete DB/txn errors to `FAILED_DATABASE`
      (not cacheable `FAILED`)
- [ ] Deployed Python includes rapid-loop breaker (TTL 300s, max 512;
      caches only EXECUTED / FAILED / FAILED_INVALID_SCHEDULE) +
      `expected_run_time` logging
- [ ] SQL migration version and Python source version are known-good together
- [ ] Jobs with `next_run_time IS NULL` after fail-closed are reviewed by an
      operator before re-enabling a schedule
- [ ] Phase 5A: `20261110_01` and `20261110_02` applied; db_config pre-check
      (§10.6) run; Python includes `scheduler_backend/retry_policy.py`
- [ ] Phase 5A: operators know that historical overdue occurrences will be
      recorded as *Needs review* (not executed) on start
- [ ] Start scheduler only after the above; watch journal for first executions
      and any rapid-loop breaker CRITICAL lines

### 10.3 Phase 2 — controlled parallel execution deploy order

Server-side steps (operator). Do **not** jump to concurrency `3` on day one.

1. `git pull` (intended revision).
2. Review
   `sql/migrations/20261013_01_scheduled_target_advisory_lock.sql`.
3. Back up the current live definition of
   `mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone)`
   (e.g. `pg_get_functiondef`).
4. Apply the Phase 2 SQL migration in a DBA window
   (`BEGIN`/`COMMIT` already in the file). Prefer `CREATE OR REPLACE` (no
   `DROP FUNCTION` / no `CASCADE`).
5. Verify function still resolves; confirm owner remains `mubasher_oms` and
   `partition_job_ui` / scheduler role still have `EXECUTE`.
   Confirm source contains `87421032` and `SKIPPED_TARGET_LOCKED`.
6. Add to `.env.realtime` (mode 600, `enterprisedb`):
   `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS=1`
7. Restart `partition-job-scheduler.service`.
8. Verify `/health` and `/internal/scheduler/status`
   (`max_concurrent_jobs=1`, `active_job_count` ≤ 1).
9. Confirm serial execution with concurrency `1`.
10. When stable, set `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS=2`.
11. Restart scheduler.
12. Verify two jobs on **different** tables may run concurrently
    (`active_job_count` ≤ 2; status `available_slots` coherent).
13. Confirm same target table is serialized (`SKIPPED_TARGET_LOCKED` or
    sequential completion — never overlapping CREATE/DROP on same table).
14. Observe DB load before increasing further (only then consider `3`).

Rollback (serial without reverting source):

```bash
# in .env.realtime
PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS=1
sudo systemctl restart partition-job-scheduler.service
```

This restores serial scheduler behaviour. SQL target lock may remain deployed
(safe; with concurrency `1` it is rarely contended).

### 10.4 Phase 3 — Datadog observability (future server setup)

PartOps emits optional DogStatsD metrics and structured logs. The **Datadog
Agent** owns the Datadog API key. Never put a Datadog API key in PartOps `.env` /
`.env.realtime`.

#### Deploy order (operator)

1. Install Datadog Agent using the approved company method.
2. Configure the Datadog API key **only** in Agent configuration (not PartOps).
3. Enable journald/systemd log collection for:
   - `partition-job-api.service`
   - `partition-job-ui.service`
   - `partition-job-scheduler.service`
   Ensure Agent `logs_enabled: true` (or equivalent company template).
4. Verify Agent DogStatsD listens on localhost UDP `8125`.
5. Deploy PartOps source (`git pull` intended revision).
6. Add to `.env` (API) and `.env.realtime` (scheduler), mode `600`:
   ```bash
   PARTOPS_DATADOG_ENABLED=true
   PARTOPS_DOGSTATSD_HOST=127.0.0.1
   PARTOPS_DOGSTATSD_PORT=8125
   PARTOPS_ENVIRONMENT=production
   ```
7. Restart API and/or scheduler as appropriate (`systemctl restart …`).
   Keep scheduler stopped if overdue-job policy still requires it.
8. Verify metrics arrive in Datadog (`partops.scheduler.*`, `partops.api.*`).
9. Verify journal logs arrive for the three units.
10. Create dashboard/monitors manually (or approved Datadog tooling) — do **not**
    drive this from PartOps code.

#### Rollback

```bash
PARTOPS_DATADOG_ENABLED=false
# restart API / scheduler as needed
```

PartOps continues to operate if the Agent is stopped or UDP send fails.

#### Suggested dashboard widgets

- Scheduler heartbeat
- Queue depth / overdue jobs
- Active jobs / max concurrency / available slots
- **Failed jobs (current):** `max:partops.jobs.failed_count{…}`
  (enabled rows with `last_run_status='FAIL'` — not attempt count)
- Job execution events by status (`partops.job.execution` — troubleshooting)
- CREATE vs DROP counts (`operation` tag)
- Job duration p50 / p95 / p99 (`partops.job.duration_ms`)
- DB connection failures (by `component`)
- Secrets Manager retrieval failures / refresh
- API request / error (`status_class`)
- systemd / HTTP health (Agent checks)
- EC2 CPU / memory / disk

Note: do **not** use `sum`/`count` of `partops.job.execution{status:failed*}` as
the primary “Failed Partition Jobs” KPI — that counts attempts. Use
`partops.jobs.failed_count` instead.

#### Recommended monitors (starting points — tune with ops evidence)

| # | Monitor | Starting idea |
|---|---------|---------------|
| 1 | Scheduler heartbeat missing | no `partops.scheduler.heartbeat` for ~2–3× reconcile interval |
| 2 | Scheduler service down | systemd / process check |
| 3 | API service down | systemd / process check |
| 4 | UI service down | systemd / process check |
| 5 | API `/health` failing | Agent HTTP check |
| 6 | Scheduler `/health` failing | Agent HTTP check `127.0.0.1:8765/health` |
| 7 | Enabled jobs currently FAIL | `max:partops.jobs.failed_count` above threshold |
| 8 | Execution failure event rate | `partops.job.execution{status:failed*}` (secondary) |
| 9 | High job duration | p95 `partops.job.duration_ms` |
| 10 | Queue depth increasing | `partops.scheduler.queue_depth` |
| 11 | Overdue/due jobs above threshold | `partops.scheduler.overdue_jobs` |
| 12 | Sustained saturation | `active_jobs >= max_concurrent_jobs` for several minutes |
| 13 | DB connection failures | `partops.db.connection_failure` |
| 14 | Secrets Manager failures | `partops.secrets_manager.retrieval_failure` |
| 15–17 | Host CPU / memory / disk | Agent host metrics |

Do not set aggressive production thresholds without operational evidence.

### 10.5 Phase 4 — secure Edit / Enable / Disable deploy order

Do **not** grant `UPDATE` on `partitioning_job_table` to `partition_job_ui`.

1. `git pull` (intended revision).
2. Review
   `sql/migrations/20261013_02_secure_update_partition_job.sql`.
3. Back up the live function:
   ```sql
   SELECT pg_get_functiondef(
     'mubasher_oms.update_data_to_partition_job_table(numeric,character varying,boolean,character varying,character varying,jsonb,character varying,interval,timestamp without time zone,character varying,numeric,boolean,interval)'::regprocedure
   );
   ```
4. Inspect current owner / security / grants (`pg_proc`, `aclexplode`).
5. Apply the Phase 4 migration in a DBA window.
6. Verify:
   - `prosecdef` = true (SECURITY DEFINER)
   - owner = `mubasher_oms`
   - fixed `search_path` includes `pg_catalog, mubasher_oms, pg_temp`
   - PUBLIC has no EXECUTE
   - `partition_job_ui` has EXECUTE
7. Restart `partition-job-api.service` (UI if needed).
8. UI: edit a harmless field on an approved non-critical job → Save → confirm
   DB + UI.
9. Disable that approved job → confirm `is_enabled=false`, history retained,
   `next_run_time` retained.
10. Re-enable → confirm a **future** `next_run_time`.
11. When the scheduler is running later: confirm refresh after edit and that a
    queued occurrence for a disabled job returns `SKIPPED_DISABLED`.

Rollback: restore the previous function body from the backup
(`CREATE OR REPLACE` / prior definition). Do **not** “fix” by granting
table UPDATE to `partition_job_ui`.

### 10.6 Phase 5A — missed runs, bounded retry, manual lock, db_config allowlist

What changes operationally:

* **Historical occurrences are never executed automatically.** An occurrence
  the scheduler first sees *already overdue* (more than
  `PARTITION_SCHEDULER_OVERDUE_TOLERANCE_SECONDS`, default 60 s, past its
  scheduled time) — typically on start after downtime, or after a DB outage —
  becomes a *Needs review* item and the job moves to its next future
  occurrence. Starting the scheduler no longer catches up historical runs.
* **Owned occurrences are not "missed" just because time passed.** An
  occurrence seen on time may run late after waiting for a concurrency slot,
  a busy table or a retry backoff. Hard limits still apply: the retry budget,
  the target-lock wait budget, and the DB safety ceiling
  `mubasher_oms.partition_job_max_execution_lateness()` (default 6 hours).
* Ownership is in memory: **restarting the scheduler turns any overdue
  occurrence into a historical one** (review item, not executed). Avoid
  restarting while jobs are queued if you want them to run.
* DB/connection failure on an owned occurrence: 4 attempts at 15/60/180 s
  (≈ 4 min), then a `RETRY_EXHAUSTED` review item. Busy target table:
  5/10/20/30 s cooldowns for up to 1 hour, then a review item. All values are
  configurable (`.env.realtime.example`); invalid values stop the scheduler at
  startup. The scheduler never spins on the same occurrence.
* `partops.jobs.failed_count` no longer counts a job whose failed scheduled run
  was followed by a successful manual run (Run now / missed-run recovery).
  `last_run_status` itself is unchanged (latest *scheduled* outcome).
* Manual "Run now" on a table that is being processed returns HTTP 409
  (nothing executed).
* `db_config_para` accepts only allowlisted keys (see README Phase 5A).
  **Pre-check existing rows before deploying** — rows with other keys will
  FAIL (logged, schedule advanced) at their next execution until edited:

  ```sql
  SELECT j.job_id, j.job_name, c.key, c.value
    FROM mubasher_oms.partitioning_job_table AS j
    CROSS JOIN LATERAL jsonb_each_text(j.db_config_para) AS c
   WHERE lower(c.key) NOT IN ('lock_timeout','statement_timeout',
           'idle_in_transaction_session_timeout','work_mem','maintenance_work_mem',
           'effective_io_concurrency','random_page_cost',
           'max_parallel_maintenance_workers','datestyle')
      OR (lower(c.key) = 'datestyle'
          AND c.value !~* '^[[:space:]]*iso[[:space:]]*(,[[:space:]]*(dmy|mdy|ymd)[[:space:]]*)?$');
  ```

Deploy order (operator, DBA window):

1. `sudo systemctl stop partition-job-scheduler.service` (keep it stopped
   until step 7).
2. `git pull` (intended revision); review both migrations.
3. Run the db_config pre-check above (read-only). Fix or record offending jobs.
4. Back up current definitions (`pg_get_functiondef`) of
   `run_partition_job_scheduled`, `run_partition_job_manual`,
   `update_data_to_partition_job_table`.
5. Apply `sql/migrations/20261110_01_partition_job_attention.sql`, then
   `sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql`.
   Then (Phase 5B) `sql/migrations/20261110_03_partops_framework_version.sql`
   and run the read-only preflight (§16.6). The Phase 5B scheduler will not
   dispatch anything until the preflight's scheduler scope passes — this
   includes the legacy functions, which must already exist on the hub.
6. Verify (read-only):
   - `to_regclass('mubasher_oms.partition_job_attention')` not NULL
   - `to_regprocedure('mubasher_oms.reconcile_missed_partition_job(numeric,timestamp without time zone,text,text)')` not NULL
   - owners `mubasher_oms`; PUBLIC has no EXECUTE on the touched functions;
     `partition_job_scheduler` has EXECUTE on scheduled + reconcile;
     `partition_job_ui` has EXECUTE on manual / recovery / acknowledge / update
   - `SELECT mubasher_oms.partition_job_max_execution_lateness();` → `06:00:00`
   - `partition_job_scheduler` has SELECT on `partitioning_job_table_log`
     (read-only; failed-jobs gauge)
7. Deploy Python + rebuild UI (§8), restart API and UI, then start the
   scheduler and watch the journal:
   ```bash
   sudo journalctl -u partition-job-scheduler.service -f | grep -E \
     'missed_occurrence_reconciled|retry_scheduled|retry_exhausted|scheduler_refresh_ok'
   ```
   After downtime, expect one `historical_occurrence_discovered` and one
   `missed_occurrence_reconciled` line per job whose next run was in the past —
   and **no** CREATE/DROP for those.
8. UI → Jobs: amber banner lists the missed runs. Review each.

DBA review of a missed run (Jobs → job → *Needs review*):

1. Check missed time, operation (CREATE/DROP), target and current partitions.
2. Either **Run missed occurrence** (confirm; DROP also requires typing
   `schema.table`) or **Acknowledge without running** (optional note).
3. Neither action changes the job's next normal run.
4. A failed recovery attempt stays *Needs review* with the attempt recorded;
   the job history shows a `MANUAL_FAIL` row.

Emergency behaviour:

* If `retry_exhausted` / `missed_occurrence_reconciled` lines flood the
  journal, stop the scheduler (§10.1) — the DB already holds the evidence in
  `partition_job_attention`; nothing is lost by stopping.
* Pending review count: `SELECT count(*) FROM mubasher_oms.partition_job_attention WHERE status = 'PENDING_REVIEW';`
* Datadog: `partops.jobs.pending_review_count`, `partops.scheduler.retry_exhausted`.

Rollback:

* Python/UI: redeploy the previous revision (pre-5A Python falls back to
  pre-5A immediate retries — prefer keeping 5A Python and rolling back only
  SQL if needed).
* SQL: re-apply the backed-up bodies (or `20261013_01` + `20261013_02` +
  the CREATE section of `20260929_03`'s manual body — never its DROP), then
  drop the three new functions (see migration header). Restoring the pre-5A
  scheduled body **re-enables automatic execution of overdue occurrences** —
  keep the scheduler stopped while deciding. Keep `partition_job_attention`
  unless the evidence is no longer needed.

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

### 10.7 Phase 5C — hub-local database targets (foundation)

Architecture: ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.

* The PartOps database you already run is the **control database**. It holds
  the target registry `mubasher_oms.partops_database_target` and all job rows.
  Migration `20261117_01` registers it automatically as target **`db01`**
  (`is_control_database = TRUE`, `database_name = current_database()`).
* Every job gets `database_target_id` (backfilled to `db01`, `NOT NULL`,
  FK `ON DELETE RESTRICT`). Business configuration, `next_run_time` and
  `db_config_para` are not touched by the backfill.
* **Phase 5C does not execute anything on additional targets.** The SQL
  executors skip jobs of other targets (`SKIPPED_REMOTE_TARGET` /
  `MANUAL_REMOTE_TARGET`, nothing executed or logged), `get_upcoming` returns
  only control-database jobs, and the API refuses (HTTP 409) creating or
  moving jobs to a non-control target. Remote execution is Phase 5D.
* Registry rows contain **no passwords and no secret values** — only an AWS
  Secrets Manager secret *identifier* (`secret_id`) for `aws` targets.

Deploy order (after §10.6 is live; disposable validation first):

1. Stop the scheduler (`partition-job-scheduler`). The API may stay up.
2. Apply `sql/migrations/20261117_01_partops_database_target.sql` as the
   schema owner. It refuses to finish if any job would be left without a
   target.
3. Apply `sql/migrations/20261117_02_target_aware_executors.sql`.
4. Run the preflight (§16.6). Expected framework version: `20261117_02`;
   check `control_target` = PASS (exactly one enabled control row naming the
   connected database).
5. Deploy the Python code (API + scheduler), restart the API.
6. Start the scheduler. `GET /health` must show `framework_status: ready`
   and `control_target_key: db01`. If the control row is missing, disabled or
   names another database the scheduler stays **blocked** (dispatches nothing).
7. `GET /api/targets` lists `db01`; `GET /api/targets/1/health` is
   `HEALTHY_WITH_WARNINGS` while the legacy definitions are not captured
   (Phase 5B.1 deferred) — warnings, not failures.

Rollback (no data loss; jobs keep their configuration):

1. **Application rollback (usually sufficient):** stop the scheduler, deploy
   the previous Python build, start it. Older code never selects the new
   column; every job is on the control target, so the guards never trigger.
   The older preflight reports the newer database version `20261117_02` as a
   WARNING (not a failure), so the scheduler gate still passes.
2. **Full SQL rollback (only if required):** with the scheduler stopped,
   re-apply `20261110_02_missed_run_reconcile_retry_and_manual_lock.sql`
   (restores the pre-5C executor bodies), then run the rollback statements in
   the header of `20261117_01` (FK, column, function, table) and
   `DELETE FROM mubasher_oms.partops_framework_version WHERE migration_id IN
   ('20261117_01','20261117_02')`. Do this only together with step 1.

### 10.8 Phase 5D — remote execution on managed targets

Architecture: ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.
Design reference: `REMOTE_EXECUTION.md` (diagrams, state machine, rules).

What changes operationally:

* Jobs on enabled managed targets (db02, …) are executed by the scheduler's
  remote orchestrator; db01 jobs keep `run_partition_job_scheduled`.
* Every remote attempt is recorded in `mubasher_oms.partops_execution_attempt`
  (control DB). There is no distributed transaction: a target outcome that
  cannot be proven becomes **OUTCOME_UNKNOWN** and is **never re-run
  automatically** — the job is paused, a `REMOTE_OUTCOME_UNKNOWN` review item
  is created, and edits / re-enable of that job are refused until resolved.
* Proven failures (no connection, preflight, table busy, worker error with
  confirmed rollback) use the Phase 5A bounded retry; exhaustion creates a
  `RETRY_EXHAUSTED` item and moves the schedule.
* A confirmed target COMMIT is never executed again; only the control
  finalization is retried (also after a restart).
* Manual "Run now" / missed-run recovery of a db02+ job is executed by the
  scheduler (must be running; HTTP 503 otherwise). DROP still needs explicit
  confirmation. HTTP 502 = outcome unknown → do NOT re-run; inspect the target.
* db01 also honours `partops_database_target.max_concurrent_jobs` now
  (default 1). If the hub runs `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS > 1`
  for db01 jobs, raise db01's limit first (DBA):
  `UPDATE mubasher_oms.partops_database_target SET max_concurrent_jobs = <n> WHERE target_key = 'db01';`

Deploy order (after §10.7; disposable validation first):

1. Stop the scheduler (`partition-job-scheduler`).
2. Apply `sql/migrations/20261117_03_remote_execution_ledger.sql` (schema owner).
3. Apply `sql/migrations/20261117_04_remote_execution_functions.sql`.
4. Preflight (§16.6): expected framework version `20261117_04`.
5. On every managed target that will run jobs: the legacy workers exist and
   the scheduler login role has USAGE on `mubasher_oms` and EXECUTE on
   `create_any_table_partition` / `drop_any_table_partition`
   (`GET /api/targets/{id}/health` → `HEALTHY_WITH_WARNINGS` while Phase 5B.1
   is deferred; FAIL = blocked). No PartOps tables are installed on targets.
6. Per-target credentials for the scheduler AND the API (AWS secret id in the
   registry; `env` only for local development).
7. Deploy Python, restart the API, start the scheduler. `GET /health` →
   `framework_status: ready`; status shows `targets`, `global_active`,
   `unknown_outcomes`.
8. Enable the target (`is_enabled = true`, DBA) and assign jobs (API:
   `database_target_id`, target must be healthy).

Rollback:

1. Disable managed targets (`is_enabled = false`, DBA) so no new remote work
   starts; wait until no attempt is DISPATCHING / TARGET_SUCCEEDED
   (`GET /api/execution-attempts?open=true`). Resolve any OUTCOME_UNKNOWN.
2. Stop the scheduler; deploy the Phase 5C build (it discovers only db01 jobs
   via `get_upcoming_partition_jobs`; remote jobs are skipped, never run
   locally). The 5C preflight reports the newer DB version as a WARNING.
3. Full SQL rollback only if required: move remote jobs back to db01 (or keep
   them disabled), re-apply the `reconcile_missed_partition_job` body from
   `20261117_02`, drop the 20261117_04 functions, the triggers/functions of
   20261117_03 (header), and delete version rows `20261117_03/_04`. Keep the
   ledger and attention evidence unless explicitly no longer needed.

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

---

## 15. Local laptop development (no production DB)

Use this when developing on a workstation (Cursor on laptop). Production
server `systemctl` steps above do **not** apply.

### Safety

- Never point local `.env` / `.env.realtime` at `172.20.224.68`, `ukbodev`,
  or other real GTN databases unless explicitly approved.
- Do not start the scheduler until `DB_HOST` is localhost and PartOps schema
  objects exist in a dedicated local/test database.
- API `GET /api/health` does not require a database.

### Start

```powershell
cd <repo>\partition-job-ui
.\.venv\Scripts\python.exe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8001
# other terminal (Node on PATH):
cd frontend
npm ci
npm run dev -- -H 127.0.0.1 -p 8501
# or: .\scripts\dev-local.ps1
```

### Verify

```powershell
curl http://127.0.0.1:8001/api/health
curl -o NUL -w "%{http_code}" http://127.0.0.1:8501/
```

### Stop

Close the terminal windows or stop the PIDs printed by `scripts/dev-local.ps1`.
---

## 16. Database Framework Capture and Installation

Audience: a DBA who has never seen PartOps. Details and file layout:
`sql/framework/README.md`. Object list: `sql/framework/INVENTORY.md`
(generated from `sql/framework/framework_manifest.json`).

### 16.1 What the framework is

Every database object PartOps needs on a hub database, in two groups:

* **PartOps-owned** — defined and versioned in this repository:
  `run_partition_job_scheduled`, `get_upcoming_partition_jobs`,
  `reconcile_missed_partition_job`, `run_partition_job_manual`,
  `update_data_to_partition_job_table`, `run_partition_job_attention_recovery`,
  `acknowledge_partition_job_attention`, the attention table + trigger, the
  allowlist / max-lateness helpers and `partops_framework_version`.
* **Existing / legacy framework** — come from the approved reference database:
  `cron_to_interval_or_next_run`, `create_any_table_partition`,
  `drop_any_table_partition`, `insert_data_to_partition_job_table`
  (and possibly `cron_field_to_int_array`). **Their definitions are not yet
  in Git** (ISSUE-003, ISSUE-008). Tables, sequences and
  `cron_schedule_result` are reconstructed by
  `sql/bootstrap_partition_job_framework.sql`.

Never write a legacy function body from memory, from its callers or from tests.

### 16.2 Capture (read-only, approved reference DB only)

1. Record which database is the authoritative reference and who approved it.
2. With a read-only DBA login:
   ```bash
   psql "host=<ref-host> dbname=<ref-db> user=<dba>" -X -v ON_ERROR_STOP=1 -P pager=off \
        -f sql/capture_reference_framework.sql -o capture_<ref-db>_<YYYYMMDD>.txt
   ```
   The script opens `BEGIN TRANSACTION READ ONLY` and ends with `ROLLBACK`;
   it cannot modify anything. Last line: "Capture complete …".
3. Keep the output file local; check function bodies for embedded
   credentials before sharing (if found: stop and escalate).

### 16.3 Review

1. For each missing object copy the section-5 definition verbatim into
   `sql/framework/captured/<name>.sql` with the header from
   `sql/framework/captured/README.md` (include the md5).
2. Compare owner / SECURITY mode / `proconfig` search_path / EXECUTE grants
   (sections 3–4) with the manifest.
3. Complete `sql/framework/SECURITY_REVIEW_CHECKLIST.md` per function
   (dynamic SQL quoting, DROP behaviour, SET/role switching, transaction
   control, exception swallowing, EDB `sys` search_path note).
4. Update the manifest entry (`definition_in_git: yes`, `status: versioned`,
   security, owner, grants), delete the `.CAPTURE_REQUIRED.md` placeholder,
   run `python scripts/framework_inventory.py --write`.

### 16.4 Version and validate

* Framework version = `max(migration_id)` in
  `mubasher_oms.partops_framework_version`; the application expects the
  manifest's `framework_version` (`20261110_03`). Older/unknown → FAIL, newer →
  WARNING. Only reviewed migrations write it; PartOps never auto-upgrades.
* Local proof on a disposable cluster (existing PostgreSQL binaries, nothing
  installed, nothing shared):
  ```bash
  PG_BIN="/path/to/postgres/bin" scripts/disposable_db/run_disposable_db_test.sh --i-understand-this-is-disposable
  ```
  With all captures present it also runs the full integration plan
  (`sql/framework/INTEGRATION_TEST_PLAN.md`); otherwise it reports
  `FRAMEWORK DEFINITION NOT AVAILABLE`.
* Backend tests: `python -m unittest discover -s . -p "test_*.py"`.
* Commit only after human review of the diff.

### 16.5 Install on a new hub database

1. DBA creates roles `mubasher_oms` (NOLOGIN owner), `partition_job_ui`,
   `partition_job_scheduler`.
2. `sql/bootstrap_partition_job_framework.sql` (needs the in-session approval
   TEMP TABLE; refuses on conflicts; tables start empty).
3. Reviewed `sql/framework/captured/*.sql`.
4. `sql/realtime_scheduler_v1.sql` (its dependency gate requires step 3).
5. Migrations in order: `20260929_01`, `20260929_02`, `20260929_03`,
   `20261013_02`, `20261110_01`, `20261110_02`, `20261110_03`.
6. Remaining app grants per the manifest (`table_privileges`, `usage_roles`,
   `execute_roles` on the legacy functions).

### 16.6 Verify (read-only)

```bash
python scripts/preflight_framework.py --env-file .env.realtime \
    --expect-database <db> --scope scheduler --only-problems   # as the scheduler role
python scripts/preflight_framework.py --env-file .env \
    --expect-database <db> --scope api --only-problems         # as the UI role
```

Exit 0 = PASS (warnings allowed), 2 = FAIL. Then start the scheduler: its
startup gate repeats the scheduler-scope check and **dispatches nothing**
while any check FAILs (`framework_status` on
`/internal/scheduler/status`, journal `event=framework_check_failed`, metric
`partops.scheduler.framework_ready`). The System page shows the API-scope
result ("Database framework (manifest)").

### 16.7 Rollback

* Framework version record: `DELETE FROM mubasher_oms.partops_framework_version
  WHERE migration_id = '20261110_03';` — the Phase 5B scheduler will then
  refuse to dispatch (by design) until the version is restored or older Python
  is redeployed.
* Captured legacy functions: they already exist on existing hubs and are not
  replaced by PartOps; on a new hub, drop only what step 3 created.
* Phase 5A/4 bodies: see §10.5 / §10.6.

## 17. Database targets (Phase 5C)

Architecture: ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.

### 17.1 Inspect (read-only)

```bash
curl -s http://127.0.0.1:8501/api/targets          # registry, no secrets
curl -s http://127.0.0.1:8501/api/targets/1/health # health of ONE target
```

Health states: `HEALTHY`, `HEALTHY_WITH_WARNINGS`, `DISABLED` (not contacted),
`CONFIG_ERROR`, `UNREACHABLE`, `AUTH_FAILED`, `WRONG_DATABASE` (connected
database ≠ registry `database_name`), `FRAMEWORK_INCOMPATIBLE`. Each target is
checked separately; one failing target never changes another's state. System
readiness has a "Database targets" category with one item per target.

### 17.2 Register a second target (DBA only; no API write endpoint)

The API has no authentication, so registry writes are DBA-only SQL as the
schema owner. Example (identifier only — never a password or secret JSON):

```sql
INSERT INTO mubasher_oms.partops_database_target
  (target_key, display_name, host, port, database_name,
   credential_provider, secret_id, aws_region, ssl_mode, environment, is_enabled)
VALUES ('db02', 'DB-02', 'db02.hub.internal', 5444, 'oms_db02',
        'aws', 'partops/<hub>/db02', 'eu-west-1', 'require', 'production', false);
```

* Phase 5D executes jobs on enabled managed targets (§10.8). Keep a new
  target **disabled** until its health check is `HEALTHY` /
  `HEALTHY_WITH_WARNINGS` and credentials are deployed to scheduler and API.
* `credential_provider`: `inherit` (control target only — process creds),
  `aws` (the target's own secret, fail closed, never `DB_PASSWORD`),
  `env` (local development only: `PARTOPS_TARGET_<KEY>_DB_USER` /
  `PARTOPS_TARGET_<KEY>_DB_PASSWORD`).
* `max_concurrent_jobs` defaults to 1 (range 1..10).
* A target with jobs cannot be deleted (FK RESTRICT). Disable it instead.

### 17.3 Troubleshooting

| Symptom | Meaning / action |
|---|---|
| Scheduler `framework_status: blocked`, failure "control target lookup failed" | Control row missing, disabled, duplicated or naming another DB. Fix the registry (DBA), then wait for the 300 s recheck or restart. |
| Target `WRONG_DATABASE` | Host/port reach a different database than `database_name`. Fix the registry row; PartOps never runs anything there. |
| Target `AUTH_FAILED` | That target's secret is wrong/rotated. Only that target's cached secret was invalidated and retried once; others unaffected. |
| Job status `SKIPPED_REMOTE_TARGET` / `MANUAL_REMOTE_TARGET` | A local executor was asked to run a db02+ job; nothing ran (Phase 5D routes these jobs to the remote orchestrator). |
| Attention `REMOTE_OUTCOME_UNKNOWN` / attempt `OUTCOME_UNKNOWN` | Target commit could not be confirmed. Inspect the table on that target, then `POST /api/execution-attempts/{id}/resolve` (APPLIED / NOT_APPLIED + note; DROP: type `schema.table`). Never re-run first. |
| Status `REMOTE_FINALIZE_PENDING` / `finalization_pending > 0` | Target committed; control DB write is being retried. No action unless it persists (check control DB). |
| Target `available: false` in scheduler status | Target in connection cooldown (15 s → 300 s). Other targets unaffected. |
| HTTP 409 "remote execution attempt in state …" on edit | The job has an in-flight or unresolved remote attempt; wait / resolve first. Disable is always allowed. |

## 18. Multi-target operations UI (Phase 5E)

One PartOps UI per hub: it shows only this hub's control database and managed
targets. There is no hub selector or fleet view.

### 18.1 Where things are

| Need | Where |
|---|---|
| Target list, health, framework state, concurrency, backoff | **Database targets** (`/targets`) — read-only; click a row for detail, "Refresh health" re-checks each target once |
| Which database a job runs on | Jobs table **Database** column (+ "All targets" filter, search by key); job drawer **Database target** section |
| Choose / change a job's database | Create job / Edit configuration → **Database target**. Only enabled targets that pass health are selectable; a change needs its own explicit confirmation |
| Run a db02+ job now | Job drawer → Run now (goes through the scheduler). DROP keeps its confirmation |
| Unknown outcomes, open attempts | **History** → red "outcome unknown" banner and "Remote execution attempts"; also Jobs → Needs review |
| Hub health | **System** → hub status (OPERATIONAL / DEGRADED / DOWN), targets, per-target scheduler concurrency |

### 18.2 What the UI will never do

* Add / edit / delete targets, credentials, hosts or concurrency limits (DBA
  procedure, §17). The UI has no authentication.
* Retry, force or "ignore and run" an attempt whose outcome is unknown.
* Guess an unknown outcome. Resolution is the operator's finding:
  "applied" or "not applied" + note (+ typed `schema.table` for DROP).

### 18.3 Resolving an unknown outcome

1. Inspect the table on the named target (partitions present / absent).
2. History → open the attempt (or Jobs → the job → Needs review).
3. "Record inspection result…" → choose what you found (no default), tick the
   matching verification sentence, write the note, type the table for DROP,
   "Record resolution". Nothing is executed; scheduling resumes.

### 18.4 Message guide

| Message | Meaning |
|---|---|
| Manual run could not start because the scheduler is unavailable | HTTP 503: nothing executed; start the scheduler |
| No answer from the scheduler in time | HTTP 504: the run may still be in progress — check History before anything else |
| Outcome unknown — do not run again | HTTP 502 for a remote run whose commit could not be confirmed |
| Blocked by an active or unresolved execution | HTTP 409 from the job guard; wait or resolve first |
| Dangerous change not confirmed | Target / table / operation change without its confirmation |
| Temporarily backing off after connection failure | Target cooldown (15 s → 300 s); other targets unaffected |

### 18.5 UI verification matrix — executed (Phase 5E.1, 2026-11-18)

Executed end-to-end against the REAL local stack on disposable data only:
Next.js production build (127.0.0.1:8501) → FastAPI (8001) → realtime
scheduler (8765) → throwaway PostgreSQL 16 cluster (scratchpad initdb,
127.0.0.1:55450) with `partops_control` (control DB / db01), `partops_db02`
(managed target) and `partops_db03` (managed target without workers →
framework incompatible). Worker / cron / insert functions were TEST-ONLY
stubs (they validate PartOps orchestration only, never production worker
logic). The browser was the locally installed Chrome driven headless through
the DevTools protocol (network requests captured). No production, corporate
or shared database was touched.

| # | Scenario | Result | Notes |
|---|---|---|---|
| 1 | db01 healthy | PASS | "Healthy with warnings" (uncaptured legacy definitions; Phase 5B.1 deferred) |
| 2 | db02 healthy | PASS | Healthy with warnings |
| 3 | db02 unreachable | PASS | registry port changed on the disposable control DB; db01 stayed healthy |
| 4 | db02 disabled | PASS | Disabled badge; healthy again after re-enable |
| 5 | db02 framework incompatible (db03) | PASS | detail separates 2 blocking problems from warnings; option disabled; API refuses with 409 |
| 6 | create db01 job | PASS | POST body `database_target_id=1` (default = control) |
| 7 | create db02 job | PASS | POST body `database_target_id=2`; registry row target 2 |
| 8 | filter jobs by target | PASS | DB02 filter shows only db02 jobs |
| 9 | dangerous DB target change | PASS | separate confirmation gates Save; PATCH carries `confirm_dangerous=true`; API rejects an unconfirmed replay with 400 |
| 10 | target change blocked by open attempt | PASS | API 409; UI shows the exact guard message |
| 11 | remote manual run succeeds | PASS | one POST; stub call only in db02; attempt FINALIZED / MANUAL_SUCCESS; "Running…" shown, button disabled while running |
| 12 | scheduler unavailable | PASS | HTTP 503, exact wording, no attempt row, no worker call; db01 manual runs still work (local path) |
| 13 | missed occurrence | PASS | historical occurrence reconciled by the real scheduler; 0 worker calls |
| 14 | retry exhausted | PASS | 4 bounded attempts (FAIL history) → RETRY_EXHAUSTED review with attempt link |
| 15 | OUTCOME_UNKNOWN scenario | PASS | produced through the ledger's own SQL transitions (claim → dispatching → unknown) |
| 16 | Overview warning | PASS | red "remote outcomes unknown" banner |
| 17 | Jobs warning | PASS | red banner + OUTCOME UNKNOWN row badges ("since …") |
| 18 | History warning | PASS | red banner; unknown attempts pinned first |
| 19 | no retry button | PASS (after fix) | no retry / run-anyway / force control anywhere; review actions disabled; **Run now was enabled → fixed (F4)** |
| 20 | resolve APPLIED | PASS | no default; choice + verification + note required; one POST to `/execution-attempts/{id}/resolve`; RESOLVED_APPLIED; no worker call |
| 21 | resolve NOT_APPLIED | PASS | RESOLVED_NOT_APPLIED; no worker call |
| 22 | DROP resolution | PASS | button disabled until the exact `schema.table` is typed (wrong text rejected); `confirm_target` sent |
| 23 | history target filter | PASS | |
| 24 | execution-attempt detail | PASS | human label + exact technical state + fields |
| 25 | finalized-attempt toggle | PASS | open=0 → with finalized=5 |
| 26 | manual result wording | PASS | "Last scheduled status" vs "Latest manual run"; no cross-job leakage (job without manual runs shows "None in recent history") |
| 27 | light mode | PASS | screenshots reviewed (targets, jobs, history, system, overview) |
| 28 | dark mode | PASS | same pages legible |
| 7a | 504 (scheduler accepted, never answered) | PASS (after fix) | "No answer from the scheduler in time"; Run now locked with a link to History (F5) |
| 7b | connection dropped after the request was sent | PASS (after fix) | was 503 "nothing was executed" → now ambiguous 504 (F6) |
| 10a | db02 unreachable → hub DEGRADED | PASS (after fix) | headline now lists every reason (F2) |
| 10b | control DB unavailable → DOWN | PASS (after fix) | readiness took 47 s and the UI showed "Internal Server Error" → dependent checks short-circuited, 21 s (F10) |
| a11y | keyboard smoke | PASS (after fixes) | job rows focusable (F11), focus restored to the row after closing a sheet (F11b), labelled form controls (F3) |
| poll | request audit | PASS | 65 s idle: scheduler status ×2–3, readiness ×2–3, targets ×≤1, health only on demand; no duplicates |
| state | refresh stability | PASS | open sheet, resolution note, typed table, edit-form values and target selection survive two refresh cycles; no stray loaders |
| redact | error redaction | PASS | no password / DSN / secret id / stack trace in UI or health API (`target db02: ConnectionTimeout`) |
