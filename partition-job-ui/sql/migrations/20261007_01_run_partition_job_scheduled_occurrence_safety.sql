-- =============================================================================
-- Migration: Phase 0 scheduled-occurrence safety for run_partition_job_scheduled
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Prerequisite: mubasher_oms.run_partition_job_scheduled already deployed
-- (or deploy from sql/realtime_scheduler_v1.sql first).
--
-- Same return type — CREATE OR REPLACE is sufficient (no DROP).
-- Authoritative body is kept in sync with sql/realtime_scheduler_v1.sql.
--
-- Safety added:
--   * After worker attempt, next_run_time must be a FUTURE timestamp
--     distinct from p_expected_run_time (occurrence T).
--   * Invalid / non-future cron helper results use frequency or 1-day fallback.
--   * Prevents same (job_id, expected_run_time) tight-loop after EXECUTED.
--   * Overdue semantics: execute once, advance to next future occurrence
--     (no miss-by-miss catch-up storm).
--
-- Also redeploy Python scheduler_backend for secondary circuit breaker.
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
        status := 'FAILED';
        job_id := v_job.job_id;
        message := 'is_create is NULL; refusing to decide CREATE vs DROP.';
        next_run_time := v_now + interval '1 day';
        is_create := NULL;
        -- No CREATE/DROP worker ran â€” duration is unknown, not 0ms.
        -- Advance next_run_time so an invalid job cannot tight-loop overdue.
        UPDATE mubasher_oms.partitioning_job_table AS j
           SET last_run_time   = v_now,
               last_run_status = 'FAIL',
               next_run_time   = v_now + interval '1 day'
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

    v_duration_ms := GREATEST(
        0,
        (EXTRACT(EPOCH FROM (clock_timestamp() - v_exec_start)) * 1000)::bigint
    );

    -- Calculate next_run_time using authoritative cron helper + legacy CASE.
    -- Intended overdue semantics: execute THIS occurrence once, then advance to
    -- a next FUTURE occurrence (no uncontrolled miss-by-miss catch-up loop).
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

    -- Reject invalid / non-advancing next states that would re-queue occurrence T
    -- or drive an immediate overdue catch-up storm after refresh.
    IF v_new_next_run IS NULL
       OR v_new_next_run IS NOT DISTINCT FROM p_expected_run_time
       OR v_new_next_run <= p_expected_run_time
       OR v_new_next_run <= v_now THEN
        IF v_job.frequency IS NOT NULL AND v_job.frequency > interval '0' THEN
            v_new_next_run := v_now + v_job.frequency;
        ELSE
            v_new_next_run := v_now + interval '1 day';
        END IF;
        IF v_new_next_run IS NULL
           OR v_new_next_run IS NOT DISTINCT FROM p_expected_run_time
           OR v_new_next_run <= v_now THEN
            v_new_next_run := v_now + interval '1 day';
        END IF;
        IF v_message IS NULL THEN
            v_message := 'Schedule advanced with controlled fallback '
                || '(invalid or non-future next occurrence).';
        ELSE
            v_message := v_message
                || ' Schedule advanced with controlled fallback '
                || '(invalid or non-future next occurrence).';
        END IF;
    END IF;

    -- After an actual attempt, advance next_run_time so the same occurrence
    -- cannot tight-loop during reconciliation.
    IF NOT v_attempted AND v_error IS NOT NULL THEN
        v_attempted := TRUE;
    END IF;

    UPDATE mubasher_oms.partitioning_job_table AS j
       SET last_run_time   = v_now,
           last_run_status = CASE
                                 WHEN v_status = 'EXECUTED' THEN 'SUCCESS'
                                 ELSE 'FAIL'
                             END,
           next_run_time   = COALESCE(v_new_next_run, v_now + interval '1 day')
     WHERE j.job_id = v_job.job_id;

    -- job_log_id uses column DEFAULT / sequence â€” do not supply it manually.
    -- job_runtime = when the attempt occurred; execution_duration_ms = how long.
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
    next_run_time := COALESCE(v_new_next_run, v_now + interval '1 day');
    is_create := v_job.is_create;
    RETURN NEXT;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone) IS
'Execute one scheduled partition-job occurrence after DB revalidation. '
'Uses exact create_any_table_partition / drop_any_table_partition signatures. '
'Records execution_duration_ms on SUCCESS and FAIL log rows. '
'Advances next_run_time to a future occurrence distinct from p_expected_run_time. '
'Rejects stale expected_run_time and disabled jobs before workers run. '
'Does not replace run_partition_job_manual. Does not call legacy polling runners.'

COMMIT;

