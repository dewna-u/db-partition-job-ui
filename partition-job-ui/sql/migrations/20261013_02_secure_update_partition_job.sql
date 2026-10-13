-- =============================================================================
-- Migration: Phase 4 secure update_data_to_partition_job_table
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Root cause of Edit/Enable/Disable persistence failures:
--   Prior function was SECURITY INVOKER with no OWNER/GRANT hardening, so
--   partition_job_ui needed direct UPDATE on partitioning_job_table (or the
--   call failed). EXECUTE grant was commented out. No row lock / server-side
--   next_run_time safety on re-enable.
--
-- This migration:
--   * SECURITY DEFINER + OWNER mubasher_oms
--   * fixed search_path (pg_temp last)
--   * REVOKE PUBLIC; GRANT EXECUTE TO partition_job_ui
--   * SELECT … FOR UPDATE
--   * disable preserves next/last_run columns (never clears history)
--   * re-enable / schedule change recalculates ONE future next_run_time via
--     cron_to_interval_or_next_run (fail closed; no +1 day / frequency invent)
--   * same signature / RETURNS void (API compatible)
--
-- Does NOT GRANT UPDATE ON partitioning_job_table TO partition_job_ui.
-- =============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION mubasher_oms.update_data_to_partition_job_table(
    p_job_id                   numeric,
    p_job_name                 character varying,
    p_is_enabled               boolean,
    p_table_schema             character varying,
    p_table_name               character varying,
    p_db_config_para           jsonb,
    p_job_schedule             character varying,
    p_frequency                interval,
    p_next_run_time            timestamp without time zone,
    p_partition_unit           character varying,
    p_partition_period         numeric,
    p_is_create                boolean,
    p_is_create_drop_interval  interval
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_job              mubasher_oms.partitioning_job_table%ROWTYPE;
    v_now              timestamp without time zone;
    v_schedule         mubasher_oms.cron_schedule_result;
    v_effective_next   timestamp without time zone;
    v_need_recalc      boolean := FALSE;
    v_schedule_changed boolean := FALSE;
BEGIN
    IF p_job_id IS NULL OR p_job_id <= 0 THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: invalid job_id %', p_job_id
            USING ERRCODE = '22023';
    END IF;

    IF p_job_name IS NULL OR btrim(p_job_name) = '' THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: job_name is required'
            USING ERRCODE = '22023';
    END IF;

    IF p_table_schema IS NULL OR btrim(p_table_schema) = '' THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: table_schema is required'
            USING ERRCODE = '22023';
    END IF;

    IF p_table_name IS NULL OR btrim(p_table_name) = '' THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: table_name is required'
            USING ERRCODE = '22023';
    END IF;

    IF p_job_schedule IS NULL OR btrim(p_job_schedule) = '' THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: job_schedule is required'
            USING ERRCODE = '22023';
    END IF;

    IF p_partition_unit IS NULL OR btrim(p_partition_unit) = '' THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: partition_unit is required'
            USING ERRCODE = '22023';
    END IF;

    IF p_partition_period IS NULL OR p_partition_period <= 0 THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: partition_period must be positive'
            USING ERRCODE = '22023';
    END IF;

    IF p_is_create IS NULL THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: is_create must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    IF p_is_create_drop_interval IS NULL THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: create_drop_interval is required'
            USING ERRCODE = '22023';
    END IF;

    IF p_frequency IS NULL THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: frequency is required'
            USING ERRCODE = '22023';
    END IF;

    IF p_db_config_para IS NULL THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: db_config_para must be JSONB (use {})'
            USING ERRCODE = '22023';
    END IF;

    IF jsonb_typeof(p_db_config_para) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: db_config_para must be a JSON object'
            USING ERRCODE = '22023';
    END IF;

    -- Lock the configuration row for the duration of this transaction.
    SELECT *
      INTO v_job
      FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = p_job_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'update_data_to_partition_job_table: job_id % not found', p_job_id
            USING ERRCODE = 'P0002';
    END IF;

    v_now := clock_timestamp()::timestamp without time zone;
    v_schedule_changed := (p_job_schedule IS DISTINCT FROM v_job.job_schedule);

    -- Disable (or stay disabled without schedule change): preserve next_run_time.
    -- Re-enable / schedule change / enabled+stale: recalculate ONE future occurrence.
    IF p_is_enabled IS NOT TRUE THEN
        IF v_schedule_changed THEN
            v_need_recalc := TRUE;
        ELSE
            v_effective_next := v_job.next_run_time;
        END IF;
    ELSE
        IF v_schedule_changed
           OR v_job.is_enabled IS NOT TRUE
           OR v_job.next_run_time IS NULL
           OR v_job.next_run_time <= v_now
           OR p_next_run_time IS NULL
           OR p_next_run_time <= v_now THEN
            v_need_recalc := TRUE;
        ELSE
            v_effective_next := p_next_run_time;
        END IF;
    END IF;

    IF v_need_recalc THEN
        BEGIN
            v_schedule := mubasher_oms.cron_to_interval_or_next_run(p_job_schedule);
            v_effective_next := CASE
                WHEN v_schedule.schedule_interval IS NOT NULL
                    THEN v_now + v_schedule.schedule_interval
                WHEN v_schedule.next_run_time IS NOT NULL
                    THEN v_schedule.next_run_time
                ELSE
                    NULL
            END;
        EXCEPTION
            WHEN OTHERS THEN
                RAISE EXCEPTION
                    'update_data_to_partition_job_table: schedule calculation failed (%). '
                    'Update rolled back; previous configuration unchanged.',
                    SQLERRM
                    USING ERRCODE = '22023';
        END;

        IF v_effective_next IS NULL
           OR v_effective_next <= v_now THEN
            RAISE EXCEPTION
                'update_data_to_partition_job_table: could not calculate a future '
                'next_run_time for schedule % (calculated=%, now=%). '
                'Fail closed; previous configuration unchanged.',
                p_job_schedule,
                v_effective_next,
                v_now
                USING ERRCODE = '22023';
        END IF;
    END IF;

    UPDATE mubasher_oms.partitioning_job_table AS j
       SET job_name             = btrim(p_job_name),
           is_enabled           = p_is_enabled,
           table_schema         = btrim(p_table_schema),
           table_name           = btrim(p_table_name),
           db_config_para       = p_db_config_para,
           job_schedule         = btrim(p_job_schedule),
           frequency            = p_frequency,
           next_run_time        = v_effective_next,
           partition_unit       = btrim(p_partition_unit),
           partition_period     = p_partition_period,
           is_create            = p_is_create,
           create_drop_interval = p_is_create_drop_interval
     WHERE j.job_id = p_job_id;

    -- Explicitly never touch: job_id, last_run_time, last_run_status, logs.
END;
$function$;

ALTER FUNCTION mubasher_oms.update_data_to_partition_job_table(
    numeric, character varying, boolean, character varying, character varying,
    jsonb, character varying, interval, timestamp without time zone,
    character varying, numeric, boolean, interval
) OWNER TO mubasher_oms;

REVOKE ALL ON FUNCTION mubasher_oms.update_data_to_partition_job_table(
    numeric, character varying, boolean, character varying, character varying,
    jsonb, character varying, interval, timestamp without time zone,
    character varying, numeric, boolean, interval
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION mubasher_oms.update_data_to_partition_job_table(
    numeric, character varying, boolean, character varying, character varying,
    jsonb, character varying, interval, timestamp without time zone,
    character varying, numeric, boolean, interval
) TO partition_job_ui;

COMMENT ON FUNCTION mubasher_oms.update_data_to_partition_job_table(
    numeric, character varying, boolean, character varying, character varying,
    jsonb, character varying, interval, timestamp without time zone,
    character varying, numeric, boolean, interval
) IS
'Secure config update for one partitioning_job_table row (SECURITY DEFINER, '
'owner mubasher_oms, fixed search_path). Locks row FOR UPDATE. Never changes '
'job_id, last_run_time, last_run_status, or logs. Disable preserves '
'next_run_time. Re-enable/schedule change recalculates one future '
'next_run_time via cron_to_interval_or_next_run (fail closed; no +1 day). '
'Callers need EXECUTE only — not table UPDATE.';

COMMIT;
