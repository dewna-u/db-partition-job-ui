# Local laptop PartOps database (`partops_dev`)

**LOCALHOST ONLY.** Never point this at `172.20.224.68`, `ukbodev`, or any company DB.

## Goal

Create an empty, synthetic PartOps framework DB for continuous local development:

| Item | Value |
|---|---|
| Host | `127.0.0.1` |
| Port | `5432` |
| Database | `partops_dev` |
| App role | `partops_local` (non-superuser) |

## Dependency order (version-controlled)

| Step | Source | Status in repo |
|---|---|---|
| 1 | `sql/bootstrap_partition_job_framework.sql` | Schema, sequences, job/log tables, `cron_schedule_result`, trigger |
| 2 | `sql/migrations/20260929_01_add_execution_duration_ms.sql` | `execution_duration_ms` column |
| 3 | **Import / local-dev stubs** for missing framework functions | **NOT in Git** (see blockers) |
| 4 | `sql/migrations/20260929_02_update_data_to_partition_job_table.sql` | Update helper |
| 5 | `sql/migrations/20260929_03_run_partition_job_manual_duration.sql` | Manual run |
| 6 | `sql/realtime_scheduler_v1.sql` (Phase 0 hardened body) | Upcoming + scheduled run |
| 7 | Synthetic partitioned table + DEV_* jobs | Local-only seed |

## Missing from Git (cannot invent)

These are required by the scheduler/API but have **no CREATE FUNCTION body** in this repository:

1. `mubasher_oms.cron_to_interval_or_next_run(text)` → `cron_schedule_result`
2. `mubasher_oms.cron_field_to_int_array(...)` (helper used by cron, if present in reference)
3. `mubasher_oms.create_any_table_partition(varchar,varchar,varchar,integer,interval)`
4. `mubasher_oms.drop_any_table_partition(varchar,varchar,interval)`
5. `mubasher_oms.insert_data_to_partition_job_table(...)` (API create-job path)

Live Phase 0 inspection showed the cron helper can return a correct next Friday for
`0 15 0 * * 5`, but the **function source body was never checked into Git**.
Do **not** treat any future local stub as a production migration source.

## Bootstrap script

```powershell
# Set admin password for local PostgreSQL only (session env — not committed):
$env:PARTOPS_LOCAL_PG_ADMIN_PASSWORD = '<local postgres password>'
$env:PARTOPS_LOCAL_PG_ADMIN_USER = 'postgres'   # optional default

.\scripts\bootstrap-local-partops-dev.ps1
```

The script refuses non-localhost hosts and never prints the password.

## Env files (gitignored)

After a successful bootstrap the script writes (or refreshes) gitignored:

- `.env` — API → `partops_dev` @ `127.0.0.1`
- `.env.realtime` — scheduler → same + `REALTIME_DB_EXPECTED_NAME=partops_dev`
