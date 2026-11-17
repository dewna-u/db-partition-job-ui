-- DISPOSABLE TEST DATABASE ONLY — run exclusively by
-- scripts/disposable_db/run_disposable_db_test.sh against a throwaway cluster.
-- Never run against production, reference or shared databases.
--
-- FULL INTEGRATION (sql/framework/INTEGRATION_TEST_PLAN.md).
-- Runs ONLY when every required authoritative definition has been captured
-- into sql/framework/captured/ (the harness checks this and otherwise prints
-- "FRAMEWORK DEFINITION NOT AVAILABLE" without running this file).
--
-- NOT YET EXECUTED: no captured definitions exist yet. Assertions are at the
-- PartOps contract level only. The fixture below is a GUESS at the table
-- shape the workers expect and is marked FIXTURE ADAPTATION REQUIRED — adapt
-- it to the captured create_any_table_partition / drop_any_table_partition
-- during the security review. Each case prints NOTICE 'ITEST <id> OK'.
\set ON_ERROR_STOP 1

-- ---------------------------------------------------------------------------
-- FIXTURE ADAPTATION REQUIRED (synthetic, range-partitioned by day)
-- ---------------------------------------------------------------------------
CREATE SCHEMA itest;
CREATE TABLE itest.orders (id bigint, created_at timestamp NOT NULL) PARTITION BY RANGE (created_at);
CREATE TABLE itest.trades (id bigint, created_at timestamp NOT NULL) PARTITION BY RANGE (created_at);
CREATE TABLE itest.trades_old PARTITION OF itest.trades
    FOR VALUES FROM ('2020-01-01') TO ('2020-01-02');
-- Business-table privileges the INVOKER workers need (document the real minimum in review).
GRANT USAGE, CREATE ON SCHEMA itest TO partition_job_scheduler, partition_job_ui;
ALTER TABLE itest.orders OWNER TO partition_job_scheduler;
ALTER TABLE itest.trades OWNER TO partition_job_scheduler;
ALTER TABLE itest.trades_old OWNER TO partition_job_scheduler;

CREATE FUNCTION pg_temp.partition_count(p_schema text, p_table text) RETURNS bigint
LANGUAGE sql AS $$
  SELECT count(*) FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhparent JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = p_schema AND c.relname = p_table
$$;

INSERT INTO mubasher_oms.partitioning_job_table
  (job_id, job_name, is_enabled, table_schema, table_name, db_config_para, frequency,
   next_run_time, partition_unit, partition_period, is_create, create_drop_interval, job_schedule)
VALUES
  (101, 'IT_CREATE', true, 'itest', 'orders', '{}', '1 day', clock_timestamp()::timestamp - interval '5 seconds', 'day', 1, true,  '7 days',  '0 0 2 * * *'),
  (102, 'IT_CREATE_MISSED', true, 'itest', 'orders', '{}', '1 day', clock_timestamp()::timestamp - interval '3 days', 'day', 1, true, '7 days', '0 0 2 * * *'),
  (201, 'IT_DROP',   true, 'itest', 'trades', '{}', '1 day', clock_timestamp()::timestamp - interval '5 seconds', 'day', 1, false, '30 days', '0 0 3 * * *'),
  (202, 'IT_DROP_MISSED', true, 'itest', 'trades', '{}', '1 day', clock_timestamp()::timestamp - interval '3 days', 'day', 1, false, '30 days', '0 0 3 * * *'),
  (301, 'IT_FUTURE', true, 'itest', 'orders', '{}', '1 day', clock_timestamp()::timestamp + interval '1 day', 'day', 1, true, '7 days', '0 0 2 * * *'),
  (401, 'IT_BAD_CFG', true, 'itest', 'orders', '{"search_path": "public"}', '1 day', clock_timestamp()::timestamp - interval '5 seconds', 'day', 1, true, '7 days', '0 0 2 * * *'),
  (501, 'IT_NO_TABLE', true, 'itest', 'does_not_exist', '{}', '1 day', clock_timestamp()::timestamp - interval '5 seconds', 'day', 1, true, '7 days', '0 0 2 * * *');

CREATE TEMP TABLE it_before AS SELECT job_id, next_run_time, last_run_status, last_run_time FROM mubasher_oms.partitioning_job_table;
GRANT SELECT ON it_before TO PUBLIC;
CREATE TEMP TABLE it_parts AS SELECT pg_temp.partition_count('itest','orders') AS orders, pg_temp.partition_count('itest','trades') AS trades;
GRANT SELECT ON it_parts TO PUBLIC;

-- ---------------------------------------------------------------------------
-- Scheduler role cases
-- ---------------------------------------------------------------------------
SET ROLE partition_job_scheduler;
DO $$
DECLARE r record; b record; j record; s mubasher_oms.cron_schedule_result; expected_next timestamp;
BEGIN
  -- C1 / S5 scheduled CREATE
  SELECT * INTO b FROM it_before WHERE job_id = 101;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(101, b.next_run_time);
  IF r.status <> 'EXECUTED' THEN RAISE EXCEPTION 'C1 status % (%)', r.status, r.message; END IF;
  SELECT * INTO j FROM mubasher_oms.partitioning_job_table WHERE job_id = 101;
  IF j.last_run_status <> 'SUCCESS' OR j.next_run_time <= clock_timestamp()::timestamp THEN RAISE EXCEPTION 'C1 row %', row_to_json(j); END IF;
  IF (SELECT count(*) FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 101 AND last_run_status = 'SUCCESS' AND execution_duration_ms IS NOT NULL) <> 1 THEN RAISE EXCEPTION 'C1 log'; END IF;
  IF pg_temp.partition_count('itest','orders') < (SELECT orders FROM it_parts) THEN RAISE EXCEPTION 'C1 partitions decreased'; END IF;
  s := mubasher_oms.cron_to_interval_or_next_run('0 0 2 * * *');
  expected_next := COALESCE(s.next_run_time, clock_timestamp()::timestamp + s.schedule_interval);
  IF s.next_run_time IS NOT NULL AND j.next_run_time <> s.next_run_time THEN RAISE EXCEPTION 'S5 next % vs %', j.next_run_time, expected_next; END IF;
  RAISE NOTICE 'ITEST C1 OK'; RAISE NOTICE 'ITEST S5 OK';

  -- C3 missed CREATE (historical)
  SELECT * INTO b FROM it_before WHERE job_id = 102;
  SELECT * INTO r FROM mubasher_oms.reconcile_missed_partition_job(102, b.next_run_time, 'MISSED_SCHEDULED_RUN', 'itest');
  IF r.status <> 'MISSED_RESCHEDULED' THEN RAISE EXCEPTION 'C3 status %', r.status; END IF;
  IF EXISTS (SELECT 1 FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 102) THEN RAISE EXCEPTION 'C3 log written'; END IF;
  IF (SELECT last_run_status FROM mubasher_oms.partitioning_job_table WHERE job_id = 102) IS NOT NULL THEN RAISE EXCEPTION 'C3 last_run_status changed'; END IF;
  RAISE NOTICE 'ITEST C3 OK'; RAISE NOTICE 'ITEST S2 OK';

  -- D1 scheduled DROP
  SELECT * INTO b FROM it_before WHERE job_id = 201;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(201, b.next_run_time);
  IF r.status <> 'EXECUTED' THEN RAISE EXCEPTION 'D1 status % (%)', r.status, r.message; END IF;
  IF pg_temp.partition_count('itest','trades') > (SELECT trades FROM it_parts) THEN RAISE EXCEPTION 'D1 partitions increased'; END IF;
  RAISE NOTICE 'ITEST D1 OK';

  -- D3 missed DROP
  SELECT * INTO b FROM it_before WHERE job_id = 202;
  SELECT * INTO r FROM mubasher_oms.reconcile_missed_partition_job(202, b.next_run_time, 'MISSED_SCHEDULED_RUN', 'itest');
  IF r.status <> 'MISSED_RESCHEDULED' THEN RAISE EXCEPTION 'D3 status %', r.status; END IF;
  RAISE NOTICE 'ITEST D3 OK';

  -- S1 future occurrence
  SELECT * INTO b FROM it_before WHERE job_id = 301;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(301, b.next_run_time);
  IF r.status <> 'SKIPPED_NOT_DUE' THEN RAISE EXCEPTION 'S1 status %', r.status; END IF;
  RAISE NOTICE 'ITEST S1 OK';

  -- SEC3 invalid db_config (scheduled)
  SELECT * INTO b FROM it_before WHERE job_id = 401;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(401, b.next_run_time);
  IF r.status <> 'FAILED' OR r.message NOT LIKE '%allowlist%' THEN RAISE EXCEPTION 'SEC3 % %', r.status, r.message; END IF;
  RAISE NOTICE 'ITEST SEC3 OK';

  -- T1 / T2 worker failure (non-existent table)
  SELECT * INTO b FROM it_before WHERE job_id = 501;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_scheduled(501, b.next_run_time);
  IF r.status <> 'FAILED' THEN RAISE EXCEPTION 'T1 status %', r.status; END IF;
  IF (SELECT count(*) FROM mubasher_oms.partitioning_job_table_log WHERE job_id = 501 AND last_run_status = 'FAIL' AND job_error IS NOT NULL) <> 1 THEN RAISE EXCEPTION 'T1 log'; END IF;
  IF (SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 501) <= b.next_run_time THEN RAISE EXCEPTION 'T1 schedule not advanced'; END IF;
  RAISE NOTICE 'ITEST T1 OK'; RAISE NOTICE 'ITEST T2 OK';

  -- SEC2 scheduler cannot run UI functions
  BEGIN PERFORM mubasher_oms.run_partition_job_attention_recovery(1); RAISE EXCEPTION 'SEC2 scheduler ran recovery';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RAISE NOTICE 'ITEST SEC1 OK (scheduler)';
END $$;
RESET ROLE;

-- ---------------------------------------------------------------------------
-- UI role cases
-- ---------------------------------------------------------------------------
SET ROLE partition_job_ui;
DO $$
DECLARE r record; nr timestamp; aid bigint;
BEGIN
  -- C2 manual CREATE
  SELECT next_run_time INTO nr FROM mubasher_oms.partitioning_job_table WHERE job_id = 101;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_manual(101);
  IF r.status <> 'MANUAL_SUCCESS' THEN RAISE EXCEPTION 'C2 % %', r.status, r.message; END IF;
  IF (SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 101) <> nr THEN RAISE EXCEPTION 'C2 moved schedule'; END IF;
  RAISE NOTICE 'ITEST C2 OK';

  -- C4 recovery CREATE
  SELECT attention_id INTO aid FROM mubasher_oms.partition_job_attention WHERE job_id = 102;
  SELECT next_run_time INTO nr FROM mubasher_oms.partitioning_job_table WHERE job_id = 102;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_attention_recovery(aid);
  IF r.result_status <> 'MANUAL_SUCCESS' OR r.attention_status <> 'MANUALLY_RERUN' THEN RAISE EXCEPTION 'C4 % %', r.result_status, r.attention_status; END IF;
  IF (SELECT next_run_time FROM mubasher_oms.partitioning_job_table WHERE job_id = 102) IS DISTINCT FROM nr THEN RAISE EXCEPTION 'C4 moved schedule'; END IF;
  RAISE NOTICE 'ITEST C4 OK';

  -- D2 manual DROP
  SELECT * INTO r FROM mubasher_oms.run_partition_job_manual(201);
  IF r.status <> 'MANUAL_SUCCESS' THEN RAISE EXCEPTION 'D2 % %', r.status, r.message; END IF;
  RAISE NOTICE 'ITEST D2 OK';

  -- D4 recovery DROP
  SELECT attention_id INTO aid FROM mubasher_oms.partition_job_attention WHERE job_id = 202;
  SELECT * INTO r FROM mubasher_oms.run_partition_job_attention_recovery(aid);
  IF r.attention_status <> 'MANUALLY_RERUN' THEN RAISE EXCEPTION 'D4 %', r.result_status; END IF;
  RAISE NOTICE 'ITEST D4 OK';

  -- T1 manual failure committed
  SELECT * INTO r FROM mubasher_oms.run_partition_job_manual(501);
  IF r.status <> 'MANUAL_FAIL' THEN RAISE EXCEPTION 'T1m %', r.status; END IF;

  -- SEC2 UI cannot run scheduler functions
  BEGIN PERFORM mubasher_oms.run_partition_job_scheduled(101, now()::timestamp); RAISE EXCEPTION 'SEC2 UI ran scheduled';
  EXCEPTION WHEN insufficient_privilege THEN NULL; END;
  RAISE NOTICE 'ITEST SEC2 OK'; RAISE NOTICE 'ITEST SEC1 OK (ui)';

  -- T3 history: one row per execution, none for skips / missed
  IF EXISTS (SELECT 1 FROM mubasher_oms.partitioning_job_table_log WHERE job_id IN (301, 202, 102) AND last_run_status IN ('SUCCESS','FAIL')) THEN
    RAISE EXCEPTION 'T3 log rows for non-executions'; END IF;
  RAISE NOTICE 'ITEST T3 OK';

  -- T4 attention: single row per occurrence, resolved once
  IF (SELECT count(*) FROM mubasher_oms.partition_job_attention WHERE job_id IN (102, 202)) <> 2 THEN RAISE EXCEPTION 'T4 rows'; END IF;
  BEGIN PERFORM mubasher_oms.acknowledge_partition_job_attention(aid, 'again'); RAISE EXCEPTION 'T4 resolved twice';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  RAISE NOTICE 'ITEST T4 OK';
END $$;
RESET ROLE;

-- S3 retry exhaustion record
SET ROLE partition_job_scheduler;
DO $$
DECLARE r record;
BEGIN
  UPDATE mubasher_oms.partitioning_job_table SET next_run_time = timestamp '2026-01-01 03:00' WHERE job_id = 301;
EXCEPTION WHEN insufficient_privilege THEN NULL;  -- scheduler may only update 3 columns; next_run_time is one of them
END $$;
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM mubasher_oms.reconcile_missed_partition_job(301, timestamp '2026-01-01 03:00', 'RETRY_EXHAUSTED', 'itest');
  IF r.status <> 'MISSED_RESCHEDULED' OR (SELECT reason FROM mubasher_oms.partition_job_attention WHERE job_id = 301) <> 'RETRY_EXHAUSTED' THEN
    RAISE EXCEPTION 'S3 % ', r.status; END IF;
  RAISE NOTICE 'ITEST S3 OK';
END $$;
RESET ROLE;
