-- =============================================================================
-- Migration: update_data_to_partition_job_table (config edit path)
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Mirrors insert_data_to_partition_job_table for configuration updates.
-- Does NOT modify: job_id, last_run_time, last_run_status, or log history.
--
-- Rollback:
--   DROP FUNCTION IF EXISTS mubasher_oms.update_data_to_partition_job_table(
--     numeric, character varying, boolean, character varying, character varying,
--     jsonb, character varying, interval, timestamp without time zone,
--     character varying, numeric, boolean, interval
--   );
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
SECURITY INVOKER
AS $function$
DECLARE
    v_updated integer;
BEGIN
    IF p_job_id IS NULL OR p_job_id <= 0 THEN
        RAISE EXCEPTION 'update_data_to_partition_job_table: invalid job_id %', p_job_id;
    END IF;

    UPDATE mubasher_oms.partitioning_job_table
       SET job_name             = p_job_name,
           is_enabled           = p_is_enabled,
           table_schema         = p_table_schema,
           table_name           = p_table_name,
           db_config_para       = p_db_config_para,
           job_schedule         = p_job_schedule,
           frequency            = p_frequency,
           next_run_time        = p_next_run_time,
           partition_unit       = p_partition_unit,
           partition_period     = p_partition_period,
           is_create            = p_is_create,
           create_drop_interval = p_is_create_drop_interval
     WHERE job_id = p_job_id;

    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated = 0 THEN
        RAISE EXCEPTION 'update_data_to_partition_job_table: job_id % not found', p_job_id
            USING ERRCODE = 'P0002';
    END IF;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.update_data_to_partition_job_table(
    numeric, character varying, boolean, character varying, character varying,
    jsonb, character varying, interval, timestamp without time zone,
    character varying, numeric, boolean, interval
) IS
'Update configuration columns of one partitioning_job_table row. '
'Never touches job_id, last_run_time, last_run_status, or execution logs.';

-- Optional app-role grant (role must already exist):
-- GRANT EXECUTE ON FUNCTION mubasher_oms.update_data_to_partition_job_table(
--     numeric, character varying, boolean, character varying, character varying,
--     jsonb, character varying, interval, timestamp without time zone,
--     character varying, numeric, boolean, interval
-- ) TO partition_job_ui;

COMMIT;
