# Database migrations (PREPARE ONLY)

These SQL files are **not** applied automatically by the application.

Apply in a controlled DBA window, in order:

1. `20260929_01_add_execution_duration_ms.sql`  
   Adds nullable `mubasher_oms.partitioning_job_table_log.execution_duration_ms BIGINT`.  
   Historical rows stay NULL. Do not backfill.

2. `20260929_02_update_data_to_partition_job_table.sql`  
   Config-edit function. Does not touch `job_id`, `last_run_time`, `last_run_status`, or logs.

3. `20260929_03_run_partition_job_manual_duration.sql`  
   CREATE OR REPLACE `mubasher_oms.run_partition_job_manual(numeric)` so duration is stored
   **atomically in the same MANUAL_SUCCESS / MANUAL_FAIL log INSERT**.  
   There is no `stamp_latest_job_log_duration` helper.

4. Deploy **both** realtime scheduler functions from `../realtime_scheduler_v1.sql`:
   - `mubasher_oms.get_upcoming_partition_jobs(interval)`
   - `mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone)`

   See `20260929_04_run_partition_job_scheduled_duration.md.sql`.  
   This is not “duration-only”: the live database may not have either function yet.

Until (1) is applied, the API still works: job/log queries fall back to
legacy SELECTs without duration columns, and duration UI shows "—".

Until (2) is applied, `PATCH /api/jobs/{id}` will fail with a missing-function
error — do not enable Edit in production until this function exists and the
app role has `EXECUTE`.

Until (3) is applied, manual runs still succeed but new duration values will
not be stored on MANUAL_SUCCESS / MANUAL_FAIL rows.

## Before starting the realtime scheduler

Both must return a non-NULL procedure identity:

```sql
SELECT to_regprocedure(
    'mubasher_oms.get_upcoming_partition_jobs(interval)'
);

SELECT to_regprocedure(
    'mubasher_oms.run_partition_job_scheduled(numeric,timestamp without time zone)'
);
```

Then a safe read-only smoke (does **not** execute partition jobs):

```sql
SELECT *
FROM mubasher_oms.get_upcoming_partition_jobs(interval '120 seconds');
```

Do **not** call `run_partition_job_scheduled(...)` merely as a health test.
