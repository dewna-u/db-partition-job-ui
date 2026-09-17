-- ============================================================================
-- realtime_scheduler_v1.sql
-- ============================================================================
-- NEW DATABASE ONLY (production-copy / sensitive). Do NOT run on live reference.
--
-- Prerequisites (must already exist from bootstrap + reference-function import):
--   mubasher_oms.partitioning_job_table
--   mubasher_oms.partitioning_job_table_log
--   mubasher_oms.cron_schedule_result
--   mubasher_oms.cron_to_interval_or_next_run(text)
--   mubasher_oms.create_any_table_partition(varchar,varchar,varchar,integer,interval)
--   mubasher_oms.drop_any_table_partition(varchar,varchar,interval)
--
-- This file creates ONLY:
--   get_upcoming_partition_jobs(interval)
--   run_partition_job_scheduled(numeric, timestamp without time zone)
--
-- No LISTEN/NOTIFY. No legacy runner changes.
-- Tables must remain empty of production-copy target rows until controlled onboarding.
--
-- DO NOT RUN until:
--   1. preflight reviewed
--   2. bootstrap applied
--   3. missing reference functions imported and signature-verified
--   4. TEMP TABLE _partition_realtime_migration_approved created in-session
--   5. explicit human approval
-- ============================================================================


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
            'Refusing realtime migration: create TEMP TABLE '
            '_partition_realtime_migration_approved AS '
            'SELECT now() AS approved_at, current_database() AS approved_database; '
            'after explicit approval. Current database=%',
            current_database();
    END IF;
END
$$;


-- Dependency checks (fail closed; no function DDL until these pass)
DO $$
DECLARE
    v_missing text := '';
BEGIN
    IF to_regclass('mubasher_oms.partitioning_job_table') IS NULL THEN
        v_missing := v_missing || ' partitioning_job_table';
    END IF;
    IF to_regclass('mubasher_oms.partitioning_job_table_log') IS NULL THEN
        v_missing := v_missing || ' partitioning_job_table_log';
    END IF;
    IF to_regtype('mubasher_oms.cron_schedule_result') IS NULL THEN
        v_missing := v_missing || ' cron_schedule_result';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'mubasher_oms'
           AND p.proname = 'create_any_table_partition'
    ) THEN
        v_missing := v_missing || ' create_any_table_partition';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'mubasher_oms'
           AND p.proname = 'drop_any_table_partition'
    ) THEN
        v_missing := v_missing || ' drop_any_table_partition';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'mubasher_oms'
           AND p.proname = 'cron_to_interval_or_next_run'
    ) THEN
        v_missing := v_missing || ' cron_to_interval_or_next_run';
    END IF;

    IF v_missing <> '' THEN
        RAISE EXCEPTION
            'Realtime migration prerequisites missing:% — apply bootstrap and import '
            'authoritative reference function definitions first.',
            v_missing;
    END IF;
END
$$;


BEGIN;

-- ----------------------------------------------------------------------------
-- 1. Upcoming jobs discovery (READ ONLY)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.get_upcoming_partition_jobs(
    p_lookahead interval
)
RETURNS TABLE (
    job_id            numeric,
    job_name          character varying,
    is_create         boolean,
    expected_run_time timestamp without time zone,
    delay_seconds     double precision
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
AS $function$
DECLARE
    v_now timestamp without time zone;
BEGIN
    IF p_lookahead IS NULL OR p_lookahead <= interval '0' THEN
        RAISE EXCEPTION
            'get_upcoming_partition_jobs: p_lookahead must be NOT NULL and > 0 (got %)',
            p_lookahead;
    END IF;

    v_now := clock_timestamp()::timestamp without time zone;

    RETURN QUERY
    SELECT
        j.job_id,
        j.job_name,
        j.is_create,
        j.next_run_time AS expected_run_time,
        EXTRACT(EPOCH FROM (j.next_run_time - v_now))::double precision AS delay_seconds
    FROM mubasher_oms.partitioning_job_table AS j
    WHERE j.is_enabled IS TRUE
      AND j.next_run_time IS NOT NULL
      AND j.next_run_time <= v_now + p_lookahead
    ORDER BY j.next_run_time ASC, j.job_id ASC;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.get_upcoming_partition_jobs(interval) IS
'Read-only discovery of enabled partition jobs due within the lookahead window '
'(including overdue). Used by the realtime scheduler backend. Does not mutate data.';


-- ----------------------------------------------------------------------------
-- 2. Single scheduled occurrence executor
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_scheduled(
    p_job_id numeric,
    p_expected_run_time timestamp without time zone
)
RETURNS TABLE (
    status            text,
    job_id            numeric,
    message           text,
    next_run_time     timestamp without time zone,
    is_create         boolean
)
LANGUAGE plpgsql
SECURITY INVOKER
AS $function$
DECLARE
    v_job                 mubasher_oms.partitioning_job_table%ROWTYPE;
    v_now                 timestamp without time zone;
    v_schedule            mubasher_oms.cron_schedule_result;
    v_new_next_run        timestamp without time zone;
    v_status              text;
    v_message             text;
    v_error               text;
    v_lock_ok             boolean;
    v_cfg                 record;
    v_attempted           boolean := FALSE;
BEGIN
    v_now := clock_timestamp()::timestamp without time zone;
    v_status := 'FAILED';
    v_message := NULL;
    v_error := NULL;
    v_new_next_run := NULL;

    -- Transaction-scoped advisory lock (no persistent session required).
    v_lock_ok := pg_try_advisory_xact_lock(
        87421031,
        (abs(hashtext('partition_job_scheduled:' || p_job_id::text)) % 2147483647)::integer
    );
    IF NOT v_lock_ok THEN
        status := 'SKIPPED_LOCKED';
        job_id := p_job_id;
        message := 'Another transaction is already executing this job_id.';
        next_run_time := NULL;
        is_create := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    SELECT *
      INTO v_job
      FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = p_job_id
     FOR UPDATE;

    IF NOT FOUND THEN
        status := 'NOT_FOUND';
        job_id := p_job_id;
        message := 'Job row does not exist.';
        next_run_time := NULL;
        is_create := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_job.is_enabled IS NOT TRUE THEN
        status := 'SKIPPED_DISABLED';
        job_id := v_job.job_id;
        message := 'Job is disabled.';
        next_run_time := v_job.next_run_time;
        is_create := v_job.is_create;
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_job.next_run_time IS DISTINCT FROM p_expected_run_time THEN
        status := 'SKIPPED_RESCHEDULED';
        job_id := v_job.job_id;
        message := format(
            'Expected next_run_time %s but database has %s.',
            p_expected_run_time,
            v_job.next_run_time
        );
        next_run_time := v_job.next_run_time;
        is_create := v_job.is_create;
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_job.next_run_time > v_now THEN
        status := 'SKIPPED_NOT_DUE';
        job_id := v_job.job_id;
        message := format(
            'Job is not due yet (next_run_time=%s, now=%s).',
            v_job.next_run_time,
            v_now
        );
        next_run_time := v_job.next_run_time;
        is_create := v_job.is_create;
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_job.is_create IS NULL THEN
        status := 'FAILED';
        job_id := v_job.job_id;
        message := 'is_create is NULL; refusing to decide CREATE vs DROP.';
        next_run_time := v_job.next_run_time;
        is_create := NULL;
        UPDATE mubasher_oms.partitioning_job_table
           SET last_run_time   = v_now,
               last_run_status = 'FAIL'
         WHERE job_id = v_job.job_id;
        INSERT INTO mubasher_oms.partitioning_job_table_log (
            job_id, job_name, last_run_status, job_runtime, job_error
        ) VALUES (
            v_job.job_id, v_job.job_name, 'FAIL', v_now, message
        );
        RETURN NEXT;
        RETURN;
    END IF;

    -- Apply db_config_para for this transaction only (is_local = true).
    -- Verify against reference run_partition_create_jobs() when imported.
    BEGIN
        FOR v_cfg IN
            SELECT key, value
              FROM jsonb_each_text(COALESCE(v_job.db_config_para, '{}'::jsonb))
        LOOP
            PERFORM set_config(v_cfg.key, v_cfg.value, true);
        END LOOP;
    EXCEPTION
        WHEN OTHERS THEN
            v_error := 'Failed applying db_config_para: ' || SQLERRM;
            v_status := 'FAILED';
            v_message := v_error;
            v_attempted := TRUE;
    END;

    IF v_error IS NULL THEN
        BEGIN
            IF v_job.is_create IS TRUE THEN
                -- Exact 5-argument CREATE worker signature.
                PERFORM mubasher_oms.create_any_table_partition(
                    v_job.table_schema,
                    v_job.table_name,
                    v_job.partition_unit,
                    v_job.partition_period::integer,
                    v_job.create_drop_interval
                );
            ELSE
                -- Exact 3-argument DROP worker signature.
                PERFORM mubasher_oms.drop_any_table_partition(
                    v_job.table_schema,
                    v_job.table_name,
                    v_job.create_drop_interval
                );
            END IF;
            v_attempted := TRUE;
            v_status := 'EXECUTED';
            v_message := CASE
                WHEN v_job.is_create IS TRUE THEN 'CREATE partition operation completed.'
                ELSE 'DROP partition operation completed.'
            END;
            v_error := NULL;
        EXCEPTION
            WHEN OTHERS THEN
                v_attempted := TRUE;
                v_status := 'FAILED';
                v_message := 'Partition worker raised an error.';
                v_error := SQLERRM;
        END;
    END IF;

    -- Calculate next_run_time using authoritative cron helper + legacy CASE.
    BEGIN
        v_schedule := mubasher_oms.cron_to_interval_or_next_run(v_job.job_schedule);
        v_new_next_run := CASE
            WHEN v_schedule.schedule_interval IS NOT NULL
                THEN v_now + v_schedule.schedule_interval
            WHEN v_schedule.next_run_time IS NOT NULL
                THEN v_schedule.next_run_time
            ELSE
                v_now + interval '1 day'
        END;
    EXCEPTION
        WHEN OTHERS THEN
            -- Controlled fallback prevents uncontrolled overdue retry loops.
            v_new_next_run := v_now + interval '1 day';
            IF v_error IS NULL THEN
                v_error := 'Cron calculation failed: ' || SQLERRM
                    || ' (fallback next_run_time = now() + 1 day)';
            ELSE
                v_error := v_error
                    || ' | Cron calculation failed: ' || SQLERRM
                    || ' (fallback next_run_time = now() + 1 day)';
            END IF;
            v_status := 'FAILED';
            v_message := 'Schedule recalculation failed; applied 1-day fallback.';
    END;

    -- After an actual attempt, advance next_run_time so the same occurrence
    -- cannot tight-loop during reconciliation.
    IF NOT v_attempted AND v_error IS NOT NULL THEN
        v_attempted := TRUE;
    END IF;

    UPDATE mubasher_oms.partitioning_job_table
       SET last_run_time   = v_now,
           last_run_status = CASE
                                 WHEN v_status = 'EXECUTED' THEN 'SUCCESS'
                                 ELSE 'FAIL'
                             END,
           next_run_time   = COALESCE(v_new_next_run, v_now + interval '1 day')
     WHERE job_id = v_job.job_id;

    -- job_log_id uses column DEFAULT / sequence — do not supply it manually.
    INSERT INTO mubasher_oms.partitioning_job_table_log (
        job_id,
        job_name,
        last_run_status,
        job_runtime,
        job_error
    ) VALUES (
        v_job.job_id,
        v_job.job_name,
        CASE
            WHEN v_error IS NULL AND v_status = 'EXECUTED' THEN 'SUCCESS'
            ELSE 'FAIL'
        END,
        v_now,
        v_error
    );

    status := v_status;
    job_id := v_job.job_id;
    message := COALESCE(v_message, v_error);
    next_run_time := COALESCE(v_new_next_run, v_now + interval '1 day');
    is_create := v_job.is_create;
    RETURN NEXT;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone) IS
'Execute one scheduled partition-job occurrence after DB revalidation. '
'Uses exact create_any_table_partition / drop_any_table_partition signatures. '
'Does not replace run_partition_job_manual. Does not call legacy polling runners.';


-- ----------------------------------------------------------------------------
-- 3. Least-privilege grants (role must already exist — DBA creates it)
-- ----------------------------------------------------------------------------
-- Do NOT GRANT ALL. Do NOT grant DDL over existing business tables here.
-- Production table onboarding is a separate controlled DBA action.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_scheduler') THEN
        RAISE NOTICE
            'Role partition_job_scheduler does not exist — skipping grants. '
            'Create the role, then re-run the GRANT section manually.';
        RETURN;
    END IF;

    EXECUTE 'GRANT USAGE ON SCHEMA mubasher_oms TO partition_job_scheduler';
    EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.get_upcoming_partition_jobs(interval) TO partition_job_scheduler';
    EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone) TO partition_job_scheduler';
    EXECUTE 'GRANT SELECT ON mubasher_oms.partitioning_job_table TO partition_job_scheduler';
    EXECUTE 'GRANT UPDATE (last_run_time, last_run_status, next_run_time) ON mubasher_oms.partitioning_job_table TO partition_job_scheduler';
    EXECUTE 'GRANT INSERT ON mubasher_oms.partitioning_job_table_log TO partition_job_scheduler';
END
$$;

-- Downstream EXECUTE on workers/cron is required for SECURITY INVOKER:
--   GRANT EXECUTE ON FUNCTION mubasher_oms.create_any_table_partition(varchar, varchar, varchar, integer, interval)
--       TO partition_job_scheduler;
--   GRANT EXECUTE ON FUNCTION mubasher_oms.drop_any_table_partition(varchar, varchar, interval)
--       TO partition_job_scheduler;
--   GRANT EXECUTE ON FUNCTION mubasher_oms.cron_to_interval_or_next_run(text)
--       TO partition_job_scheduler;
-- Plus only the table-level privileges needed for explicitly onboarded targets.


-- ----------------------------------------------------------------------------
-- 4. Verification
-- ----------------------------------------------------------------------------
SELECT proname, pg_get_function_identity_arguments(oid) AS args
  FROM pg_proc
 WHERE pronamespace = 'mubasher_oms'::regnamespace
   AND proname IN (
         'get_upcoming_partition_jobs',
         'run_partition_job_scheduled'
       )
 ORDER BY 1;

COMMIT;


-- ----------------------------------------------------------------------------
-- ROLLBACK NOTES (NEW realtime objects only)
-- ----------------------------------------------------------------------------
--   DROP FUNCTION IF EXISTS mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone);
--   DROP FUNCTION IF EXISTS mubasher_oms.get_upcoming_partition_jobs(interval);
--
-- Do NOT drop legacy/reference functions or application schemas/tables.
-- Legacy runners are preserved for rollback and are NOT called by the backend.
