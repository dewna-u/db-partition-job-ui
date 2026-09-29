# Database migrations (PREPARE ONLY)

These SQL files are **not** applied automatically by the application.

Apply in a controlled DBA window, in order:

1. `20260929_01_add_execution_duration_ms.sql`
2. `20260929_02_update_data_to_partition_job_table.sql`
3. `20260929_03_stamp_latest_job_log_duration.sql`
4. Redeploy `run_partition_job_scheduled` from `../realtime_scheduler_v1.sql`
   (see `20260929_04_run_partition_job_scheduled_duration.md.sql`)

Until (1) is applied, the API still works: job/log queries fall back to
legacy SELECTs without duration columns, and duration UI shows "—".

Until (2) is applied, `PATCH /api/jobs/{id}` will fail with a missing-function
error — do not enable Edit in production until this function exists and the
app role has `EXECUTE`.

Until (3) is applied, manual runs still succeed; duration simply will not be
stamped for manual history rows.
