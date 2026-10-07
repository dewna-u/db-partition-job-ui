# Database migrations (PREPARE ONLY)

These SQL files are **not** applied automatically by the application.

Apply in a controlled DBA window, in order:

1. `20260929_01_add_execution_duration_ms.sql`  
   Adds nullable `mubasher_oms.partitioning_job_table_log.execution_duration_ms BIGINT`.  
   Historical rows stay NULL. Do not backfill.

2. `20260929_02_update_data_to_partition_job_table.sql`  
   Config-edit function. Does not touch `job_id`, `last_run_time`, `last_run_status`, or logs.

3. `20260929_03_run_partition_job_manual_duration.sql`  
   Replaces `mubasher_oms.run_partition_job_manual(numeric)` (DROP without CASCADE,
   then CREATE) so duration is stored **atomically in the same MANUAL_SUCCESS /
   MANUAL_FAIL log INSERT**. Worker failures **return** `status = MANUAL_FAIL`
   instead of `RAISE`, so the calling transaction can COMMIT the failure log.
   Return contract: `TABLE(status text, message text, execution_duration_ms bigint)`.
   Restore `OWNER TO mubasher_oms` after CREATE. Re-GRANT `EXECUTE` to
   `partition_job_ui` after DROP (not GRANT ALL). There is no
   `stamp_latest_job_log_duration` helper.

4. Deploy **both** realtime scheduler functions from `../realtime_scheduler_v1.sql`:
   - `mubasher_oms.get_upcoming_partition_jobs(interval)`
   - `mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone)`

   See `20260929_04_run_partition_job_scheduled_duration.md.sql`.  
   This is not “duration-only”: the live database may not have either function yet.

5. `20261007_01_run_partition_job_scheduled_occurrence_safety.sql`
   Phase 0 safety (hardened): `CREATE OR REPLACE` of
   `run_partition_job_scheduled(numeric, timestamp without time zone)`.
   Validates a future next occurrence **before** CREATE/DROP. Invalid helper
   results **fail closed** (`FAILED_INVALID_SCHEDULE`, `next_run_time = NULL`,
   no worker). Does **not** invent `+1 day` or frequency fallbacks.
   Body must match `../realtime_scheduler_v1.sql`. Also deploy updated
   `scheduler_backend` Python (bounded TTL circuit breaker) before restarting
   the scheduler.

Until (1) is applied, the API still works: job/log queries fall back to
legacy SELECTs without duration columns, and duration UI shows "—".

Until (2) is applied, `PATCH /api/jobs/{id}` will fail with a missing-function
error — do not enable Edit in production until this function exists and the
app role has `EXECUTE`.

Until (3) is applied, manual runs still succeed but new duration values will
not be stored on MANUAL_SUCCESS / MANUAL_FAIL rows, and a worker `RAISE` can
still roll back a MANUAL_FAIL insert.

After (3), `SELECT status, message, execution_duration_ms FROM
mubasher_oms.run_partition_job_manual(job_id)` returns the outcome. The
application COMMITs first, then maps `MANUAL_FAIL` to an HTTP error. Missing
jobs still `RAISE` (no attempt, no log). The function does not change
`next_run_time`, `last_run_time`, or `last_run_status`.

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
