# Modern web UI (Next.js) — Streamlit archived

Streamlit presentation files were moved to `archives/streamlit-ui-*.zip`.
The live UI is Next.js + FastAPI.

## Architecture

```
Browser
  -> Next.js (port 8501)          frontend/
       /api/* rewrites
  -> FastAPI (127.0.0.1:8000)     api/
  -> database.py / validators / job_autofill
  -> scheduler_backend (unchanged)
```

Routes:
- `/` Overview
- `/convert` Convert existing job
- `/jobs/new` Create new job
- `/jobs` Configured jobs
- `/history` Execution history

Sidebar-only navigation (no top page tabs).

## Local development

```bash
# API
pip install -r requirements.txt
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

Open http://localhost:3000  
Dev can leave `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000` in `.env.local`,
or rely on Next rewrites (empty base URL) after `next build`.

## Production (systemd)

```bash
cd /opt/partition-job-ui   # or your deploy path
git pull
.venv/bin/pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..

sudo cp systemd/partition-job-api.service /etc/systemd/system/
sudo cp partition-job-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now partition-job-api.service
sudo systemctl restart partition-job-ui.service
sudo systemctl status partition-job-api.service partition-job-ui.service --no-pager -l
```

UI stays on **port 8501**. API listens on localhost **8000** only.

## Restore Streamlit (optional)

See `archives/README.md`. Do not unzip over the live tree unless intentional.
