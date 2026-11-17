-- =============================================================================
-- Migration: Phase 5A (part 2) — missed-run reconciliation, manual target lock,
--            db_config_para allowlist enforcement, DBA review functions
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
--
-- Prerequisites:
--   * 20261110_01_partition_job_attention.sql applied
--   * Phase 2 body of run_partition_job_scheduled (20261013_01) deployed
--   * Phase 4 update_data_to_partition_job_table (20261013_02) deployed
--
-- Changes (CREATE OR REPLACE everywhere existing objects are touched — owner and
-- existing EXECUTE grants are preserved; no DROP FUNCTION, no CASCADE):
--
--   1. NEW reconcile_missed_partition_job(numeric, timestamp, text, text)
--        Records a partition_job_attention row and advances next_run_time to
--        the first legitimate FUTURE occurrence. Never runs CREATE/DROP.
--        Never touches last_run_time / last_run_status. Fail closed (NULL
--        next_run_time) when no valid future schedule exists.
--   2. run_partition_job_scheduled(numeric, timestamp)
--        Safety ceiling: an occurrence later than expected +
--        partition_job_max_execution_lateness() (default 6 h) is reconciled as
--        missed (MISSED_RESCHEDULED / MISSED_INVALID_SCHEDULE) instead of
--        executed. The PRIMARY missed-run decision is the scheduler's: it calls
--        reconcile_missed_partition_job directly for occurrences it first
--        discovered already overdue, and only calls this function for
--        occurrences it owned (discovered on time). db_config_para keys are
--        checked against the allowlist before set_config(). Advisory-lock key
--        arithmetic made overflow-safe (identical keys for all other inputs).
--   3. run_partition_job_manual(numeric)
--        Takes the SAME target-table transaction advisory lock as scheduled
--        execution (namespace 87421032, identical key). Busy → returns
--        MANUAL_TARGET_LOCKED (nothing executed, no log row). Allowlist check.
--   4. update_data_to_partition_job_table(...)
--        Rejects db_config_para keys/values outside the allowlist (22023).
--        SECURITY DEFINER / owner / fixed search_path restated unchanged.
--   5. NEW run_partition_job_attention_recovery(bigint)   (SECURITY INVOKER)
--        DBA "run missed occurrence": locks the attention row (NOWAIT), checks
--        PENDING_REVIEW and unchanged job target/operation, executes via
--        run_partition_job_manual (so it uses the caller's privileges, the
--        target lock and the existing MANUAL_* logging), records the result.
--        Does NOT change next_run_time.
--   6. NEW acknowledge_partition_job_attention(bigint, text)  (SECURITY INVOKER)
--        DBA "acknowledge without running". No DDL.
--   7. PUBLIC EXECUTE revoked on all of the above; narrow role grants.
--
-- Rollback: re-apply the previous bodies in this order:
--   sql/migrations/20261013_01_scheduled_target_advisory_lock.sql
--   sql/migrations/20261013_02_secure_update_partition_job.sql
--   run_partition_job_manual body from 20260929_03 (CREATE OR REPLACE section
--     only — do NOT re-run its DROP FUNCTION)
--   then:
--   DROP FUNCTION IF EXISTS mubasher_oms.acknowledge_partition_job_attention(bigint, text);
--   DROP FUNCTION IF EXISTS mubasher_oms.run_partition_job_attention_recovery(bigint);
--   DROP FUNCTION IF EXISTS mubasher_oms.reconcile_missed_partition_job(
--       numeric, timestamp without time zone, text, text);
-- Restoring the pre-5A scheduled body ALSO restores automatic execution of
-- overdue occurrences — keep the scheduler stopped while deciding.
-- =============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. Missed-occurrence reconciliation (no DDL)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.reconcile_missed_partition_job(
    p_job_id            numeric,
    p_expected_run_time timestamp without time zone,
    p_reason            text,
    p_detail            text DEFAULT NULL
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
    v_job        mubasher_oms.partitioning_job_table%ROWTYPE;
    v_now        timestamp without time zone;
    v_schedule   mubasher_oms.cron_schedule_result;
    v_next       timestamp without time zone;
    v_error      text;
    v_reason     text;
    v_lock_ok    boolean;
BEGIN
    v_reason := upper(btrim(COALESCE(p_reason, '')));
    IF v_reason NOT IN ('MISSED_SCHEDULED_RUN', 'RETRY_EXHAUSTED') THEN
        RAISE EXCEPTION
            'reconcile_missed_partition_job: unsupported reason %', p_reason
            USING ERRCODE = '22023';
    END IF;

    -- Same per-job lock as run_partition_job_scheduled (re-entrant when called
    -- from inside it in the same transaction).
    v_lock_ok := pg_try_advisory_xact_lock(
        87421031,
        (abs(hashtext('partition_job_scheduled:' || p_job_id::text)::bigint) % 2147483647)::integer
    );
    IF NOT v_lock_ok THEN
        status := 'SKIPPED_LOCKED';
        job_id := p_job_id;
        message := 'Another transaction is already processing this job_id.';
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

    v_now := clock_timestamp()::timestamp without time zone;

    IF v_job.is_enabled IS NOT TRUE THEN
        status := 'SKIPPED_DISABLED';
        job_id := v_job.job_id;
        message := 'Job is disabled; nothing reconciled.';
        next_run_time := v_job.next_run_time;
        is_create := v_job.is_create;
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_job.next_run_time IS DISTINCT FROM p_expected_run_time THEN
        status := 'SKIPPED_RESCHEDULED';
        job_id := v_job.job_id;
        message := format(
            'Expected next_run_time %s but database has %s; nothing reconciled.',
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
        message := 'Occurrence is in the future; nothing to reconcile.';
        next_run_time := v_job.next_run_time;
        is_create := v_job.is_create;
        RETURN NEXT;
        RETURN;
    END IF;

    -- First legitimate FUTURE occurrence from the authoritative cron helper.
    -- No +1 day / frequency invention.
    BEGIN
        v_schedule := mubasher_oms.cron_to_interval_or_next_run(v_job.job_schedule);
        v_next := CASE
            WHEN v_schedule.schedule_interval IS NOT NULL
                THEN v_now + v_schedule.schedule_interval
            WHEN v_schedule.next_run_time IS NOT NULL
                THEN v_schedule.next_run_time
            ELSE
                NULL
        END;
    EXCEPTION
        WHEN OTHERS THEN
            v_next := NULL;
            v_error := 'Cron calculation failed: ' || SQLERRM;
    END;

    IF v_next IS NOT NULL
       AND (v_next <= v_now OR v_next <= p_expected_run_time) THEN
        v_error := COALESCE(
            v_error,
            format('Schedule helper returned non-future next_run_time %s (now=%s).',
                   v_next, v_now)
        );
        v_next := NULL;
    ELSIF v_next IS NULL THEN
        v_error := COALESCE(v_error, 'Schedule helper returned no next occurrence.');
    END IF;

    -- Evidence first. Idempotent per (job_id, missed_run_time).
    INSERT INTO mubasher_oms.partition_job_attention (
        job_id, job_name, table_schema, table_name, is_create,
        reason, missed_run_time, detected_at, rescheduled_to, detail
    ) VALUES (
        v_job.job_id, v_job.job_name, v_job.table_schema, v_job.table_name,
        v_job.is_create, v_reason, p_expected_run_time, v_now, v_next,
        NULLIF(left(concat_ws(' ', NULLIF(btrim(COALESCE(p_detail, '')), ''), v_error), 1000), '')
    )
    ON CONFLICT ON CONSTRAINT partition_job_attention_job_missed_uq DO NOTHING;

    -- Advance the normal cadence. last_run_time / last_run_status are NOT
    -- touched: no execution happened (a miss is not a FAIL).
    UPDATE mubasher_oms.partitioning_job_table AS j
       SET next_run_time = v_next
     WHERE j.job_id = v_job.job_id;

    job_id := v_job.job_id;
    next_run_time := v_next;
    is_create := v_job.is_create;
    IF v_next IS NULL THEN
        status := 'MISSED_INVALID_SCHEDULE';
        message := 'Missed occurrence recorded for review and NOT executed. '
                   || 'No valid future schedule: next_run_time cleared (fail closed). '
                   || COALESCE(v_error, '');
    ELSE
        status := 'MISSED_RESCHEDULED';
        message := format(
            'Missed occurrence %s recorded for review and NOT executed. '
            'Next scheduled run: %s.',
            p_expected_run_time, v_next
        );
    END IF;
    RETURN NEXT;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.reconcile_missed_partition_job(
    numeric, timestamp without time zone, text, text
) IS
'Phase 5A: record a not-executed scheduled occurrence in partition_job_attention '
'and advance next_run_time to the first future occurrence (fail closed to NULL). '
'Never runs CREATE/DROP and never changes last_run_time/last_run_status.';

ALTER FUNCTION mubasher_oms.reconcile_missed_partition_job(
    numeric, timestamp without time zone, text, text
) OWNER TO mubasher_oms;


-- ----------------------------------------------------------------------------
-- 2. Scheduled executor (canonical body also in sql/realtime_scheduler_v1.sql)
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
        (abs(hashtext('partition_job_scheduled:' || p_job_id::text)::bigint) % 2147483647)::integer
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

    -- Phase 5A safety ceiling. The scheduler only calls this function for
    -- occurrences it discovered on time ("owned"); historical occurrences go
    -- straight to reconcile_missed_partition_job. Independently of the
    -- caller, the database never executes an occurrence later than the
    -- maximum execution lateness: it is recorded for DBA review and the job
    -- moves to its first legitimate future occurrence. No worker runs here.
    IF v_now > p_expected_run_time + mubasher_oms.partition_job_max_execution_lateness() THEN
        RETURN QUERY
        SELECT r.status, r.job_id, r.message, r.next_run_time, r.is_create
          FROM mubasher_oms.reconcile_missed_partition_job(
                   p_job_id, p_expected_run_time, 'MISSED_SCHEDULED_RUN',
                   'Exceeded maximum execution lateness; not executed.'
               ) AS r;
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

    -- Target-table transaction advisory lock (namespace 87421032 — distinct from
    -- job-id lock 87421031). Serializes CREATE/DROP against the same
    -- (table_schema, table_name) across different job_ids AND against
    -- run_partition_job_manual (same namespace, identical key). Non-blocking:
    -- SKIPPED_TARGET_LOCKED so the occurrence remains eligible for a later,
    -- bounded, backed-off retry by the scheduler. Lock order: job advisory →
    -- FOR UPDATE → validate → target advisory → schedule calc → worker.
    v_lock_ok := pg_try_advisory_xact_lock(
        87421032,
        (abs(hashtext(
            'partition_job_target:'
            || COALESCE(v_job.table_schema, '')
            || '.'
            || COALESCE(v_job.table_name, '')
        )::bigint) % 2147483647)::integer
    );
    IF NOT v_lock_ok THEN
        status := 'SKIPPED_TARGET_LOCKED';
        job_id := v_job.job_id;
        message := format(
            'Another transaction is already operating on target %s.%s.',
            COALESCE(v_job.table_schema, ''),
            COALESCE(v_job.table_name, '')
        );
        next_run_time := v_job.next_run_time;
        is_create := v_job.is_create;
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
    -- Every key/value must pass the PartOps allowlist (fail closed).
    BEGIN
        FOR v_cfg IN
            SELECT key, value
              FROM jsonb_each_text(COALESCE(v_job.db_config_para, '{}'::jsonb))
        LOOP
            IF NOT mubasher_oms.partition_job_config_param_allowed(v_cfg.key, v_cfg.value) THEN
                RAISE EXCEPTION
                    'db_config_para parameter "%" (or its value) is not allowed by the PartOps allowlist',
                    v_cfg.key
                    USING ERRCODE = '22023';
            END IF;
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
'Job-id advisory lock (87421031) then target-table advisory lock (87421032). '
'Same (table_schema, table_name) cannot run CREATE/DROP concurrently '
'(SKIPPED_TARGET_LOCKED; non-blocking; occurrence left eligible). '
'Phase 5A: the scheduler reconciles historical (not owned) occurrences via '
'reconcile_missed_partition_job; as a safety ceiling this function never '
'executes an occurrence later than partition_job_max_execution_lateness() and '
'records it in partition_job_attention instead (MISSED_RESCHEDULED / '
'MISSED_INVALID_SCHEDULE). '
'db_config_para must pass partition_job_config_param_allowed. '
'Validates a future next_run_time distinct from p_expected_run_time BEFORE '
'CREATE/DROP. Invalid schedule fails closed (next_run_time NULL, no worker). '
'Does not invent +1 day or frequency fallbacks. '
'Records execution_duration_ms on SUCCESS and FAIL worker attempts. '
'Rejects stale expected_run_time and disabled jobs before workers run. '
'Does not replace run_partition_job_manual. Does not call legacy polling runners.';


-- ----------------------------------------------------------------------------
-- 3. Manual run: target-table lock + allowlist (same return contract)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_manual(
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
    v_lock_ok      boolean;
BEGIN
    SELECT *
      INTO v_job
      FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = p_job_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'run_partition_job_manual: job_id % not found', p_job_id;
    END IF;

    -- Same target-table advisory lock as run_partition_job_scheduled
    -- (namespace 87421032, identical key): manual and scheduled CREATE/DROP on
    -- one table are mutually exclusive. Non-blocking: nothing executed and no
    -- log row when busy.
    v_lock_ok := pg_try_advisory_xact_lock(
        87421032,
        (abs(hashtext(
            'partition_job_target:'
            || COALESCE(v_job.table_schema, '')
            || '.'
            || COALESCE(v_job.table_name, '')
        )::bigint) % 2147483647)::integer
    );
    IF NOT v_lock_ok THEN
        status := 'MANUAL_TARGET_LOCKED';
        message := format(
            'Another transaction is already operating on target %s.%s. '
            'Nothing was executed; try again when it has finished.',
            COALESCE(v_job.table_schema, ''),
            COALESCE(v_job.table_name, '')
        );
        execution_duration_ms := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    -- Wall-clock of the actual attempt (config apply + CREATE/DROP), not queue wait.
    v_exec_start := clock_timestamp();

    BEGIN
        FOR v_cfg IN
            SELECT key, value
              FROM jsonb_each_text(COALESCE(v_job.db_config_para, '{}'::jsonb))
        LOOP
            IF NOT mubasher_oms.partition_job_config_param_allowed(v_cfg.key, v_cfg.value) THEN
                RAISE EXCEPTION
                    'db_config_para parameter "%" (or its value) is not allowed by the PartOps allowlist',
                    v_cfg.key
                    USING ERRCODE = '22023';
            END IF;
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
'Execute one configured partition job immediately. Takes the target-table '
'advisory lock (87421032) shared with scheduled execution; busy target returns '
'MANUAL_TARGET_LOCKED without executing. Writes MANUAL_SUCCESS or MANUAL_FAIL '
'with execution_duration_ms on the same log INSERT. Returns '
'status/message/duration; MANUAL_FAIL does not RAISE so the caller can COMMIT. '
'Does not change next_run_time, last_run_time, or last_run_status.';


-- ----------------------------------------------------------------------------
-- 4. Secure config update: + db_config_para allowlist
-- ----------------------------------------------------------------------------
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
    v_cfg              record;
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

    -- Phase 5A: only allowlisted session parameters may be stored.
    FOR v_cfg IN
        SELECT key, value FROM jsonb_each_text(p_db_config_para)
    LOOP
        IF NOT mubasher_oms.partition_job_config_param_allowed(v_cfg.key, v_cfg.value) THEN
            RAISE EXCEPTION
                'update_data_to_partition_job_table: db_config_para parameter "%" '
                '(or its value) is not allowed by the PartOps allowlist',
                v_cfg.key
                USING ERRCODE = '22023';
        END IF;
    END LOOP;

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


-- ----------------------------------------------------------------------------
-- 5. DBA review: run the missed occurrence manually (SECURITY INVOKER)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.run_partition_job_attention_recovery(
    p_attention_id bigint
)
RETURNS TABLE (
    result_status       text,
    result_message      text,
    result_duration_ms  bigint,
    attention_status    text,
    attention_job_id    numeric
)
LANGUAGE plpgsql
SECURITY INVOKER
AS $function$
DECLARE
    v_att         mubasher_oms.partition_job_attention%ROWTYPE;
    v_job         mubasher_oms.partitioning_job_table%ROWTYPE;
    v_status      text;
    v_message     text;
    v_duration    bigint;
    v_new_status  text;
    v_now         timestamp without time zone;
BEGIN
    IF p_attention_id IS NULL OR p_attention_id <= 0 THEN
        RAISE EXCEPTION 'Invalid attention_id %', p_attention_id
            USING ERRCODE = '22023';
    END IF;

    -- NOWAIT: a concurrent recovery/acknowledge of the same item fails fast
    -- (SQLSTATE 55P03) instead of queueing a second DDL attempt.
    SELECT *
      INTO v_att
      FROM mubasher_oms.partition_job_attention AS a
     WHERE a.attention_id = p_attention_id
     FOR UPDATE NOWAIT;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Attention item % not found', p_attention_id
            USING ERRCODE = 'P0002';
    END IF;

    IF v_att.status IS DISTINCT FROM 'PENDING_REVIEW' THEN
        RAISE EXCEPTION
            'Attention item % is already %; nothing was executed.',
            p_attention_id, v_att.status
            USING ERRCODE = '22023';
    END IF;

    SELECT *
      INTO v_job
      FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = v_att.job_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Job % for attention item % not found',
            v_att.job_id, p_attention_id
            USING ERRCODE = 'P0002';
    END IF;

    IF v_job.is_create IS DISTINCT FROM v_att.is_create
       OR v_job.table_schema IS DISTINCT FROM v_att.table_schema
       OR v_job.table_name IS DISTINCT FROM v_att.table_name THEN
        RAISE EXCEPTION
            'Job % operation or target table changed since the missed run was '
            'detected; nothing was executed. Review and acknowledge instead.',
            v_att.job_id
            USING ERRCODE = '22023';
    END IF;

    -- Existing manual path: caller privileges, target-table lock, allowlist,
    -- MANUAL_SUCCESS / MANUAL_FAIL log row. Never changes next_run_time.
    SELECT r.status, r.message, r.execution_duration_ms
      INTO v_status, v_message, v_duration
      FROM mubasher_oms.run_partition_job_manual(v_att.job_id) AS r;

    v_now := clock_timestamp()::timestamp without time zone;
    v_new_status := CASE
        WHEN v_status = 'MANUAL_SUCCESS' THEN 'MANUALLY_RERUN'
        ELSE 'PENDING_REVIEW'
    END;

    UPDATE mubasher_oms.partition_job_attention AS a
       SET status = v_new_status,
           reviewed_at = CASE
               WHEN v_new_status = 'PENDING_REVIEW' THEN a.reviewed_at
               ELSE v_now
           END,
           resolution = CASE
               WHEN v_new_status = 'PENDING_REVIEW' THEN a.resolution
               ELSE 'Missed occurrence executed manually by DBA recovery.'
           END,
           manual_execution_result = v_status,
           manual_execution_message = left(v_message, 2000),
           manual_execution_duration_ms = v_duration,
           manual_executed_at = v_now
     WHERE a.attention_id = v_att.attention_id;

    result_status := v_status;
    result_message := v_message;
    result_duration_ms := v_duration;
    attention_status := v_new_status;
    attention_job_id := v_att.job_id;
    RETURN NEXT;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.run_partition_job_attention_recovery(bigint) IS
'DBA recovery of a missed occurrence: executes via run_partition_job_manual '
'(caller privileges + target lock) and records the outcome on the attention '
'row. MANUAL_SUCCESS → MANUALLY_RERUN; otherwise stays PENDING_REVIEW. '
'Never changes partitioning_job_table.next_run_time.';

ALTER FUNCTION mubasher_oms.run_partition_job_attention_recovery(bigint)
    OWNER TO mubasher_oms;


-- ----------------------------------------------------------------------------
-- 6. DBA review: acknowledge without running (SECURITY INVOKER, no DDL)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.acknowledge_partition_job_attention(
    p_attention_id bigint,
    p_note         text DEFAULT NULL
)
RETURNS TABLE (
    attention_status  text,
    attention_job_id  numeric,
    acknowledged_at   timestamp without time zone
)
LANGUAGE plpgsql
SECURITY INVOKER
-- Calls no other routines, so pinning search_path cannot change behaviour.
-- (Functions that call the unversioned cron helper / partition workers keep
-- the caller's search_path on purpose, exactly like the pre-5A executors.)
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_att  mubasher_oms.partition_job_attention%ROWTYPE;
    v_now  timestamp without time zone;
BEGIN
    IF p_attention_id IS NULL OR p_attention_id <= 0 THEN
        RAISE EXCEPTION 'Invalid attention_id %', p_attention_id
            USING ERRCODE = '22023';
    END IF;

    SELECT *
      INTO v_att
      FROM mubasher_oms.partition_job_attention AS a
     WHERE a.attention_id = p_attention_id
     FOR UPDATE NOWAIT;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Attention item % not found', p_attention_id
            USING ERRCODE = 'P0002';
    END IF;

    IF v_att.status IS DISTINCT FROM 'PENDING_REVIEW' THEN
        RAISE EXCEPTION 'Attention item % is already %.',
            p_attention_id, v_att.status
            USING ERRCODE = '22023';
    END IF;

    v_now := clock_timestamp()::timestamp without time zone;

    UPDATE mubasher_oms.partition_job_attention AS a
       SET status = 'ACKNOWLEDGED',
           reviewed_at = v_now,
           resolution = left(
               COALESCE(NULLIF(btrim(COALESCE(p_note, '')), ''),
                        'Acknowledged without execution.'),
               500
           )
     WHERE a.attention_id = v_att.attention_id;

    attention_status := 'ACKNOWLEDGED';
    attention_job_id := v_att.job_id;
    acknowledged_at := v_now;
    RETURN NEXT;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.acknowledge_partition_job_attention(bigint, text) IS
'DBA acknowledges a missed occurrence without executing it. No DDL. '
'Does not change next_run_time.';

ALTER FUNCTION mubasher_oms.acknowledge_partition_job_attention(bigint, text)
    OWNER TO mubasher_oms;


-- ----------------------------------------------------------------------------
-- 7. PUBLIC revoked; narrow EXECUTE grants
-- ----------------------------------------------------------------------------
REVOKE ALL ON FUNCTION mubasher_oms.reconcile_missed_partition_job(
    numeric, timestamp without time zone, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.run_partition_job_scheduled(
    numeric, timestamp without time zone) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.run_partition_job_manual(numeric) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.run_partition_job_attention_recovery(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.acknowledge_partition_job_attention(bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.update_data_to_partition_job_table(
    numeric, character varying, boolean, character varying, character varying,
    jsonb, character varying, interval, timestamp without time zone,
    character varying, numeric, boolean, interval
) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_scheduler') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.run_partition_job_scheduled(numeric, timestamp without time zone) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.reconcile_missed_partition_job(numeric, timestamp without time zone, text, text) TO partition_job_scheduler';
    ELSE
        RAISE NOTICE 'Role partition_job_scheduler does not exist — scheduler grants skipped.';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_ui') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.run_partition_job_manual(numeric) TO partition_job_ui';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.run_partition_job_attention_recovery(bigint) TO partition_job_ui';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.acknowledge_partition_job_attention(bigint, text) TO partition_job_ui';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.update_data_to_partition_job_table('
                'numeric, character varying, boolean, character varying, character varying, '
                'jsonb, character varying, interval, timestamp without time zone, '
                'character varying, numeric, boolean, interval) TO partition_job_ui';
    ELSE
        RAISE NOTICE 'Role partition_job_ui does not exist — UI grants skipped.';
    END IF;
END
$$;

COMMIT;

-- Verification (read-only):
--   SELECT p.oid::regprocedure, p.prosecdef, pg_get_userbyid(p.proowner),
--          p.proacl
--     FROM pg_proc p
--    WHERE p.pronamespace = 'mubasher_oms'::regnamespace
--      AND p.proname IN ('run_partition_job_scheduled', 'run_partition_job_manual',
--                        'reconcile_missed_partition_job',
--                        'run_partition_job_attention_recovery',
--                        'acknowledge_partition_job_attention',
--                        'update_data_to_partition_job_table')
--    ORDER BY 1;
-- Do NOT call run_partition_job_scheduled / reconcile as a health test.
