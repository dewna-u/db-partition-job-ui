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

6. `20261013_01_scheduled_target_advisory_lock.sql`
   Phase 2: `CREATE OR REPLACE` of the same
   `run_partition_job_scheduled(numeric, timestamp without time zone)`.
   Adds target-table transaction advisory lock (namespace `87421032`) after
   job-id lock / row validation. Same `(table_schema, table_name)` cannot run
   CREATE/DROP concurrently (`SKIPPED_TARGET_LOCKED`, non-blocking).
   `CREATE OR REPLACE` preserves function owner and existing `EXECUTE` grants
   (no `DROP FUNCTION`). Body must match `../realtime_scheduler_v1.sql`.
   Deploy with scheduler Python concurrency changes
   (`PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS`, default `1`).

7. `20261013_02_secure_update_partition_job.sql`
   Phase 4: `CREATE OR REPLACE` of
   `update_data_to_partition_job_table(...)` as **SECURITY DEFINER**,
   `OWNER TO mubasher_oms`, fixed `search_path`, `REVOKE` PUBLIC,
   `GRANT EXECUTE` to `partition_job_ui` only. Row `FOR UPDATE`. Disable
   preserves `next_run_time` / history columns. Re-enable and schedule edits
   recalculate one future `next_run_time` via `cron_to_interval_or_next_run`
   (fail closed). Does **not** grant table `UPDATE` to `partition_job_ui`.

8. `20261110_01_partition_job_attention.sql`
   Phase 5A (part 1): new table `mubasher_oms.partition_job_attention`
   (missed / not-executed scheduled occurrences + DBA review outcome),
   evidence-guard trigger (immutable evidence, single resolution,
   MANUALLY_RERUN only with a recorded MANUAL_SUCCESS),
   `partition_job_max_execution_lateness()` (DB safety ceiling, default
   6 hours — not the primary missed-run rule) and
   `partition_job_config_param_allowed(text, text)` (db_config_para
   allowlist). Owner `mubasher_oms`, PUBLIC revoked; scheduler gets
   SELECT/INSERT on attention + read-only SELECT on the log table (failed-jobs
   gauge); UI gets SELECT + column UPDATE of review columns only. Idempotent.

9. `20261110_02_missed_run_reconcile_retry_and_manual_lock.sql`
   Phase 5A (part 2), `CREATE OR REPLACE` only (owners/grants preserved):
   - new `reconcile_missed_partition_job(numeric, timestamp, text, text)`
     (records attention row, advances `next_run_time` to the first future
     occurrence, never runs CREATE/DROP, never touches `last_run_*`);
   - `run_partition_job_scheduled`: safety ceiling — never executes an
     occurrence later than `partition_job_max_execution_lateness()` (reconciled
     as missed instead); db_config_para allowlist. The scheduler reconciles
     historical (not owned) occurrences directly via
     `reconcile_missed_partition_job`;
   - `run_partition_job_manual`: shared target-table lock `87421032`
     (`MANUAL_TARGET_LOCKED`), allowlist;
   - `update_data_to_partition_job_table`: allowlist (still SECURITY DEFINER);
   - new `run_partition_job_attention_recovery(bigint)` and
     `acknowledge_partition_job_attention(bigint, text)` (SECURITY INVOKER).
   Body of `run_partition_job_scheduled` / reconcile must match
   `../realtime_scheduler_v1.sql` (unit-tested).
   Apply (8) then (9), then deploy the Phase 5A Python + UI. Keep the
   scheduler STOPPED between the SQL and Python deploy.

10. `20261110_03_partops_framework_version.sql`
   Phase 5B: `mubasher_oms.partops_framework_version` (compatibility record;
   read-only for app roles). Verifies the Phase 5A objects exist, then records
   framework version `20261110_03`. The Phase 5B scheduler refuses to dispatch
   until the database reports this version (and the rest of the manifest gate
   passes). Never written by the application.

Framework model, capture workflow and full install order:
[`../framework/README.md`](../framework/README.md). Read-only DBA preflight:
`python scripts/preflight_framework.py --expect-database <db> --env-file <env>`.

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

11. `20261117_01_partops_database_target.sql` (Phase 5C)
   - Target registry table (no credential storage), default control target
     `db01` = `current_database()`, `partops_control_target_id()`.
   - `partitioning_job_table.database_target_id`: add → backfill → orphan
     check → DEFAULT → NOT NULL → FK (RESTRICT) → index. Idempotent.
12. `20261117_02_target_aware_executors.sql` (Phase 5C)
   - Re-creates `get_upcoming_partition_jobs`, `run_partition_job_scheduled`,
     `reconcile_missed_partition_job`, `run_partition_job_manual` with a
     control-target guard (`SKIPPED_REMOTE_TARGET` / `MANUAL_REMOTE_TARGET`);
     all Phase 5A rules unchanged. Records framework version `20261117_02`.
   - Legacy worker bodies are NOT defined or modified.
   - Rollback: re-apply `20261110_02` (restores the pre-5C bodies).

13. `20261117_03_remote_execution_ledger.sql` (Phase 5D)
   - `partops_execution_attempt` ledger (+ state-transition guard), attention
     reason `REMOTE_OUTCOME_UNKNOWN` + target/attempt evidence, history
     columns `database_target_id` / `execution_attempt_id` (no backfill),
     job configuration guard (55006), new-job target hook. App roles: SELECT.
14. `20261117_04_remote_execution_functions.sql` (Phase 5D)
   - `partops_get_upcoming_jobs`, `partops_remote_*` (claim, dispatch,
     success, failure, unknown, finalize, recover, close),
     `partops_resolve_remote_outcome`, `partops_set_job_target` (SECURITY
     DEFINER, pinned search_path, PUBLIC revoked, narrow grants); target-aware
     `reconcile_missed_partition_job`. Records framework version `20261117_04`.
   - Legacy worker / cron / insert bodies are NOT defined or modified.
   - Rollback: RUNBOOK §10.8.

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
