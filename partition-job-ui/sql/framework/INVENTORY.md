# PartOps database object inventory

<!-- GENERATED from framework_manifest.json by scripts/framework_inventory.py — do not edit by hand. -->

Expected framework version: `20261117_04`. Required roles: `mubasher_oms`, `partition_job_ui`, `partition_job_scheduler`.

Status legend: `versioned` = definition in Git; `existing` = pre-existing object; `capture_required` = **authoritative definition NOT in Git** (capture from the approved reference DB; never invent); `legacy_must_not_run` = legacy runner PartOps never calls and that must not be scheduled alongside the realtime scheduler; `external_optional` = pgAgent.

`in_git`: `yes` = authoritative definition in Git; `reconstructed` = table/type shape recreated by `bootstrap_partition_job_framework.sql` from the project specification (verify against the reference capture); `partial` / `no` as stated.

## PARTOPS-OWNED OBJECTS (definition versioned in this repository)

| kind | object | signature | status | in_git | required_by | required | security | owner | grants | called by | source |
|---|---|---|---|---|---|---|---|---|---|---|---|
| function | `mubasher_oms.run_partition_job_manual` | (numeric) | versioned | yes | api, manual_run | yes | invoker | mubasher_oms | EXECUTE→partition_job_ui | database.run_partition_job_manual (POST /api/jobs/{id}/run), run_partition_job_attention_recovery | sql/migrations/20260929_03_run_partition_job_manual_duration.sql, sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql, sql/migrations/20261117_02_target_aware_executors.sql |
| function | `mubasher_oms.update_data_to_partition_job_table` | (numeric, character varying, boolean, character varying, character varying, jsonb, character varying, interval, timestamp without time zone, character varying, numeric, boolean, interval) | versioned | yes | api | yes | definer | mubasher_oms | EXECUTE→partition_job_ui | database.update_partition_job (PATCH /api/jobs/{id}) | sql/migrations/20261013_02_secure_update_partition_job.sql, sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql |
| function | `mubasher_oms.get_upcoming_partition_jobs` | (interval) | versioned | yes | scheduler | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler_database.fetch_upcoming_jobs | sql/realtime_scheduler_v1.sql, sql/migrations/20261117_02_target_aware_executors.sql |
| function | `mubasher_oms.run_partition_job_scheduled` | (numeric, timestamp without time zone) | versioned | yes | scheduler | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler_database.execute_scheduled_job | sql/realtime_scheduler_v1.sql, sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql, sql/migrations/20261117_02_target_aware_executors.sql |
| function | `mubasher_oms.reconcile_missed_partition_job` | (numeric, timestamp without time zone, text, text) | versioned | yes | scheduler | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler_database.reconcile_occurrence, run_partition_job_scheduled (ceiling), remote claim (lateness ceiling) | sql/realtime_scheduler_v1.sql, sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql, sql/migrations/20261117_02_target_aware_executors.sql, sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.run_partition_job_attention_recovery` | (bigint) | versioned | yes | api, manual_run | yes | invoker | mubasher_oms | EXECUTE→partition_job_ui | database.recover_partition_job_attention (POST /api/attention/{id}/recover) | sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql |
| function | `mubasher_oms.acknowledge_partition_job_attention` | (bigint, text) | versioned | yes | api | yes | invoker | mubasher_oms | EXECUTE→partition_job_ui | database.acknowledge_partition_job_attention (POST /api/attention/{id}/acknowledge) | sql/migrations/20261110_02_missed_run_reconcile_retry_and_manual_lock.sql |
| function | `mubasher_oms.partition_job_max_execution_lateness` | () | versioned | yes | scheduler | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler | run_partition_job_scheduled | sql/migrations/20261110_01_partition_job_attention.sql |
| function | `mubasher_oms.partition_job_config_param_allowed` | (text, text) | versioned | yes | scheduler, manual_run, api | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler; EXECUTE→partition_job_ui | run_partition_job_scheduled, run_partition_job_manual, update_data_to_partition_job_table | sql/migrations/20261110_01_partition_job_attention.sql |
| function | `mubasher_oms.fn_partition_job_attention_guard` | () | versioned | yes | api, scheduler | yes | invoker | mubasher_oms | — | trg_partition_job_attention_guard | sql/migrations/20261110_01_partition_job_attention.sql, sql/migrations/20261117_03_remote_execution_ledger.sql |
| table | `mubasher_oms.partition_job_attention` | — | versioned | yes | api, scheduler | yes | — | mubasher_oms | SELECT→partition_job_ui; SELECT/INSERT→partition_job_scheduler | reconcile_missed_partition_job, recovery/acknowledge, database.get_partition_job_attention, count_pending_attention | sql/migrations/20261110_01_partition_job_attention.sql, sql/migrations/20261117_03_remote_execution_ledger.sql |
| trigger | `mubasher_oms.trg_partition_job_attention_guard` | — | versioned | yes | api, scheduler | yes | — | — | — | updates of partition_job_attention | sql/migrations/20261110_01_partition_job_attention.sql |
| table | `mubasher_oms.partops_framework_version` | — | versioned | yes | api, scheduler | yes | — | mubasher_oms | SELECT→partition_job_ui; SELECT→partition_job_scheduler | framework preflight, scheduler start gate, system readiness | sql/migrations/20261110_03_partops_framework_version.sql |
| table | `mubasher_oms.partops_database_target` | — | versioned | yes | api, scheduler | yes | — | mubasher_oms | SELECT→partition_job_ui; SELECT→partition_job_scheduler | database_targets, database.list_database_targets, scheduler control-target lookup, partops_control_target_id | sql/migrations/20261117_01_partops_database_target.sql |
| function | `mubasher_oms.partops_control_target_id` | () | versioned | yes | api, scheduler, manual_run | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler; EXECUTE→partition_job_ui | get_upcoming_partition_jobs, run_partition_job_scheduled, reconcile_missed_partition_job, run_partition_job_manual, job inserts (column DEFAULT) | sql/migrations/20261117_01_partops_database_target.sql |
| table | `mubasher_oms.partops_execution_attempt` | — | versioned | yes | api, scheduler | yes | — | mubasher_oms | SELECT→partition_job_ui; SELECT→partition_job_scheduler | partops_remote_* functions, API execution-attempt endpoints, job configuration guard | sql/migrations/20261117_03_remote_execution_ledger.sql |
| function | `mubasher_oms.fn_partops_execution_attempt_guard` | () | versioned | yes | api, scheduler | yes | invoker | mubasher_oms | — | trg_partops_execution_attempt_guard | sql/migrations/20261117_03_remote_execution_ledger.sql |
| trigger | `mubasher_oms.trg_partops_execution_attempt_guard` | — | versioned | yes | api, scheduler | yes | — | — | — | ledger updates | sql/migrations/20261117_03_remote_execution_ledger.sql |
| function | `mubasher_oms.fn_partops_job_execution_guard` | () | versioned | yes | api, scheduler | yes | invoker | mubasher_oms | — | trg_partops_job_execution_guard | sql/migrations/20261117_03_remote_execution_ledger.sql |
| trigger | `mubasher_oms.trg_partops_job_execution_guard` | — | versioned | yes | api, scheduler | yes | — | — | — | job edits / enable | sql/migrations/20261117_03_remote_execution_ledger.sql |
| function | `mubasher_oms.fn_partops_job_target_on_insert` | () | versioned | yes | api | yes | invoker | mubasher_oms | — | trg_partops_job_target_on_insert | sql/migrations/20261117_03_remote_execution_ledger.sql |
| trigger | `mubasher_oms.trg_partops_job_target_on_insert` | — | versioned | yes | api | yes | — | — | — | database.create_partition_job | sql/migrations/20261117_03_remote_execution_ledger.sql |
| function | `mubasher_oms.partops_job_open_attempt_state` | (numeric) | versioned | yes | api, scheduler | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler; EXECUTE→partition_job_ui | job configuration guard, API | sql/migrations/20261117_03_remote_execution_ledger.sql |
| function | `mubasher_oms.partops_compute_next_run` | (character varying, timestamp without time zone, timestamp without time zone) | versioned | yes | scheduler | yes | invoker | mubasher_oms | — | remote claim / finalize / resolution | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_safe_error` | (text) | versioned | yes | scheduler | yes | invoker | mubasher_oms | — | remote ledger functions | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_get_upcoming_jobs` | (interval) | versioned | yes | scheduler | yes | invoker | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler_database.fetch_upcoming_jobs | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_close_attempt` | (bigint, text) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | reconcile_missed_partition_job | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_claim` | (numeric, timestamp without time zone, text, text, bigint, boolean) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler remote orchestrator | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_mark_dispatching` | (bigint, text) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler remote orchestrator | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_record_success` | (bigint, bigint) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler remote orchestrator | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_record_failure` | (bigint, text, text, bigint) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler remote orchestrator | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_mark_unknown` | (bigint, text) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler remote orchestrator, partops_remote_recover_attempts | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_finalize` | (bigint) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler remote orchestrator | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_remote_recover_attempts` | (text) | versioned | yes | scheduler | yes | definer | mubasher_oms | EXECUTE→partition_job_scheduler | scheduler start | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_resolve_remote_outcome` | (bigint, text, text) | versioned | yes | api | yes | definer | mubasher_oms | EXECUTE→partition_job_ui | POST /api/execution-attempts/{id}/resolve | sql/migrations/20261117_04_remote_execution_functions.sql |
| function | `mubasher_oms.partops_set_job_target` | (numeric, integer) | versioned | yes | api | yes | definer | mubasher_oms | EXECUTE→partition_job_ui | PATCH /api/jobs/{id} | sql/migrations/20261117_04_remote_execution_functions.sql |

Purpose:

- `run_partition_job_manual` — Manual Run now: shared target lock, allowlist, MANUAL_* log; never changes next_run_time / last_run_*. Phase 5C: jobs of other targets → MANUAL_REMOTE_TARGET.
- `update_data_to_partition_job_table` — Edit / Enable / Disable (SECURITY DEFINER, fixed search_path, allowlist).
- `get_upcoming_partition_jobs` — Read-only discovery of enabled jobs due within lookahead (includes overdue). Phase 5C: control-database jobs only.
- `run_partition_job_scheduled` — Execute one owned occurrence after DB revalidation; DB max-lateness ceiling. Phase 5C: jobs of other targets → SKIPPED_REMOTE_TARGET.
- `reconcile_missed_partition_job` — Record a not-executed occurrence (attention) and advance next_run_time; no DDL. Phase 5D: every target of the hub; refuses while a remote attempt is in flight / unresolved.
- `run_partition_job_attention_recovery` — DBA recovery of a missed occurrence via run_partition_job_manual.
- `acknowledge_partition_job_attention` — Acknowledge a missed occurrence without executing.
- `partition_job_max_execution_lateness` — DB safety ceiling (default 6 h) used by run_partition_job_scheduled.
- `partition_job_config_param_allowed` — db_config_para allowlist (mirrored in validators.py).
- `fn_partition_job_attention_guard` — Trigger function: immutable evidence (incl. target/attempt), single resolution, MANUALLY_RERUN needs MANUAL_SUCCESS, unknown remote outcomes never re-run.
- `partition_job_attention` — Missed / retry-exhausted occurrences and DBA review outcome.
- `trg_partition_job_attention_guard` — BEFORE UPDATE guard on partition_job_attention.
- `partops_framework_version` — Applied PartOps framework migrations (compatibility check only; never written by the application).
- `partops_database_target` — Hub-local registry of managed databases (control database = default target). No passwords/keys/secret values.
- `partops_control_target_id` — target_id of the control database; DEFAULT of partitioning_job_table.database_target_id; executor remote-target guard.
- `partops_execution_attempt` — Remote execution attempt ledger (control DB): state machine PREPARED → DISPATCHING → TARGET_SUCCEEDED → FINALIZED; TARGET_FAILED (proven rollback); OUTCOME_UNKNOWN (review). No credentials.
- `fn_partops_execution_attempt_guard` — Trigger function: legal attempt state transitions; identity immutable.
- `trg_partops_execution_attempt_guard` — BEFORE UPDATE state-machine guard on the ledger.
- `fn_partops_job_execution_guard` — Trigger function: target/table/operation/schedule/config cannot change while a remote attempt is in flight or unresolved; no re-enable while an outcome is unknown.
- `trg_partops_job_execution_guard` — BEFORE UPDATE job configuration guard (Phase 5D).
- `fn_partops_job_target_on_insert` — Trigger function: a new job may request its target via partops.requested_target_id (validated).
- `trg_partops_job_target_on_insert` — BEFORE INSERT target assignment of new jobs.
- `partops_job_open_attempt_state` — Most significant open remote attempt state of a job.
- `partops_compute_next_run` — Internal: first legitimate future occurrence via the legacy cron helper (no invention).
- `partops_safe_error` — Internal: bounded error text without secret-like tokens.
- `partops_get_upcoming_jobs` — Target-aware discovery of due/upcoming jobs across all enabled targets (read-only).
- `partops_remote_close_attempt` — Close a PREPARED / TARGET_FAILED attempt when its occurrence is reconciled.
- `partops_remote_claim` — Revalidate a remote occurrence / manual run and create or reuse its attempt (no DDL).
- `partops_remote_mark_dispatching` — PREPARED → DISPATCHING before the target worker call.
- `partops_remote_record_success` — DISPATCHING → TARGET_SUCCEEDED (target COMMIT confirmed).
- `partops_remote_record_failure` — PREPARED/DISPATCHING → TARGET_FAILED (proven rollback); manual runs finalized.
- `partops_remote_mark_unknown` — DISPATCHING → OUTCOME_UNKNOWN; REMOTE_OUTCOME_UNKNOWN attention; job scheduling paused.
- `partops_remote_finalize` — TARGET_SUCCEEDED → FINALIZED: history, last status, next legitimate schedule (control DB only).
- `partops_remote_recover_attempts` — Scheduler start: DISPATCHING → OUTCOME_UNKNOWN; TARGET_SUCCEEDED → finalize only; stale attempts closed.
- `partops_resolve_remote_outcome` — Operator resolution of OUTCOME_UNKNOWN (APPLIED / NOT_APPLIED + note); resumes scheduling. No DDL.
- `partops_set_job_target` — Move a job to another enabled target (guarded by the job configuration guard).

## EXISTING / LEGACY DATABASE FRAMEWORK OBJECTS

| kind | object | signature | status | in_git | required_by | required | security | owner | grants | called by | source |
|---|---|---|---|---|---|---|---|---|---|---|---|
| schema | `mubasher_oms.mubasher_oms` | — | existing | partial | api, manual_run, migration, scheduler, target_execution | yes | — | — | — | all | sql/bootstrap_partition_job_framework.sql |
| table | `mubasher_oms.partitioning_job_table` | — | versioned | reconstructed | api, scheduler, manual_run, migration | yes | — | mubasher_oms | SELECT→partition_job_ui; SELECT→partition_job_scheduler | database.py, scheduler functions, update/manual/reconcile functions | sql/bootstrap_partition_job_framework.sql, sql/migrations/20261117_01_partops_database_target.sql, sql/migrations/20261117_03_remote_execution_ledger.sql |
| table | `mubasher_oms.partitioning_job_table_log` | — | versioned | reconstructed | api, scheduler, manual_run | yes | — | mubasher_oms | SELECT/INSERT→partition_job_ui; SELECT/INSERT→partition_job_scheduler | database.py, run_partition_job_scheduled, run_partition_job_manual, count_failed_jobs | sql/bootstrap_partition_job_framework.sql, sql/migrations/20260929_01_add_execution_duration_ms.sql, sql/migrations/20261117_03_remote_execution_ledger.sql |
| sequence | `mubasher_oms.seq_partitioning_job_id` | — | versioned | reconstructed | api | yes | — | — | — | insert_data_to_partition_job_table | sql/bootstrap_partition_job_framework.sql |
| sequence | `mubasher_oms.seq_partitioning_job_log_id` | — | versioned | reconstructed | scheduler, manual_run | yes | — | — | USAGE→partition_job_ui; USAGE→partition_job_scheduler | run_partition_job_scheduled, run_partition_job_manual | sql/bootstrap_partition_job_framework.sql |
| type | `mubasher_oms.cron_schedule_result` | — | versioned | reconstructed | scheduler, api, migration | yes | — | — | — | cron_to_interval_or_next_run, run_partition_job_scheduled, reconcile_missed_partition_job, update_data_to_partition_job_table | sql/bootstrap_partition_job_framework.sql |
| function | `mubasher_oms.fn_set_default_db_config_para` | () | versioned | yes | api | no | invoker | — | — | trg_set_default_db_config_para | sql/bootstrap_partition_job_framework.sql, ../sql/remove_default_lock_timeout_from_db_config.sql |
| trigger | `mubasher_oms.trg_set_default_db_config_para` | — | versioned | yes | api | no | — | — | — | inserts/updates of partitioning_job_table | sql/bootstrap_partition_job_framework.sql |
| function | `mubasher_oms.insert_data_to_partition_job_table` | (character varying, boolean, character varying, character varying, jsonb, character varying, interval, timestamp without time zone, character varying, numeric, boolean, interval) | capture_required | no | api | yes | unknown | — | EXECUTE→partition_job_ui | database.create_partition_job (POST /api/jobs), system_readiness | NOT IN GIT |
| function | `mubasher_oms.cron_to_interval_or_next_run` | (text) | capture_required | no | scheduler, api | yes | unknown | — | EXECUTE→partition_job_scheduler | run_partition_job_scheduled, reconcile_missed_partition_job, update_data_to_partition_job_table (as owner) | NOT IN GIT |
| function | `mubasher_oms.cron_field_to_int_array` | (unknown) | capture_required | no | scheduler | no | unknown | — | — | cron_to_interval_or_next_run (unverified) | NOT IN GIT |
| function | `mubasher_oms.create_any_table_partition` | (character varying, character varying, character varying, integer, interval) | capture_required | no | manual_run, scheduler, target_execution | yes | unknown | — | EXECUTE→partition_job_scheduler; EXECUTE→partition_job_ui | run_partition_job_scheduled, run_partition_job_manual, scheduler remote orchestrator (on the managed TARGET database) | NOT IN GIT |
| function | `mubasher_oms.drop_any_table_partition` | (character varying, character varying, interval) | capture_required | no | manual_run, scheduler, target_execution | yes | unknown | — | EXECUTE→partition_job_scheduler; EXECUTE→partition_job_ui | run_partition_job_scheduled, run_partition_job_manual, scheduler remote orchestrator (on the managed TARGET database) | NOT IN GIT |
| function | `mubasher_oms.run_partition_create_jobs` | () | legacy_must_not_run | no | — | no | unknown | — | — | legacy pgAgent job (detected read-only) | NOT IN GIT |
| function | `mubasher_oms.run_partition_drop_jobs` | () | legacy_must_not_run | no | — | no | unknown | — | — | legacy pgAgent job (detected read-only) | NOT IN GIT |

Purpose:

- `mubasher_oms` — Application schema that hosts the partition framework (existing in production; bootstrap only creates it when absent).
- `partitioning_job_table` — One row per partition job: target table, schedule, next_run_time, last scheduled outcome.
- `partitioning_job_table_log` — Execution history (SUCCESS/FAIL/MANUAL_SUCCESS/MANUAL_FAIL) with execution_duration_ms.
- `seq_partitioning_job_id` — job_id default.
- `seq_partitioning_job_log_id` — job_log_id default (log inserts by scheduler and manual run).
- `cron_schedule_result` — Composite (schedule_interval interval, next_run_time timestamp) returned by cron_to_interval_or_next_run.
- `fn_set_default_db_config_para` — Trigger function: NULL db_config_para -> '{}' (never injects defaults).
- `trg_set_default_db_config_para` — BEFORE INSERT OR UPDATE OF db_config_para normalisation.
- `insert_data_to_partition_job_table` — Create path: inserts one partitioning_job_table row (called with named notation by database.create_partition_job).
- `cron_to_interval_or_next_run` — Six-field cron -> interval or next run timestamp. Authoritative schedule calculation (ISSUE-003).
- `cron_field_to_int_array` — Reported (bootstrap notes) as a helper of cron_to_interval_or_next_run. Signature and actual use UNVERIFIED until captured.
- `create_any_table_partition` — CREATE worker: creates future partitions of (schema, table).
- `drop_any_table_partition` — DROP worker: removes partitions older than the retention interval (destructive).
- `run_partition_create_jobs` — Legacy polling runner. PartOps never calls it; it must NOT be scheduled (pgAgent) against a database served by the realtime scheduler.
- `run_partition_drop_jobs` — Legacy polling runner (see run_partition_create_jobs).

## EXTERNAL OBJECTS (pgAgent; optional, read-only)

| kind | object | signature | status | in_git | required_by | required | security | owner | grants | called by | source |
|---|---|---|---|---|---|---|---|---|---|---|---|
| table | `pgagent.pga_job` | — | external_optional | no | api | no | — | — | SELECT→partition_job_ui | database.get_pgagent_jobs, system_readiness | NOT IN GIT |
| table | `pgagent.pga_jobstep` | — | external_optional | no | api | no | — | — | SELECT→partition_job_ui | database.get_pgagent_job_details | NOT IN GIT |
| table | `pgagent.pga_schedule` | — | external_optional | no | api | no | — | — | SELECT→partition_job_ui | database.get_pgagent_job_details | NOT IN GIT |

Purpose:

- `pga_job` — pgAgent job list (read-only; job listing, Convert page, legacy-runner detection).
- `pga_jobstep` — pgAgent job steps (read-only).
- `pga_schedule` — pgAgent schedules (read-only).

## Authoritative definitions missing from Git (5)

- `mubasher_oms.insert_data_to_partition_job_table(character varying, boolean, character varying, character varying, jsonb, character varying, interval, timestamp without time zone, character varying, numeric, boolean, interval)` — see `sql/framework/captured/insert_data_to_partition_job_table.CAPTURE_REQUIRED.md`
- `mubasher_oms.cron_to_interval_or_next_run(text)` — see `sql/framework/captured/cron_to_interval_or_next_run.CAPTURE_REQUIRED.md`
- `mubasher_oms.cron_field_to_int_array(unknown)` — see `sql/framework/captured/cron_field_to_int_array.CAPTURE_REQUIRED.md`
- `mubasher_oms.create_any_table_partition(character varying, character varying, character varying, integer, interval)` — see `sql/framework/captured/create_any_table_partition.CAPTURE_REQUIRED.md`
- `mubasher_oms.drop_any_table_partition(character varying, character varying, interval)` — see `sql/framework/captured/drop_any_table_partition.CAPTURE_REQUIRED.md`

## Built-in functions and lock namespaces used

- `pg_try_advisory_xact_lock(integer, integer)`
- `hashtext(text)`
- `set_config(text, text, boolean)`
- `jsonb_each_text(jsonb)`
- `clock_timestamp()`
- `to_char(timestamp, text)`
- `current_database()`
- `pg_get_functiondef(oid)`
- `has_function_privilege`
- `has_table_privilege`
- `to_regprocedure`
- `to_regclass`
- advisory lock namespace `87421031` — per job_id (run_partition_job_scheduled, reconcile_missed_partition_job)
- advisory lock namespace `87421032` — per target (schema.table) — scheduled AND manual CREATE/DROP
- extensions required: none; optional: pgagent (job listing / Convert page only)
