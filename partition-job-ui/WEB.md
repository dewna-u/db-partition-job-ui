# Modern web stack (Streamlit remains available during migration)

## Architecture

```
frontend/   Next.js + React + TypeScript + Tailwind  (presentation)
api/        FastAPI                                   (HTTP facade)
database.py / validators.py / job_autofill.py         (unchanged)
scheduler_backend/                                    (unchanged realtime process)
```

Navigation is **sidebar-only** (no top page tabs).

Routes:
- `/` Overview
- `/convert` Convert existing job
- `/jobs/new` Create new job
- `/jobs` Configured jobs
- `/history` Execution history

## Run API

From `partition-job-ui/` (so imports resolve to existing modules):

```bash
pip install -r api/requirements.txt
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

Uses the same `.env` as Streamlit for DB and scheduler URLs.

## Run frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000

Optional: set `NEXT_PUBLIC_API_BASE_URL` (defaults to `http://127.0.0.1:8000`).

## What did not change

- PostgreSQL SQL functions and tables
- `create_partition_job` / `run_partition_job_manual`
- `scheduler_backend` queue/timer execution
- Short-lived DB connections
- Validation and job_autofill detection logic
