-- ============================================================================
-- bootstrap_partition_job_framework.sql
-- ============================================================================
-- NEW DATABASE ONLY (production-copy / sensitive application data present).
--
-- DO NOT RUN until:
--   1. sql/preflight_realtime_database.sql was reviewed
--   2. REALTIME_DB_EXPECTED_NAME matches current_database()
--   3. PARTITION_REALTIME_ALLOW_MIGRATION=true is set for tooling (optional)
--   4. Explicit human approval was given
--   5. In-session approval marker TEMP TABLE was created (see below)
--
-- This script creates ONLY the partition-job framework objects that can be
-- defined from authoritative project material. It does NOT:
--   * DROP / TRUNCATE / DELETE application data
--   * DROP SCHEMA mubasher_oms
--   * recreate the whole application schema
--   * invent missing partition-worker / cron / runner function bodies
--   * insert any configuration rows (tables start EMPTY)
--
-- After this script, import missing reference functions, then apply
-- sql/realtime_scheduler_v1.sql separately.
-- ============================================================================


-- Safety: refuse accidental apply without an explicit session approval marker.
-- After human approval, in the same session run:
--   CREATE TEMP TABLE _partition_realtime_migration_approved AS
--   SELECT now() AS approved_at, current_database() AS approved_database;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relname = '_partition_realtime_migration_approved'
           AND n.nspname LIKE 'pg_temp%'
           AND c.relkind = 'r'
    ) THEN
        RAISE EXCEPTION
            'Refusing bootstrap: create TEMP TABLE '
            '_partition_realtime_migration_approved AS '
            'SELECT now() AS approved_at, current_database() AS approved_database; '
            'in this session after explicit approval. Current database=%',
            current_database();
    END IF;
END
$$;


BEGIN;

-- ----------------------------------------------------------------------------
-- 1. Schema — create ONLY if missing. Never replace an existing schema.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regnamespace('mubasher_oms') IS NULL THEN
        EXECUTE 'CREATE SCHEMA mubasher_oms';
        RAISE NOTICE 'Created schema mubasher_oms';
    ELSE
        RAISE NOTICE 'Schema mubasher_oms already exists — leaving owner/objects untouched';
    END IF;
END
$$;


-- ----------------------------------------------------------------------------
-- 2. Sequences
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('mubasher_oms.seq_partitioning_job_id') IS NOT NULL THEN
        RAISE EXCEPTION
            'Conflict: mubasher_oms.seq_partitioning_job_id already exists. '
            'Compare definition before continuing.';
    END IF;
    IF to_regclass('mubasher_oms.seq_partitioning_job_log_id') IS NOT NULL THEN
        RAISE EXCEPTION
            'Conflict: mubasher_oms.seq_partitioning_job_log_id already exists. '
            'Compare definition before continuing.';
    END IF;
END
$$;

CREATE SEQUENCE mubasher_oms.seq_partitioning_job_id;
CREATE SEQUENCE mubasher_oms.seq_partitioning_job_log_id;


-- ----------------------------------------------------------------------------
-- 3. Tables (authoritative column lists from project specification)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('mubasher_oms.partitioning_job_table') IS NOT NULL THEN
        RAISE EXCEPTION
            'Conflict: mubasher_oms.partitioning_job_table already exists. '
            'Do not overwrite. Inspect columns and stop.';
    END IF;
    IF to_regclass('mubasher_oms.partitioning_job_table_log') IS NOT NULL THEN
        RAISE EXCEPTION
            'Conflict: mubasher_oms.partitioning_job_table_log already exists. '
            'Do not overwrite. Inspect columns and stop.';
    END IF;
END
$$;

CREATE TABLE mubasher_oms.partitioning_job_table (
    job_id               numeric NOT NULL
        DEFAULT nextval('mubasher_oms.seq_partitioning_job_id'::regclass),
    job_name             character varying,
    is_enabled           boolean,
    table_schema         character varying,
    table_name           character varying,
    db_config_para       jsonb NOT NULL DEFAULT '{}'::jsonb,
    frequency            interval,
    last_run_time        timestamp without time zone,
    next_run_time        timestamp without time zone,
    last_run_status      character varying,
    partition_unit       character varying,
    partition_period     numeric,
    is_create            boolean,
    create_drop_interval interval,
    job_schedule         character varying,
    CONSTRAINT partitioning_job_table_pkey PRIMARY KEY (job_id)
);

COMMENT ON COLUMN mubasher_oms.partitioning_job_table.frequency IS
    'Authoritative schedule interval column name is frequency.';

CREATE TABLE mubasher_oms.partitioning_job_table_log (
    job_log_id      integer NOT NULL
        DEFAULT nextval('mubasher_oms.seq_partitioning_job_log_id'::regclass),
    job_id          numeric,
    job_name        character varying,
    last_run_status character varying,
    job_runtime     timestamp without time zone,
    job_error       character varying,
    CONSTRAINT partitioning_job_table_log_pkey PRIMARY KEY (job_log_id)
);

ALTER SEQUENCE mubasher_oms.seq_partitioning_job_id
    OWNED BY mubasher_oms.partitioning_job_table.job_id;
ALTER SEQUENCE mubasher_oms.seq_partitioning_job_log_id
    OWNED BY mubasher_oms.partitioning_job_table_log.job_log_id;


-- ----------------------------------------------------------------------------
-- 4. Indexes for scheduler discovery
-- ----------------------------------------------------------------------------
CREATE INDEX partitioning_job_table_enabled_next_run_idx
    ON mubasher_oms.partitioning_job_table (is_enabled, next_run_time, job_id);

CREATE INDEX partitioning_job_table_log_job_id_runtime_idx
    ON mubasher_oms.partitioning_job_table_log (job_id, job_runtime DESC);


-- ----------------------------------------------------------------------------
-- 5. Composite type used by cron_to_interval_or_next_run
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regtype('mubasher_oms.cron_schedule_result') IS NOT NULL THEN
        RAISE EXCEPTION
            'Conflict: mubasher_oms.cron_schedule_result already exists.';
    END IF;
END
$$;

CREATE TYPE mubasher_oms.cron_schedule_result AS (
    schedule_interval interval,
    next_run_time     timestamp without time zone
);


-- ----------------------------------------------------------------------------
-- 6. db_config_para normalisation trigger (authoritative body from project SQL)
--    Source: ../sql/remove_default_lock_timeout_from_db_config.sql
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.fn_set_default_db_config_para()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    IF NEW.db_config_para IS NULL THEN
        NEW.db_config_para := '{}'::jsonb;
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION mubasher_oms.fn_set_default_db_config_para() IS
    'Normalises partitioning_job_table.db_config_para from NULL to an empty '
    'JSON object. Never injects default settings.';

CREATE TRIGGER trg_set_default_db_config_para
    BEFORE INSERT OR UPDATE OF db_config_para
    ON mubasher_oms.partitioning_job_table
    FOR EACH ROW
    EXECUTE PROCEDURE mubasher_oms.fn_set_default_db_config_para();


-- ----------------------------------------------------------------------------
-- 7. HARD STOP — remaining baseline functions are NOT invented here
-- ----------------------------------------------------------------------------
-- MISSING from this repository (must import from REFERENCE DB via
-- sql/capture_reference_framework_functions.sql):
--
--   cron_field_to_int_array(...)
--   cron_to_interval_or_next_run(p_cron_expression text)
--       RETURNS mubasher_oms.cron_schedule_result
--   create_any_table_partition(
--       p_schema_name varchar, p_table_name varchar,
--       p_part_unit varchar, p_part_period integer,
--       p_create_interval interval) RETURNS void
--   drop_any_table_partition(
--       p_schema_name varchar, p_table_name varchar,
--       p_create_drop_interval interval) RETURNS void
--   insert_data_to_partition_job_table(... 12 args ...)
--   run_partition_job_manual(numeric)
--   run_partition_create_jobs()
--   run_partition_drop_jobs()
--
-- Do NOT invent placeholder bodies.

DO $$
BEGIN
    RAISE NOTICE
        'Bootstrap created tables/sequences/type/trigger only. '
        'Import missing framework functions from the reference database before '
        'applying realtime_scheduler_v1.sql. Tables are EMPTY by design.';
END
$$;


DO $$
DECLARE
    v_jobs bigint;
    v_logs bigint;
BEGIN
    SELECT count(*) INTO v_jobs FROM mubasher_oms.partitioning_job_table;
    SELECT count(*) INTO v_logs FROM mubasher_oms.partitioning_job_table_log;
    IF v_jobs <> 0 OR v_logs <> 0 THEN
        RAISE EXCEPTION
            'Unexpected non-empty framework tables after bootstrap (jobs=%, logs=%).',
            v_jobs, v_logs;
    END IF;
END
$$;

COMMIT;
