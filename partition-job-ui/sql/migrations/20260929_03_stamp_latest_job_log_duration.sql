-- =============================================================================
-- Migration: stamp_latest_job_log_duration helper (manual-run duration path)
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Used by the application after run_partition_job_manual(...) returns, to attach
-- measured wall-clock duration to the newest log row for that job when the
-- reference manual function does not yet write execution_duration_ms itself.
--
-- Idempotent: only stamps rows where execution_duration_ms IS NULL.
--
-- Requires: 20260929_01_add_execution_duration_ms.sql
--
-- Rollback:
--   DROP FUNCTION IF EXISTS mubasher_oms.stamp_latest_job_log_duration(numeric, bigint);
-- =============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION mubasher_oms.stamp_latest_job_log_duration(
    p_job_id       numeric,
    p_duration_ms  bigint
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $function$
BEGIN
    IF p_job_id IS NULL OR p_job_id <= 0 THEN
        RAISE EXCEPTION 'stamp_latest_job_log_duration: invalid job_id %', p_job_id;
    END IF;
    IF p_duration_ms IS NULL OR p_duration_ms < 0 THEN
        RAISE EXCEPTION 'stamp_latest_job_log_duration: invalid duration_ms %', p_duration_ms;
    END IF;

    UPDATE mubasher_oms.partitioning_job_table_log AS target
       SET execution_duration_ms = p_duration_ms
     WHERE target.job_log_id = (
               SELECT l.job_log_id
                 FROM mubasher_oms.partitioning_job_table_log AS l
                WHERE l.job_id = p_job_id
                ORDER BY l.job_runtime DESC NULLS LAST, l.job_log_id DESC
                LIMIT 1
           )
       AND target.execution_duration_ms IS NULL;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.stamp_latest_job_log_duration(numeric, bigint) IS
'Attach measured duration (ms) to the newest log row for a job when still NULL. '
'Does not invent values for older history.';

COMMIT;
