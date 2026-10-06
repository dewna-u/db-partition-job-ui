-- =============================================================================
-- Migration: run_partition_job_manual stores duration atomically and
--            returns a structured result so MANUAL_FAIL can COMMIT
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Prerequisite: 20260929_01_add_execution_duration_ms.sql
--
-- PostgreSQL cannot CREATE OR REPLACE a function to change RETURNS void
-- into RETURNS TABLE. This migration therefore:
--
--   1. Inspects that only the (numeric) signature exists (no extra overloads
--      in this product).
--   2. DROP FUNCTION mubasher_oms.run_partition_job_manual(numeric)
--      WITHOUT CASCADE so unexpected dependents fail the migration loudly.
--   3. CREATE FUNCTION ... RETURNS TABLE(status, message, execution_duration_ms)
--   4. ALTER FUNCTION ... OWNER TO mubasher_oms
--   5. GRANT EXECUTE ... TO partition_job_ui  (not GRANT ALL)
--
-- Source-tree callers: the UI/API SELECT only. There is no SQL wrapper that
-- depends on the void signature. DROP drops owner and privileges, so both
-- must be restored after CREATE.
--
-- Behaviour:
--   * fetch job row (missing job still RAISE — no attempt, no log)
--   * apply db_config_para with set_config(..., is_local = true)
--   * CREATE -> create_any_table_partition(varchar, varchar, varchar, integer, interval)
--   * DROP  -> drop_any_table_partition(varchar, varchar, interval)
--   * success: INSERT MANUAL_SUCCESS + duration, RETURN status MANUAL_SUCCESS
--   * failure: INSERT MANUAL_FAIL + duration, RETURN status MANUAL_FAIL
--     (do NOT RAISE — that would abort the transaction and lose the log)
--   * does NOT change next_run_time / last_run_time / last_run_status
--
-- Duration is measured around config apply + worker, then written on the SAME
-- INSERT as the log row. There is no "stamp latest log" helper.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    v_dependents integer;
BEGIN
    -- Other objects that normally depend on this function (views, other
    -- functions, etc.). Internal pg_proc/extension links use other deptypes.
    SELECT count(*)
      INTO v_dependents
      FROM pg_catalog.pg_proc AS p
      JOIN pg_catalog.pg_namespace AS n
        ON n.oid = p.pronamespace
      JOIN pg_catalog.pg_depend AS d
        ON d.refobjid = p.oid
       AND d.deptype = 'n'
     WHERE n.nspname = 'mubasher_oms'
       AND p.proname = 'run_partition_job_manual'
       AND pg_catalog.pg_get_function_identity_arguments(p.oid) = 'numeric';

    IF v_dependents > 0 THEN
        RAISE EXCEPTION
            'run_partition_job_manual(numeric) has % dependent object(s); '
            'refusing DROP without CASCADE. Inspect pg_depend before migrating.',
            v_dependents;
    END IF;
END;
$$;

DROP FUNCTION IF EXISTS mubasher_oms.run_partition_job_manual(numeric);

CREATE FUNCTION mubasher_oms.run_partition_job_manual(
    p_job_id numeric
)
RETURNS TABLE (
    status text,
    message text,
    execution_duration_ms bigint
)
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

        status := 'MANUAL_SUCCESS';
        message := CASE
            WHEN v_job.is_create IS TRUE THEN
                'CREATE partition operation completed.'
            ELSE
                'DROP partition operation completed.'
        END;
        execution_duration_ms := v_duration_ms;
        RETURN NEXT;
        RETURN;
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
            -- Do not RAISE: the caller must be able to COMMIT this log row.
            status := 'MANUAL_FAIL';
            message := v_error;
            execution_duration_ms := v_duration_ms;
            RETURN NEXT;
            RETURN;
    END;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.run_partition_job_manual(numeric) IS
'Execute one configured partition job immediately. Writes MANUAL_SUCCESS or '
'MANUAL_FAIL with execution_duration_ms on the same log INSERT. Returns '
'status/message/duration; MANUAL_FAIL does not RAISE so the caller can COMMIT. '
'Does not change next_run_time, last_run_time, or last_run_status.';

ALTER FUNCTION mubasher_oms.run_partition_job_manual(numeric)
    OWNER TO mubasher_oms;

GRANT EXECUTE ON FUNCTION
    mubasher_oms.run_partition_job_manual(numeric)
    TO partition_job_ui;

COMMIT;
