-- DISPOSABLE TEST DATABASE ONLY — run exclusively by
-- scripts/disposable_db/run_disposable_db_test.sh BEFORE 20261117_01 is applied.
-- Creates jobs that predate the target registry so the backfill can be proven.
\set ON_ERROR_STOP 1
INSERT INTO mubasher_oms.partitioning_job_table
  (job_id, job_name, is_enabled, table_schema, table_name, db_config_para, frequency,
   next_run_time, partition_unit, partition_period, is_create, create_drop_interval, job_schedule)
VALUES
  (901, 'PRE5C_CREATE', false, 'pre5c', 'orders', '{"work_mem":"64MB"}', '1 day',
      timestamp '2027-01-01 02:00', 'day', 1, true, '1 month', '0 0 2 * * *'),
  (902, 'PRE5C_DROP', false, 'pre5c', 'orders', '{}', '1 day',
      timestamp '2027-01-01 03:00', 'day', 1, false, '6 months', '0 0 3 * * *');
-- Disposable-only snapshot (public schema of the throwaway DB) to prove the
-- migration does not change business configuration.
CREATE TABLE public.pre5c_snapshot AS
  SELECT job_id, job_schedule || '|' || next_run_time::text || '|' || db_config_para::text AS payload
    FROM mubasher_oms.partitioning_job_table WHERE job_id IN (901, 902);
