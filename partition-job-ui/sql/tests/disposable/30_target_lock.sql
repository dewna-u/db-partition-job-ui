-- DISPOSABLE TEST DATABASE ONLY — run exclusively by
-- scripts/disposable_db/run_disposable_db_test.sh against a throwaway cluster.
-- Never run against production, reference or shared databases.
\set ON_ERROR_STOP 1
-- Runs while another session holds the target lock for smoke.fills.
SET ROLE partition_job_ui;
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM mubasher_oms.run_partition_job_manual(4);
  IF r.status <> 'MANUAL_TARGET_LOCKED' THEN RAISE EXCEPTION 'T8 manual status %', r.status; END IF;
  IF EXISTS (SELECT 1 FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 4) THEN
    RAISE EXCEPTION 'T8 log row written while target locked'; END IF;
  RAISE NOTICE 'T8 OK manual run blocked by shared target lock (no execution, no log)';
END $$;
RESET ROLE;
UPDATE mubasher_oms.partitioning_job_table
   SET next_run_time = clock_timestamp()::timestamp - interval '20 minutes' WHERE job_id = 4;
SET ROLE partition_job_scheduler;
DO $$
DECLARE r record; nr timestamp;
BEGIN
  SELECT next_run_time INTO nr FROM mubasher_oms.partitioning_job_table WHERE job_id = 4;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(4, nr);
  IF r.status <> 'SKIPPED_TARGET_LOCKED' THEN RAISE EXCEPTION 'T8 scheduled status %', r.status; END IF;
  IF EXISTS (SELECT 1 FROM mubasher_oms.partition_job_attention WHERE job_id = 4) THEN
    RAISE EXCEPTION 'T8 20-min-late locked occurrence wrongly recorded as missed'; END IF;
  RAISE NOTICE 'T8 OK scheduled SKIPPED_TARGET_LOCKED (owned, not missed)';
END $$;
RESET ROLE;
