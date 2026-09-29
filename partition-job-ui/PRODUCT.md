# PartOps

## Product
**PartOps** — GTN EDB Partition Operations Platform. PostgreSQL / EDB partition-job operations console for DBAs. Configure, monitor, and safely manage parameterized partition jobs backed by `partitioning_job_table`, execution logs, and a dedicated realtime scheduler process.

## Users
Database administrators and operators who manage CREATE/DROP partition schedules on `mubasher_oms` (and similar) databases.

## Mode
Operate — scanability, status clarity, and safe write actions outrank marketing expression.

## Platform
Web (Next.js frontend on :8501 + FastAPI API on 127.0.0.1:8001). Existing Python DB layer and `scheduler_backend/` remain the system of record for writes and scheduling. Realtime scheduler control API on 127.0.0.1:8765.

## Non-goals
- Do not rewrite SQL functions, partition CREATE/DROP execution, cron advancement, or scheduler queue/timer logic.
- Do not replace Streamlit by inventing a second execution path.
- Do not add fake metrics or AI insights.
- Do not use port 8000 for PartOps API (reserved elsewhere).

## Assumptions (from explicit migration brief)
- Visual direction: PartOps ops console (cream light / charcoal dark content, charcoal sidebar, blue primary, lime accent).
- Navigation: left sidebar only; no top horizontal page tabs.
- Routes: `/`, `/convert`, `/jobs/new`, `/jobs`, `/history`, `/system`.
