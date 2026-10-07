-- =============================================================================
-- Migration: Phase 0 scheduled-occurrence safety (hardened fail-closed)
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Prerequisite: mubasher_oms.run_partition_job_scheduled already deployed
-- (same return type — CREATE OR REPLACE; no DROP).
-- Authoritative body kept in sync with sql/realtime_scheduler_v1.sql.
--
-- Hardening:
--   * Validate next_run_time BEFORE CREATE/DROP
--   * Reject NULL / same-as-T / <= T / <= now
--   * Fail closed: next_run_time = NULL, no worker, no invented +1 day/frequency
--   * Operator must correct schedule before rediscovery
-- =============================================================================

BEGIN;
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
    v_exec_start          timestamptz;
    v_duration_ms         bigint;
BEGIN
    v_now := clock_timestamp()::timestamp without time zone;
    v_status := 'FAILED';
    v_message := NULL;
    v_error := NULL;
    v_new_next_run := NULL;
    v_duration_ms := NULL;

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

    -- Recalculate after FOR UPDATE so due-time checks use post-lock time.
    v_now := clock_timestamp()::timestamp without time zone;

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
        -- Fail closed: clear next_run_time so get_upcoming cannot rediscover.
        -- Do not invent a schedule (no +1 day / frequency fallback).
        status := 'FAILED_INVALID_SCHEDULE';
        job_id := v_job.job_id;
        message := 'is_create is NULL; refusing to decide CREATE vs DROP.';
        next_run_time := NULL;
        is_create := NULL;
        UPDATE mubasher_oms.partitioning_job_table AS j
           SET last_run_time   = v_now,
               last_run_status = 'FAIL',
               next_run_time   = NULL
         WHERE j.job_id = v_job.job_id;
        INSERT INTO mubasher_oms.partitioning_job_table_log (
            job_id, job_name, last_run_status, job_runtime, job_error,
            execution_duration_ms
        ) VALUES (
            v_job.job_id, v_job.job_name, 'FAIL', v_now, message, NULL
        );
        RETURN NEXT;
        RETURN;
    END IF;

    -- Calculate and VALIDATE the next occurrence BEFORE any CREATE/DROP worker.
    -- Overdue semantics: execute occurrence T at most once, then require a
    -- future next N. PartOps does not invent arbitrary dates (+1 day / frequency)
    -- when the cron helper cannot produce a valid next state.
    BEGIN
        v_schedule := mubasher_oms.cron_to_interval_or_next_run(v_job.job_schedule);
        v_new_next_run := CASE
            WHEN v_schedule.schedule_interval IS NOT NULL
                THEN v_now + v_schedule.schedule_interval
            WHEN v_schedule.next_run_time IS NOT NULL
                THEN v_schedule.next_run_time
            ELSE
                NULL
        END;
    EXCEPTION
        WHEN OTHERS THEN
            v_new_next_run := NULL;
            v_error := 'Cron calculation failed: ' || SQLERRM;
    END;

    IF v_new_next_run IS NULL
       OR v_new_next_run IS NOT DISTINCT FROM p_expected_run_time
       OR v_new_next_run <= p_expected_run_time
       OR v_new_next_run <= v_now THEN
        status := 'FAILED_INVALID_SCHEDULE';
        job_id := v_job.job_id;
        message := COALESCE(
            v_error,
            format(
                'Invalid next_run_time from schedule helper '
                || '(calculated=%s, expected_occurrence=%s, now=%s). '
                || 'Fail closed: no CREATE/DROP; next_run_time cleared for operator fix.',
                v_new_next_run,
                p_expected_run_time,
                v_now
            )
        );
        next_run_time := NULL;
        is_create := v_job.is_create;
        UPDATE mubasher_oms.partitioning_job_table AS j
           SET last_run_time   = v_now,
               last_run_status = 'FAIL',
               next_run_time   = NULL
         WHERE j.job_id = v_job.job_id;
        INSERT INTO mubasher_oms.partitioning_job_table_log (
            job_id, job_name, last_run_status, job_runtime, job_error,
            execution_duration_ms
        ) VALUES (
            v_job.job_id, v_job.job_name, 'FAIL', v_now, message, NULL
        );
        RETURN NEXT;
        RETURN;
    END IF;

    -- Measure wall-clock duration of config apply + partition worker only
    -- (not queue wait). Recorded for both SUCCESS and FAIL attempts.
    v_exec_start := clock_timestamp();

    -- Apply db_config_para for this transaction only (is_local = true).
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
                PERFORM mubasher_oms.create_any_table_partition(
                    v_job.table_schema,
                    v_job.table_name,
                    v_job.partition_unit,
                    v_job.partition_period::integer,
                    v_job.create_drop_interval
                );
            ELSE
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

    v_duration_ms := GREATEST(
        0,
        (EXTRACT(EPOCH FROM (clock_timestamp() - v_exec_start)) * 1000)::bigint
    );

    -- Validated v_new_next_run is already a future occurrence distinct from T.
    UPDATE mubasher_oms.partitioning_job_table AS j
       SET last_run_time   = v_now,
           last_run_status = CASE
                                 WHEN v_status = 'EXECUTED' THEN 'SUCCESS'
                                 ELSE 'FAIL'
                             END,
           next_run_time   = v_new_next_run
     WHERE j.job_id = v_job.job_id;

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
        CASE
            WHEN v_error IS NULL AND v_status = 'EXECUTED' THEN 'SUCCESS'
            ELSE 'FAIL'
        END,
        v_now,
        v_error,
        v_duration_ms
    );

    status := v_status;
    job_id := v_job.job_id;
    message := COALESCE(v_message, v_error);
    next_run_time := v_new_next_run;
    is_create := v_job.is_create;
    RETURN NEXT;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone) IS
'Execute one scheduled partition-job occurrence after DB revalidation. '
'Validates a future next_run_time distinct from p_expected_run_time BEFORE '
'CREATE/DROP. Invalid schedule fails closed (next_run_time NULL, no worker). '
'Does not invent +1 day or frequency fallbacks. '
'Records execution_duration_ms on SUCCESS and FAIL worker attempts. '
'Rejects stale expected_run_time and disabled jobs before workers run. '
'Does not replace run_partition_job_manual. Does not call legacy polling runners.';

COMMIT;

