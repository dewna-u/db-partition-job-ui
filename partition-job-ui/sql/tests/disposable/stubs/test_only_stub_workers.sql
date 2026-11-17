-- =============================================================================
-- DISPOSABLE TEST DATABASE ONLY — TEST-ONLY STUB WORKERS (NOT PRODUCTION CODE)
-- =============================================================================
-- Installed exclusively by scripts/disposable_db/run_disposable_db_test.sh into
-- THROWAWAY databases (control + managed targets) to exercise PartOps routing,
-- target COMMIT / ROLLBACK, locking, unknown-outcome handling, concurrency,
-- history and schedule finalization.
--
-- These are NOT the production partition workers, NOT a reconstruction of
-- them, and they validate NOTHING about production worker business logic
-- (Phase 5B.1 remains deferred; the authoritative definitions exist only in
-- production). They must never be copied into sql/migrations, the framework
-- manifest, runtime source or any deployment SQL (enforced by tests).
--
-- Behaviour (synthetic):
--   * records each call in partops_test.worker_calls of THIS database
--   * table names starting with 'fail' raise (proven rollback path)
--   * table names starting with 'slow' sleep 2 s (concurrency / locking)
-- =============================================================================
\set ON_ERROR_STOP 1

CREATE SCHEMA IF NOT EXISTS mubasher_oms;
CREATE SCHEMA IF NOT EXISTS partops_test;
CREATE TABLE IF NOT EXISTS partops_test.worker_calls (
    call_id      bigserial PRIMARY KEY,
    db           name NOT NULL DEFAULT current_database(),
    operation    text NOT NULL,
    table_schema text,
    table_name   text,
    started_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at  timestamptz
);

CREATE OR REPLACE FUNCTION partops_test.stub_work(p_op text, p_schema text, p_table text)
RETURNS void
LANGUAGE plpgsql
AS $function$
DECLARE
    v_id bigint;
BEGIN
    INSERT INTO partops_test.worker_calls (operation, table_schema, table_name)
    VALUES (p_op, p_schema, p_table) RETURNING call_id INTO v_id;
    IF p_table LIKE 'slow%' THEN
        PERFORM pg_sleep(2);
    END IF;
    IF p_table LIKE 'fail%' THEN
        RAISE EXCEPTION 'TEST-ONLY stub worker failure for %.%', p_schema, p_table;
    END IF;
    UPDATE partops_test.worker_calls SET finished_at = clock_timestamp() WHERE call_id = v_id;
END;
$function$;

CREATE OR REPLACE FUNCTION mubasher_oms.create_any_table_partition(
    p_schema character varying, p_table character varying, p_unit character varying,
    p_period integer, p_interval interval)
RETURNS void
LANGUAGE plpgsql
AS $function$
BEGIN
    -- TEST-ONLY STUB (disposable databases only).
    PERFORM partops_test.stub_work('CREATE', p_schema, p_table);
END;
$function$;

CREATE OR REPLACE FUNCTION mubasher_oms.drop_any_table_partition(
    p_schema character varying, p_table character varying, p_interval interval)
RETURNS void
LANGUAGE plpgsql
AS $function$
BEGIN
    -- TEST-ONLY STUB (disposable databases only).
    PERFORM partops_test.stub_work('DROP', p_schema, p_table);
END;
$function$;

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['partition_job_scheduler', 'partition_job_ui'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('GRANT USAGE ON SCHEMA mubasher_oms, partops_test TO %I', r);
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON partops_test.worker_calls TO %I', r);
            EXECUTE format('GRANT USAGE ON SEQUENCE partops_test.worker_calls_call_id_seq TO %I', r);
            EXECUTE format('GRANT EXECUTE ON FUNCTION mubasher_oms.create_any_table_partition(character varying, character varying, character varying, integer, interval) TO %I', r);
            EXECUTE format('GRANT EXECUTE ON FUNCTION mubasher_oms.drop_any_table_partition(character varying, character varying, interval) TO %I', r);
        END IF;
    END LOOP;
END
$$;
