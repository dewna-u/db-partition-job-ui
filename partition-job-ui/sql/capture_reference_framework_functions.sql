-- ============================================================================
-- capture_reference_framework_functions.sql
-- ============================================================================
-- READ-ONLY capture helper.
--
-- Run this against the REFERENCE database that ALREADY has the working
-- partition-job framework (NOT against the NEW production-copy database, and
-- NOT against live production unless your DBA process explicitly allows it).
--
-- Purpose
--   Dump authoritative CREATE statements so they can be reviewed and then
--   applied to the NEW database. Do not invent replacements.
--
-- After capture:
--   1. Save each definition to a reviewed file under sql/imported/ (local only;
--      do not commit secrets).
--   2. Confirm signatures match the project contract below.
--   3. Apply to NEW DB only after explicit approval.
-- ============================================================================

SELECT current_database() AS capture_database,
       current_user       AS capture_user,
       now()              AS captured_at;

-- Expected signatures (verify identity_args match):
--   create_any_table_partition(varchar, varchar, varchar, integer, interval) -> void
--   drop_any_table_partition(varchar, varchar, interval) -> void
--   cron_to_interval_or_next_run(text) -> mubasher_oms.cron_schedule_result
--   insert_data_to_partition_job_table(12 typed args used by the UI)
--   run_partition_job_manual(numeric)
--   run_partition_create_jobs()
--   run_partition_drop_jobs()
--   cron_field_to_int_array(...)

SELECT
    p.proname,
    pg_get_function_identity_arguments(p.oid) AS identity_args,
    pg_get_function_result(p.oid)             AS result_type,
    p.prosecdef                               AS is_security_definer,
    pg_get_userbyid(p.proowner)               AS owner,
    p.proconfig                               AS attached_set_clauses,
    pg_get_functiondef(p.oid)                 AS function_definition
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'mubasher_oms'
   AND p.proname IN (
         'cron_field_to_int_array',
         'cron_to_interval_or_next_run',
         'create_any_table_partition',
         'drop_any_table_partition',
         'insert_data_to_partition_job_table',
         'run_partition_job_manual',
         'run_partition_create_jobs',
         'run_partition_drop_jobs'
       )
 ORDER BY p.proname, p.oid;
