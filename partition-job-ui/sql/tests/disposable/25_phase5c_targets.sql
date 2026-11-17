-- DISPOSABLE TEST DATABASE ONLY — run exclusively by
-- scripts/disposable_db/run_disposable_db_test.sh against a throwaway cluster.
-- Never run against production, reference or shared databases.
--
-- Phase 5C: target registry, backfill, remote-target guards. No legacy worker
-- or stub is used — guards return before any worker would be called.
\set ON_ERROR_STOP 1

-- U1 default target + backfill of jobs that existed BEFORE 20261117_01
DO $$
DECLARE c int; ctl int;
BEGIN
  ctl := mubasher_oms.partops_control_target_id();
  IF ctl IS NULL THEN RAISE EXCEPTION 'U1 no control target'; END IF;
  IF (SELECT target_key FROM mubasher_oms.partops_database_target WHERE target_id = ctl) <> 'db01'
     OR (SELECT database_name FROM mubasher_oms.partops_database_target WHERE target_id = ctl) <> current_database() THEN
    RAISE EXCEPTION 'U1 default target wrong'; END IF;
  SELECT count(*) INTO c FROM mubasher_oms.partitioning_job_table WHERE job_id IN (901, 902) AND database_target_id = ctl;
  IF c <> 2 THEN RAISE EXCEPTION 'U1 pre-5C jobs not backfilled (%)', c; END IF;
  IF EXISTS (SELECT 1 FROM mubasher_oms.partitioning_job_table WHERE database_target_id IS NULL) THEN
    RAISE EXCEPTION 'U1 orphan job'; END IF;
  -- Business configuration untouched by the backfill.
  IF (SELECT job_schedule || '|' || next_run_time::text || '|' || db_config_para::text
        FROM mubasher_oms.partitioning_job_table WHERE job_id = 901)
     <> (SELECT payload FROM pre5c_snapshot WHERE job_id = 901) THEN
    RAISE EXCEPTION 'U1 job 901 configuration changed by migration'; END IF;
  RAISE NOTICE 'U1 OK default target db01 = current_database(); pre-5C jobs backfilled; config unchanged';
END $$;

-- U2 new rows default to the control target (legacy insert path is unaware of targets)
INSERT INTO mubasher_oms.partitioning_job_table
  (job_id, job_name, is_enabled, table_schema, table_name, db_config_para, frequency,
   next_run_time, partition_unit, partition_period, is_create, create_drop_interval, job_schedule)
VALUES (950, 'SMOKE_DEFAULT_TARGET', true, 'smoke', 'defaults', '{}', '1 day',
        clock_timestamp()::timestamp + interval '1 day', 'day', 1, true, '1 month', '0 0 2 * * *');
DO $$
BEGIN
  IF (SELECT database_target_id FROM mubasher_oms.partitioning_job_table WHERE job_id = 950)
     IS DISTINCT FROM mubasher_oms.partops_control_target_id() THEN
    RAISE EXCEPTION 'U2 default not applied'; END IF;
  RAISE NOTICE 'U2 OK new jobs default to the control target';
END $$;

-- Register a second managed target (DBA action) and a job pointing at it.
INSERT INTO mubasher_oms.partops_database_target
  (target_key, display_name, host, port, database_name, credential_provider, is_enabled)
VALUES ('db02', 'DB-02', '127.0.0.1', :port, 'partops_target2', 'env', true),
       ('db03', 'DB-03 (disabled)', '127.0.0.1', :port, 'partops_target3', 'env', false),
       ('db04', 'DB-04 (unreachable)', '127.0.0.1', 1, 'partops_target4', 'env', true);
INSERT INTO mubasher_oms.partitioning_job_table
  (job_id, job_name, is_enabled, table_schema, table_name, db_config_para, frequency,
   next_run_time, partition_unit, partition_period, is_create, create_drop_interval, job_schedule,
   database_target_id)
VALUES (960, 'SMOKE_REMOTE', true, 'smoke', 'remote_orders', '{}', '1 day',
        clock_timestamp()::timestamp - interval '1 minute', 'day', 1, true, '1 month', '0 0 2 * * *',
        (SELECT target_id FROM mubasher_oms.partops_database_target WHERE target_key = 'db02'));

-- U3 scheduler never sees / executes / reconciles a remote job here
SET ROLE partition_job_scheduler;
DO $$
DECLARE r record; nr timestamp;
BEGIN
  IF EXISTS (SELECT 1 FROM mubasher_oms.get_upcoming_partition_jobs(interval '1 day') u WHERE u.job_id = 960) THEN
    RAISE EXCEPTION 'U3 get_upcoming returned a remote-target job'; END IF;
  SELECT next_run_time INTO nr FROM mubasher_oms.partitioning_job_table WHERE job_id = 960;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(960, nr);
  IF r.status <> 'SKIPPED_REMOTE_TARGET' THEN RAISE EXCEPTION 'U3 scheduled status %', r.status; END IF;
  -- reconcile_missed_partition_job is target-aware since Phase 5D (records
  -- attention + advances the schedule for any target, never DDL): stage 11.
  IF EXISTS (SELECT 1 FROM mubasher_oms.partition_job_attention WHERE job_id = 960)
     OR EXISTS (SELECT 1 FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 960)
     OR (SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 960) IS DISTINCT FROM nr THEN
    RAISE EXCEPTION 'U3 remote job state changed'; END IF;
  RAISE NOTICE 'U3 OK remote-target job: not discovered by the control-only function, local executor SKIPPED_REMOTE_TARGET, no attention/log/schedule change';
END $$;
RESET ROLE;

-- U4 manual run of a remote job executes nothing
SET ROLE partition_job_ui;
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM mubasher_oms.run_partition_job_manual(960);
  IF r.status <> 'MANUAL_REMOTE_TARGET' THEN RAISE EXCEPTION 'U4 manual status %', r.status; END IF;
  IF EXISTS (SELECT 1 FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 960) THEN
    RAISE EXCEPTION 'U4 log row for remote manual run'; END IF;
  -- Registry is read-only for the API role.
  BEGIN
    UPDATE mubasher_oms.partops_database_target SET is_enabled = false WHERE target_key = 'db02';
    RAISE EXCEPTION 'U4 UI role modified the registry';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RAISE NOTICE 'U4 OK manual run of remote job: MANUAL_REMOTE_TARGET, nothing logged; registry read-only';
END $$;
RESET ROLE;

-- U5 registry constraints (no credential storage; one control row; FK protects targets)
DO $$
BEGIN
  BEGIN
    INSERT INTO mubasher_oms.partops_database_target (target_key, display_name, database_name, is_control_database)
    VALUES ('db99', 'second control', 'x', true);
    RAISE EXCEPTION 'U5 second control target accepted';
  EXCEPTION WHEN unique_violation THEN NULL; END;
  BEGIN
    INSERT INTO mubasher_oms.partops_database_target (target_key, display_name, host, database_name, credential_provider, secret_id)
    VALUES ('db98', 'json secret', 'h', 'x', 'aws', '{"password":"x"}');
    RAISE EXCEPTION 'U5 secret value accepted as secret_id';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    INSERT INTO mubasher_oms.partops_database_target (target_key, display_name, host, database_name, credential_provider)
    VALUES ('db97', 'aws no secret', 'h', 'x', 'aws');
    RAISE EXCEPTION 'U5 aws target without secret_id accepted';
  EXCEPTION WHEN check_violation THEN NULL; END;
  BEGIN
    DELETE FROM mubasher_oms.partops_database_target WHERE target_key = 'db02';
    RAISE EXCEPTION 'U5 target with jobs deleted';
  EXCEPTION WHEN foreign_key_violation THEN NULL; END;
  IF EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_schema = 'mubasher_oms' AND table_name = 'partops_database_target'
                AND column_name ILIKE ANY (ARRAY['%password%', '%access_key%', '%secret_value%'])) THEN
    RAISE EXCEPTION 'U5 credential column present'; END IF;
  RAISE NOTICE 'U5 OK one control target; no secret values; FK protects targets with jobs';
END $$;
