-- =============================================================================
-- DISPOSABLE TEST DATABASE ONLY — TEST-ONLY STUB CRON HELPER (NOT PRODUCTION)
-- =============================================================================
-- Installed exclusively by scripts/disposable_db/run_disposable_db_test.sh into
-- the THROWAWAY control database so schedule finalization can be exercised.
-- It is NOT the production cron_to_interval_or_next_run, NOT a reconstruction
-- of it, and validates nothing about real cron parsing (Phase 5B.1 deferred).
-- Behaviour (synthetic): every schedule means "every 1 day" (interval form).
-- Never copy into sql/migrations, the manifest, runtime or deployment SQL.
-- =============================================================================
\set ON_ERROR_STOP 1

CREATE OR REPLACE FUNCTION mubasher_oms.cron_to_interval_or_next_run(p_schedule text)
RETURNS mubasher_oms.cron_schedule_result
LANGUAGE sql
AS $function$
    -- TEST-ONLY STUB (disposable databases only).
    SELECT ROW(interval '1 day', NULL::timestamp without time zone)::mubasher_oms.cron_schedule_result
$function$;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['partition_job_scheduler', 'partition_job_ui'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION mubasher_oms.cron_to_interval_or_next_run(text) TO %I', r);
        END IF;
    END LOOP;
END
$$;
