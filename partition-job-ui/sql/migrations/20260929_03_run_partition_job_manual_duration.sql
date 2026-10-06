-- =============================================================================
-- Migration: run_partition_job_manual stores execution_duration_ms atomically
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Prerequisite: 20260929_01_add_execution_duration_ms.sql
--
-- CREATE OR REPLACE of the existing authoritative function:
--   mubasher_oms.run_partition_job_manual(numeric) RETURNS void
--
-- Behaviour preserved:
--   * fetch job row
--   * apply db_config_para with set_config(..., is_local = true)
--   * CREATE -> create_any_table_partition(varchar, varchar, varchar, integer, interval)
--   * DROP  -> drop_any_table_partition(varchar, varchar, interval)
--   * success log status = MANUAL_SUCCESS
--   * failure log status = MANUAL_FAIL
--   * does NOT change next_run_time / last_run_time / last_run_status
--   * RAISE after MANUAL_FAIL log insert
--
-- Duration is measured around config apply + worker, then written on the SAME
-- INSERT as the log row. There is no "stamp latest log" helper.
--
-- Rollback: restore the previous CREATE OR REPLACE body of
--   mubasher_oms.run_partition_job_manual(numeric)
--   from the reference database (without execution_duration_ms in the INSERT).
-- =============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_manual(
    p_job_id numeric
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $function$
DECLARE
    v_job          mubasher_oms.partitioning_job_table%ROWTYPE;
    v_cfg          record;
    v_exec_start   timestamptz;
    v_duration_ms  bigint;
    v_error        text;
BEGIN
    SELECT *
      INTO v_job
      FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = p_job_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'run_partition_job_manual: job_id % not found', p_job_id;
    END IF;

    -- Wall-clock of the actual attempt (config apply + CREATE/DROP), not queue wait.
    v_exec_start := clock_timestamp();

    BEGIN
        FOR v_cfg IN
            SELECT key, value
              FROM jsonb_each_text(COALESCE(v_job.db_config_para, '{}'::jsonb))
        LOOP
            PERFORM set_config(v_cfg.key, v_cfg.value, true);
        END LOOP;

        IF v_job.is_create IS TRUE THEN
            PERFORM mubasher_oms.create_any_table_partition(
                v_job.table_schema,
                v_job.table_name,
                v_job.partition_unit,
                v_job.partition_period::integer,
                v_job.create_drop_interval
            );
        ELSIF v_job.is_create IS FALSE THEN
            PERFORM mubasher_oms.drop_any_table_partition(
                v_job.table_schema,
                v_job.table_name,
                v_job.create_drop_interval
            );
        ELSE
            RAISE EXCEPTION
                'is_create is NULL; refusing to decide CREATE vs DROP.';
        END IF;

        v_duration_ms := GREATEST(
            0,
            (
                EXTRACT(EPOCH FROM (clock_timestamp() - v_exec_start)) * 1000
            )::bigint
        );

        INSERT INTO mubasher_oms.partitioning_job_table_log (
            job_id,
            job_name,
            last_run_status,
            job_runtime,
            job_error,
            execution_duration_ms
        ) VALUES (
            v_job.job_id,
            v_job.job_name,
            'MANUAL_SUCCESS',
            clock_timestamp()::timestamp without time zone,
            NULL,
            v_duration_ms
        );
    EXCEPTION
        WHEN OTHERS THEN
            v_error := SQLERRM;
            v_duration_ms := GREATEST(
                0,
                (
                    EXTRACT(EPOCH FROM (clock_timestamp() - v_exec_start)) * 1000
                )::bigint
            );
            INSERT INTO mubasher_oms.partitioning_job_table_log (
                job_id,
                job_name,
                last_run_status,
                job_runtime,
                job_error,
                execution_duration_ms
            ) VALUES (
                v_job.job_id,
                v_job.job_name,
                'MANUAL_FAIL',
                clock_timestamp()::timestamp without time zone,
                v_error,
                v_duration_ms
            );
            RAISE;
    END;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.run_partition_job_manual(numeric) IS
'Execute one configured partition job immediately. Writes MANUAL_SUCCESS or '
'MANUAL_FAIL with execution_duration_ms on the same log INSERT. Does not '
'change next_run_time, last_run_time, or last_run_status.';

COMMIT;
