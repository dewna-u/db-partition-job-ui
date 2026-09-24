# Partition Manager

## Product
PostgreSQL / EDB partition-job operations console for DBAs. Configure, monitor, and safely manage parameterized partition jobs backed by `partitioning_job_table`, execution logs, and a dedicated realtime scheduler process.

## Users
Database administrators and operators who manage CREATE/DROP partition schedules on `mubasher_oms` (and similar) databases.

## Mode
Operate — scanability, status clarity, and safe write actions outrank marketing expression.

## Platform
Web (Next.js frontend + FastAPI API). Existing Python DB layer and `scheduler_backend/` remain the system of record for writes and scheduling.

## Non-goals
- Do not rewrite SQL functions, partition CREATE/DROP execution, cron advancement, or scheduler queue/timer logic.
- Do not replace Streamlit by inventing a second execution path.
- Do not add fake metrics or AI insights.

## Assumptions (from explicit migration brief)
- Visual direction: partition.ops / aesthetix-ashen commercial ops console (cream surface, charcoal sidebar, restrained blue primary, lime accent).
- Navigation: left sidebar only; no top horizontal page tabs.
- Routes: `/`, `/convert`, `/jobs/new`, `/jobs`, `/history`.
