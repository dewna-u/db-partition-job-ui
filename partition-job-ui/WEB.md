# Modern web UI (Next.js) — Streamlit archived

Live stack: **Next.js + FastAPI**. Streamlit UI is in `archives/streamlit-ui-*.zip`.

## Deploy path (this host)

```text
/opt/db-partition-job-ui-github/partition-job-ui
```

Do **not** use `/opt/partition-job-ui` in systemd units on this server.

## Fix / install services (run as root)

```bash
cd /opt/db-partition-job-ui-github/partition-job-ui
bash scripts/fix-systemd-web-ui.sh
```

That script:

1. Installs FastAPI/uvicorn into `.venv`
2. Fixes `frontend/` ownership (`partitionui` must own `.next` + `node_modules`)
3. Writes correct units to `/usr/lib/systemd/system/`
4. Removes stale `/etc/systemd/system/` overrides
5. Restarts and health-checks

Units:

| Unit | Port | Role |
|---|---|---|
| `partition-job-api.service` | `127.0.0.1:8000` | FastAPI |
| `partition-job-ui.service` | `0.0.0.0:8501` | Next.js (proxies `/api/*`) |

## Why it failed before

| Code | Real cause |
|---|---|
| `203/EXEC` | ExecStart used `/opt/partition-job-ui/.venv/bin/uvicorn` — wrong tree + no uvicorn binary |
| `200/CHDIR` | WorkingDirectory was `/opt/partition-job-ui/frontend` — that path does not exist |

## Local development

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

cd frontend && npm install && npm run dev
```
