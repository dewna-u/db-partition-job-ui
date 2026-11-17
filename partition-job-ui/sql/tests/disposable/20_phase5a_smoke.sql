-- DISPOSABLE TEST DATABASE ONLY — run exclusively by
-- scripts/disposable_db/run_disposable_db_test.sh against a throwaway cluster.
-- Never run against production, reference or shared databases.
\set ON_ERROR_STOP 1
\pset pager off
-- =====================================================================
-- T1 catalog: security attributes, owners, PUBLIC execute, search_path
-- =====================================================================
DO $$
DECLARE r record; n int := 0;
BEGIN
  FOR r IN
    SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner) AS owner,
           COALESCE(p.proacl::text, '') AS acl, COALESCE(array_to_string(p.proconfig, ','), '') AS cfg
      FROM pg_proc p
     WHERE p.pronamespace = 'mubasher_oms'::regnamespace
       AND p.proname IN ('run_partition_job_scheduled','run_partition_job_manual',
                         'reconcile_missed_partition_job','run_partition_job_attention_recovery',
                         'acknowledge_partition_job_attention','update_data_to_partition_job_table',
                         'partition_job_max_execution_lateness','partition_job_config_param_allowed',
                         'fn_partition_job_attention_guard')
  LOOP
    n := n + 1;
    -- PUBLIC grant appears as '=X/' in the ACL. NULL acl means default (PUBLIC) → fail.
    IF r.acl = '' OR r.acl LIKE '%{=X/%' OR r.acl LIKE '%,=X/%' THEN
      RAISE EXCEPTION 'T1 % still executable by PUBLIC (acl=%)', r.proname, r.acl;
    END IF;
    IF r.proname = 'update_data_to_partition_job_table' THEN
      IF NOT r.prosecdef OR r.owner <> 'mubasher_oms' OR r.cfg NOT LIKE 'search_path=pg_catalog, mubasher_oms, pg_temp' THEN
        RAISE EXCEPTION 'T1 update fn hardening wrong: % % %', r.prosecdef, r.owner, r.cfg;
      END IF;
    ELSIF r.prosecdef THEN
      RAISE EXCEPTION 'T1 % must be SECURITY INVOKER', r.proname;
    END IF;
    IF r.proname IN ('acknowledge_partition_job_attention','fn_partition_job_attention_guard')
       AND r.cfg NOT LIKE 'search_path=pg_catalog, mubasher_oms, pg_temp' THEN
      RAISE EXCEPTION 'T1 % missing fixed search_path', r.proname;
    END IF;
    IF r.proname IN ('reconcile_missed_partition_job','run_partition_job_attention_recovery',
                     'acknowledge_partition_job_attention','partition_job_max_execution_lateness',
                     'partition_job_config_param_allowed','fn_partition_job_attention_guard',
                     'run_partition_job_manual','update_data_to_partition_job_table')
       AND r.owner <> 'mubasher_oms' THEN
      RAISE EXCEPTION 'T1 % owner is %', r.proname, r.owner;
    END IF;
  END LOOP;
  IF n <> 9 THEN RAISE EXCEPTION 'T1 expected 9 functions, found %', n; END IF;
  IF NOT has_function_privilege('partition_job_scheduler', 'mubasher_oms.reconcile_missed_partition_job(numeric,timestamp without time zone,text,text)', 'EXECUTE')
     OR has_function_privilege('partition_job_ui', 'mubasher_oms.reconcile_missed_partition_job(numeric,timestamp without time zone,text,text)', 'EXECUTE')
     OR NOT has_function_privilege('partition_job_ui', 'mubasher_oms.run_partition_job_attention_recovery(bigint)', 'EXECUTE')
     OR has_function_privilege('partition_job_scheduler', 'mubasher_oms.run_partition_job_attention_recovery(bigint)', 'EXECUTE')
     OR has_table_privilege('partition_job_ui', 'mubasher_oms.partition_job_attention', 'DELETE')
     OR has_column_privilege('partition_job_ui', 'mubasher_oms.partition_job_attention', 'missed_run_time', 'UPDATE')
     OR NOT has_column_privilege('partition_job_ui', 'mubasher_oms.partition_job_attention', 'status', 'UPDATE')
     OR has_table_privilege('partition_job_ui', 'mubasher_oms.partitioning_job_table', 'UPDATE')
     OR NOT has_table_privilege('partition_job_scheduler', 'mubasher_oms.partitioning_job_table_log', 'SELECT')
  THEN RAISE EXCEPTION 'T1 role privilege matrix wrong'; END IF;
  RAISE NOTICE 'T1 OK catalog/security (% functions)', n;
END $$;

-- =====================================================================
-- T2 allowlist incl. DateStyle; set_config keeps ISO output
-- =====================================================================
DO $$
BEGIN
  IF NOT mubasher_oms.partition_job_config_param_allowed('lock_timeout','30s')
     OR NOT mubasher_oms.partition_job_config_param_allowed('work_mem','512MB')
     OR NOT mubasher_oms.partition_job_config_param_allowed('DateStyle','ISO, DMY')
     OR NOT mubasher_oms.partition_job_config_param_allowed('datestyle','ISO, MDY')
     OR mubasher_oms.partition_job_config_param_allowed('datestyle','Redwood, SHOW_TIME')
     OR mubasher_oms.partition_job_config_param_allowed('datestyle','SQL, DMY')
     OR mubasher_oms.partition_job_config_param_allowed('search_path','public')
     OR mubasher_oms.partition_job_config_param_allowed('role','postgres')
     OR mubasher_oms.partition_job_config_param_allowed('session_authorization','x')
     OR mubasher_oms.partition_job_config_param_allowed('work_mem','1GB; RESET ROLE')
  THEN RAISE EXCEPTION 'T2 allowlist wrong'; END IF;
  PERFORM set_config('datestyle', 'ISO, DMY', true);
  IF current_setting('datestyle') NOT LIKE 'ISO%' THEN RAISE EXCEPTION 'T2 datestyle %', current_setting('datestyle'); END IF;
  PERFORM set_config('datestyle', 'ISO, MDY', true);
  IF current_setting('datestyle') NOT LIKE 'ISO%' THEN RAISE EXCEPTION 'T2 datestyle %', current_setting('datestyle'); END IF;
  RAISE NOTICE 'T2 OK allowlist + ISO DateStyle';
END $$;

-- =====================================================================
-- Synthetic jobs (no business tables touched; targets are fictitious)
-- =====================================================================
INSERT INTO mubasher_oms.partitioning_job_table
  (job_id, job_name, is_enabled, table_schema, table_name, db_config_para, frequency,
   next_run_time, last_run_time, last_run_status, partition_unit, partition_period,
   is_create, create_drop_interval, job_schedule)
VALUES
  (1, 'SMOKE_HISTORICAL', true, 'smoke', 'orders', '{}', '1 day',
      clock_timestamp()::timestamp - interval '1 day', NULL, NULL, 'day', 1, true, '1 month', '0 0 2 * * *'),
  (2, 'SMOKE_CEILING', true, 'smoke', 'trades', '{}', '1 day',
      clock_timestamp()::timestamp - interval '7 hours', clock_timestamp()::timestamp - interval '2 days', 'SUCCESS',
      'day', 1, true, '1 month', '0 0 2 * * *'),
  (3, 'SMOKE_OWNED_LATE', true, 'smoke', 'quotes', '{}', '1 day',
      clock_timestamp()::timestamp - interval '30 minutes', NULL, NULL, 'day', 1, false, '3 months', '0 0 2 * * *'),
  (4, 'SMOKE_FAILED_GAUGE', true, 'smoke', 'fills', '{}', '1 day',
      clock_timestamp()::timestamp + interval '1 day', timestamp '2026-11-01 02:00', 'FAIL',
      'day', 1, true, '1 month', '0 0 2 * * *'),
  (5, 'SMOKE_EDIT', true, 'smoke', 'audit', '{}', '1 day',
      clock_timestamp()::timestamp + interval '1 day', NULL, NULL, 'day', 1, true, '1 month', '0 0 2 * * *'),
  (6, 'SMOKE_BAD_CONFIG', true, 'smoke', 'ledger', '{"search_path": "public"}', '1 day',
      clock_timestamp()::timestamp + interval '1 day', NULL, NULL, 'day', 1, true, '1 month', '0 0 2 * * *');

CREATE TEMP TABLE smoke_before AS
  SELECT job_id, next_run_time, last_run_time, last_run_status FROM mubasher_oms.partitioning_job_table;
GRANT SELECT ON smoke_before TO PUBLIC;

-- =====================================================================
-- T3 historical reconcile (scheduler role): no DDL, attention row,
--    fail closed (cron helper not available here), last_run_* untouched
-- =====================================================================
SET ROLE partition_job_scheduler;
DO $$
DECLARE r record; a record; j record; b record;
BEGIN
  SELECT * INTO b FROM smoke_before WHERE job_id = 1;
  SELECT * INTO r FROM mubasher_oms.reconcile_missed_partition_job(1, b.next_run_time, 'MISSED_SCHEDULED_RUN', 'smoke');
  IF r.status <> 'MISSED_INVALID_SCHEDULE' THEN RAISE EXCEPTION 'T3 status %', r.status; END IF;
  SELECT * INTO j FROM mubasher_oms.partitioning_job_table WHERE job_id = 1;
  IF j.next_run_time IS NOT NULL OR j.last_run_status IS NOT NULL OR j.last_run_time IS NOT NULL THEN
    RAISE EXCEPTION 'T3 job row wrong % % %', j.next_run_time, j.last_run_status, j.last_run_time; END IF;
  SELECT * INTO a FROM mubasher_oms.partition_job_attention WHERE job_id = 1;
  IF a.status <> 'PENDING_REVIEW' OR a.reason <> 'MISSED_SCHEDULED_RUN' OR a.missed_run_time <> b.next_run_time
     OR a.rescheduled_to IS NOT NULL OR a.detail NOT LIKE 'smoke%' OR a.table_name <> 'orders' THEN
    RAISE EXCEPTION 'T3 attention row wrong %', row_to_json(a); END IF;
  -- Second call: occurrence no longer current → SKIPPED_RESCHEDULED, no duplicate row.
  SELECT * INTO r FROM mubasher_oms.reconcile_missed_partition_job(1, b.next_run_time, 'MISSED_SCHEDULED_RUN', NULL);
  IF r.status <> 'SKIPPED_RESCHEDULED' THEN RAISE EXCEPTION 'T3 second status %', r.status; END IF;
  IF (SELECT count(*) FROM mubasher_oms.partition_job_attention WHERE job_id = 1) <> 1 THEN
    RAISE EXCEPTION 'T3 duplicate attention'; END IF;
  IF (SELECT count(*) FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 1) <> 0 THEN
    RAISE EXCEPTION 'T3 log row written for a missed run'; END IF;
  RAISE NOTICE 'T3 OK historical reconcile: no DDL, attention recorded, last_run_* untouched';
END $$;

-- =====================================================================
-- T4 DB ceiling (> 6 h late) reconciles; 30 min late (owned) is NOT missed
-- =====================================================================
DO $$
DECLARE r record; b record; j record;
BEGIN
  SELECT * INTO b FROM smoke_before WHERE job_id = 2;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(2, b.next_run_time);
  IF r.status NOT IN ('MISSED_INVALID_SCHEDULE','MISSED_RESCHEDULED') THEN RAISE EXCEPTION 'T4 ceiling status %', r.status; END IF;
  SELECT * INTO j FROM mubasher_oms.partitioning_job_table WHERE job_id = 2;
  IF j.last_run_status <> 'SUCCESS' OR j.last_run_time <> b.last_run_time THEN RAISE EXCEPTION 'T4 ceiling touched last_run_*'; END IF;
  IF (SELECT detail FROM mubasher_oms.partition_job_attention WHERE job_id = 2) NOT LIKE 'Exceeded maximum execution lateness%' THEN
    RAISE EXCEPTION 'T4 ceiling detail missing'; END IF;

  SELECT * INTO b FROM smoke_before WHERE job_id = 3;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(3, b.next_run_time);
  -- Went down the EXECUTION path (no 10-minute missed rule); stopped fail-closed
  -- only because the unversioned cron helper does not exist in this disposable DB.
  IF r.status <> 'FAILED_INVALID_SCHEDULE' THEN RAISE EXCEPTION 'T4 owned-late status %', r.status; END IF;
  IF EXISTS (SELECT 1 FROM mubasher_oms.partition_job_attention WHERE job_id = 3) THEN
    RAISE EXCEPTION 'T4 30-min-late occurrence wrongly recorded as missed'; END IF;
  RAISE NOTICE 'T4 OK ceiling reconciles >6h; 30-min-late owned occurrence is executed-path (not missed)';
END $$;
RESET ROLE;

-- =====================================================================
-- T5 attention state machine / immutability (UI role + owner)
-- =====================================================================
SET ROLE partition_job_ui;
DO $$
DECLARE aid bigint; r record;
BEGIN
  SELECT attention_id INTO aid FROM mubasher_oms.partition_job_attention WHERE job_id = 2;
  BEGIN
    UPDATE mubasher_oms.partition_job_attention SET missed_run_time = now() WHERE attention_id = aid;
    RAISE EXCEPTION 'T5 UI could change missed_run_time';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    DELETE FROM mubasher_oms.partition_job_attention WHERE attention_id = aid;
    RAISE EXCEPTION 'T5 UI could delete evidence';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  BEGIN
    UPDATE mubasher_oms.partition_job_attention SET status = 'MANUALLY_RERUN', reviewed_at = now() WHERE attention_id = aid;
    RAISE EXCEPTION 'T5 MANUALLY_RERUN without success accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  SELECT * INTO r FROM mubasher_oms.acknowledge_partition_job_attention(aid, 'smoke ack');
  IF r.attention_status <> 'ACKNOWLEDGED' THEN RAISE EXCEPTION 'T5 ack status %', r.attention_status; END IF;
  BEGIN
    PERFORM mubasher_oms.acknowledge_partition_job_attention(aid, 'again');
    RAISE EXCEPTION 'T5 acknowledged twice';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    UPDATE mubasher_oms.partition_job_attention SET status = 'PENDING_REVIEW' WHERE attention_id = aid;
    RAISE EXCEPTION 'T5 resolved item reopened';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  RAISE NOTICE 'T5 OK immutable evidence, no delete, single resolution';
END $$;
RESET ROLE;
DO $$
BEGIN
  -- Even the table owner cannot rewrite detection evidence (trigger).
  BEGIN
    UPDATE mubasher_oms.partition_job_attention SET table_name = 'other' WHERE job_id = 1;
    RAISE EXCEPTION 'T5 owner rewrote target snapshot';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  RAISE NOTICE 'T5 OK owner cannot rewrite snapshot';
END $$;

-- =====================================================================
-- T6 recovery (UI role): failure recorded, item stays pending,
--    next_run_time never changed; config drift refused
-- =====================================================================
UPDATE mubasher_oms.partitioning_job_table SET next_run_time = timestamp '2026-12-01 02:00' WHERE job_id = 1;
SET ROLE partition_job_ui;
DO $$
DECLARE aid bigint; r record; a record; nr timestamp;
BEGIN
  SELECT attention_id INTO aid FROM mubasher_oms.partition_job_attention WHERE job_id = 1;
  SELECT next_run_time INTO nr FROM mubasher_oms.partitioning_job_table WHERE job_id = 1;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_attention_recovery(aid);
  -- create_any_table_partition is not available here → worker error → MANUAL_FAIL.
  IF r.result_status <> 'MANUAL_FAIL' OR r.attention_status <> 'PENDING_REVIEW' THEN
    RAISE EXCEPTION 'T6 recovery result % %', r.result_status, r.attention_status; END IF;
  SELECT * INTO a FROM mubasher_oms.partition_job_attention WHERE attention_id = aid;
  IF a.manual_execution_result <> 'MANUAL_FAIL' OR a.manual_executed_at IS NULL OR a.status <> 'PENDING_REVIEW' THEN
    RAISE EXCEPTION 'T6 attempt not recorded'; END IF;
  IF (SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 1) IS DISTINCT FROM nr THEN
    RAISE EXCEPTION 'T6 recovery changed next_run_time'; END IF;
  IF (SELECT count(*) FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 1 AND last_run_status = 'MANUAL_FAIL') <> 1 THEN
    RAISE EXCEPTION 'T6 MANUAL_FAIL log row missing'; END IF;
  RAISE NOTICE 'T6 OK recovery failure recorded; schedule unchanged';
END $$;
RESET ROLE;
UPDATE mubasher_oms.partitioning_job_table SET table_name = 'orders_v2' WHERE job_id = 1;
SET ROLE partition_job_ui;
DO $$
DECLARE aid bigint;
BEGIN
  SELECT attention_id INTO aid FROM mubasher_oms.partition_job_attention WHERE job_id = 1;
  BEGIN
    PERFORM mubasher_oms.run_partition_job_attention_recovery(aid);
    RAISE EXCEPTION 'T6 recovery ran after target changed';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  RAISE NOTICE 'T6 OK recovery refused after config drift';
END $$;

-- =====================================================================
-- T7 manual allowlist enforcement + edit-function allowlist (UI role)
-- =====================================================================
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM mubasher_oms.run_partition_job_manual(6);
  IF r.status <> 'MANUAL_FAIL' OR r.message NOT LIKE '%not allowed by the PartOps allowlist%' THEN
    RAISE EXCEPTION 'T7 manual allowlist % %', r.status, r.message; END IF;
  BEGIN
    PERFORM mubasher_oms.update_data_to_partition_job_table(
      5, 'SMOKE_EDIT', true, 'smoke', 'audit', '{"role": "postgres"}'::jsonb, '0 0 2 * * *',
      '1 day', (clock_timestamp() + interval '1 day')::timestamp, 'day', 1, true, '1 month');
    RAISE EXCEPTION 'T7 edit accepted role';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  PERFORM mubasher_oms.update_data_to_partition_job_table(
      5, 'SMOKE_EDIT', true, 'smoke', 'audit', '{"lock_timeout": "5s", "datestyle": "ISO, DMY"}'::jsonb,
      '0 0 2 * * *', '1 day', (clock_timestamp() + interval '1 day')::timestamp, 'day', 1, true, '1 month');
  IF (SELECT db_config_para->>'lock_timeout' FROM mubasher_oms.partitioning_job_table WHERE job_id = 5) <> '5s' THEN
    RAISE EXCEPTION 'T7 allowed edit not stored'; END IF;
  RAISE NOTICE 'T7 OK allowlist on manual run and edit (DEFINER)';
END $$;
RESET ROLE;

-- =====================================================================
-- T9b framework version record (Phase 5B)
-- =====================================================================
DO $$
BEGIN
  -- 20261110_03 must be recorded; later phases (5C: 20261117_02) add newer rows.
  IF NOT EXISTS (SELECT 1 FROM mubasher_oms.partops_framework_version WHERE migration_id = '20261110_03')
     OR (SELECT max(migration_id) FROM mubasher_oms.partops_framework_version) < '20261110_03' THEN
    RAISE EXCEPTION 'T9b framework version wrong'; END IF;
  IF has_table_privilege('partition_job_ui', 'mubasher_oms.partops_framework_version', 'INSERT')
     OR has_table_privilege('partition_job_scheduler', 'mubasher_oms.partops_framework_version', 'UPDATE')
     OR NOT has_table_privilege('partition_job_scheduler', 'mubasher_oms.partops_framework_version', 'SELECT') THEN
    RAISE EXCEPTION 'T9b version table privileges wrong'; END IF;
  RAISE NOTICE 'T9b OK framework version 20261110_03 recorded (max %); app roles read-only', (SELECT max(migration_id) FROM mubasher_oms.partops_framework_version);
END $$;
