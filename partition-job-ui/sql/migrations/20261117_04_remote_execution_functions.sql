-- =============================================================================
-- Migration: Phase 5D (part 2) — remote execution control functions (CONTROL DB)
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production. Apply after
-- 20261117_03_remote_execution_ledger.sql, scheduler STOPPED, as schema owner.
--
-- ONE PARTOPS INSTANCE PER HUB / ~2 TARGET DATABASES PER HUB / NO GLOBAL
-- MULTI-HUB CONTROL PLANE. Nothing here is installed on target databases.
--
-- Control-database half of remote execution. The target half is a plain
-- target transaction opened by the scheduler (advisory lock → allowlisted
-- session settings → legacy worker → COMMIT); NO distributed / prepared
-- transaction is used. Every function below runs one SHORT control
-- transaction; none is held open while target DDL runs.
--
--   partops_get_upcoming_jobs        target-aware discovery (all enabled targets)
--   partops_remote_claim             revalidate + create/reuse the attempt (PREPARED)
--   partops_remote_mark_dispatching  PREPARED → DISPATCHING (before the worker call)
--   partops_remote_record_success    DISPATCHING → TARGET_SUCCEEDED (target COMMIT done)
--   partops_remote_finalize          TARGET_SUCCEEDED → FINALIZED (history, schedule)
--   partops_remote_record_failure    PREPARED/DISPATCHING → TARGET_FAILED (proven rollback)
--   partops_remote_mark_unknown      DISPATCHING → OUTCOME_UNKNOWN (+ attention, job paused)
--   partops_remote_recover_attempts  scheduler start: DISPATCHING → OUTCOME_UNKNOWN, ...
--   partops_remote_close_attempt     reconcile helper (retry exhausted / missed)
--   partops_resolve_remote_outcome   operator resolution of OUTCOME_UNKNOWN (UI role)
--   partops_set_job_target           move a job to another target (UI role, guarded)
--   reconcile_missed_partition_job   now also for remote jobs (never while in flight)
--
-- Ledger / job / history / attention writes happen ONLY inside these SECURITY
-- DEFINER functions (owner mubasher_oms, search_path pinned). App roles get
-- EXECUTE on exactly the functions they need and no table write privilege.
-- The legacy cron helper is called (mubasher_oms-qualified) to compute the
-- next occurrence, exactly like update_data_to_partition_job_table (see
-- ISSUE-010 for the EDB `sys` search_path caveat). Legacy workers are NOT
-- called here and NOT defined or modified by this migration.
--
-- Rollback: see RUNBOOK §10.8 (re-apply 20261117_02 reconcile body; drop the
-- functions listed above; delete version row 20261117_04).
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('mubasher_oms.partops_execution_attempt') IS NULL
       OR NOT EXISTS (SELECT 1 FROM mubasher_oms.partops_framework_version
                       WHERE migration_id = '20261117_03') THEN
        RAISE EXCEPTION 'Apply 20261117_03_remote_execution_ledger.sql first.';
    END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- 0. Internal helpers (no app-role EXECUTE)
-- ----------------------------------------------------------------------------
-- First legitimate FUTURE occurrence from the authoritative cron helper; same
-- rules as run_partition_job_scheduled (no +1 day / frequency invention).
CREATE OR REPLACE FUNCTION mubasher_oms.partops_compute_next_run(
    p_job_schedule character varying,
    p_expected     timestamp without time zone,
    p_now          timestamp without time zone,
    OUT o_next     timestamp without time zone,
    OUT o_error    text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_schedule mubasher_oms.cron_schedule_result;
BEGIN
    o_next := NULL;
    o_error := NULL;
    BEGIN
        v_schedule := mubasher_oms.cron_to_interval_or_next_run(p_job_schedule);
        o_next := CASE
            WHEN v_schedule.schedule_interval IS NOT NULL THEN p_now + v_schedule.schedule_interval
            WHEN v_schedule.next_run_time IS NOT NULL THEN v_schedule.next_run_time
            ELSE NULL
        END;
    EXCEPTION
        WHEN OTHERS THEN
            o_next := NULL;
            o_error := 'Cron calculation failed: ' || SQLERRM;
    END;
    IF o_next IS NULL THEN
        o_error := COALESCE(o_error, 'Schedule helper returned no next occurrence.');
    ELSIF o_next <= p_now OR (p_expected IS NOT NULL AND o_next <= p_expected) THEN
        o_error := format('Schedule helper returned non-future next_run_time %s (now=%s).', o_next, p_now);
        o_next := NULL;
    END IF;
END;
$function$;

ALTER FUNCTION mubasher_oms.partops_compute_next_run(character varying, timestamp without time zone, timestamp without time zone)
    OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.partops_compute_next_run(character varying, timestamp without time zone, timestamp without time zone)
    FROM PUBLIC;

-- Strip anything that could carry a secret from an error text; bounded.
CREATE OR REPLACE FUNCTION mubasher_oms.partops_safe_error(p_text text)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
    SELECT NULLIF(left(regexp_replace(
               regexp_replace(COALESCE(p_text, ''), '[\r\n]+', ' ', 'g'),
               '(password|passwd|secret|token)[^ ]*', '[redacted]', 'gi'), 500), '')
$function$;

ALTER FUNCTION mubasher_oms.partops_safe_error(text) OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.partops_safe_error(text) FROM PUBLIC;


-- ----------------------------------------------------------------------------
-- 1. Target-aware discovery (read-only; replaces the control-only filter for
--    the Phase 5D scheduler; get_upcoming_partition_jobs stays for rollback)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_get_upcoming_jobs(
    p_lookahead interval
)
RETURNS TABLE (
    job_id             numeric,
    job_name           character varying,
    is_create          boolean,
    expected_run_time  timestamp without time zone,
    delay_seconds      double precision,
    database_target_id integer,
    target_key         character varying,
    is_control_target  boolean
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_now timestamp without time zone;
BEGIN
    IF p_lookahead IS NULL OR p_lookahead <= interval '0' THEN
        RAISE EXCEPTION
            'partops_get_upcoming_jobs: p_lookahead must be NOT NULL and > 0 (got %)', p_lookahead;
    END IF;
    v_now := clock_timestamp()::timestamp without time zone;
    RETURN QUERY
    SELECT j.job_id, j.job_name, j.is_create,
           j.next_run_time AS expected_run_time,
           EXTRACT(EPOCH FROM (j.next_run_time - v_now))::double precision AS delay_seconds,
           t.target_id, t.target_key, t.is_control_database
      FROM mubasher_oms.partitioning_job_table AS j
      JOIN mubasher_oms.partops_database_target AS t
        ON t.target_id = j.database_target_id
     WHERE j.is_enabled IS TRUE
       AND t.is_enabled IS TRUE
       AND j.next_run_time IS NOT NULL
       AND j.next_run_time <= v_now + p_lookahead
     ORDER BY j.next_run_time ASC, t.target_id ASC, j.job_id ASC;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.partops_get_upcoming_jobs(interval) IS
    'Phase 5D: read-only discovery of due/upcoming jobs across all ENABLED '
    'targets of this hub (job definitions live centrally in the control DB; '
    'target databases are never queried for discovery).';

ALTER FUNCTION mubasher_oms.partops_get_upcoming_jobs(interval) OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.partops_get_upcoming_jobs(interval) FROM PUBLIC;


-- ----------------------------------------------------------------------------
-- 2. Close a not-dispatched / proven-failed attempt (reconcile helper)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_close_attempt(
    p_attempt_id bigint,
    p_reason     text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_att mubasher_oms.partops_execution_attempt%ROWTYPE;
    v_now timestamp without time zone := clock_timestamp()::timestamp without time zone;
BEGIN
    SELECT * INTO v_att FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND OR v_att.state NOT IN ('PREPARED', 'TARGET_FAILED') THEN
        RETURN;  -- never closes DISPATCHING / TARGET_SUCCEEDED / UNKNOWN
    END IF;
    UPDATE mubasher_oms.partops_execution_attempt AS x
       SET state = 'FINALIZED', finalized_at = v_now,
           result = left(upper(COALESCE(p_reason, 'CLOSED')), 40)
     WHERE x.attempt_id = p_attempt_id;
    -- Exhausted after real (rolled-back) worker failures: record the failed
    -- scheduled outcome like the local executor does (no DDL here).
    IF upper(COALESCE(p_reason, '')) = 'RETRY_EXHAUSTED'
       AND v_att.error_class = 'WORKER_ERROR' THEN
        UPDATE mubasher_oms.partitioning_job_table AS j
           SET last_run_time = v_now, last_run_status = 'FAIL'
         WHERE j.job_id = v_att.job_id;
    END IF;
END;
$function$;

ALTER FUNCTION mubasher_oms.partops_remote_close_attempt(bigint, text) OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_close_attempt(bigint, text) FROM PUBLIC;


-- ----------------------------------------------------------------------------
-- 3. Missed-occurrence reconciliation (Phase 5A semantics, now target-aware)
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
    v_att_id     bigint;
    v_att_state  text;
    v_att_error  text;
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

    -- Phase 5D: a remote attempt of this occurrence that may be running, whose
    -- target commit is confirmed, or whose outcome is unknown is never
    -- reconciled away (that would advance the schedule under in-flight DDL).
    SELECT x.attempt_id, x.state, x.error_summary
      INTO v_att_id, v_att_state, v_att_error
      FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.execution_kind = 'SCHEDULED'
       AND x.job_id = v_job.job_id
       AND x.expected_run_time = p_expected_run_time
       AND x.database_target_id = v_job.database_target_id;
    IF v_att_state IN ('DISPATCHING', 'TARGET_SUCCEEDED', 'OUTCOME_UNKNOWN') THEN
        status := 'SKIPPED_ATTEMPT_ACTIVE';
        job_id := v_job.job_id;
        message := format('Remote execution attempt %s is %s; nothing reconciled.',
                          v_att_id, v_att_state);
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
        reason, missed_run_time, detected_at, rescheduled_to, detail,
        database_target_id, execution_attempt_id
    ) VALUES (
        v_job.job_id, v_job.job_name, v_job.table_schema, v_job.table_name,
        v_job.is_create, v_reason, p_expected_run_time, v_now, v_next,
        NULLIF(left(concat_ws(' ', NULLIF(btrim(COALESCE(p_detail, '')), ''), v_error,
                              CASE WHEN v_att_error IS NOT NULL
                                   THEN 'Last remote error: ' || v_att_error END), 1000), ''),
        v_job.database_target_id, v_att_id
    )
    ON CONFLICT ON CONSTRAINT partition_job_attention_job_missed_uq DO NOTHING;

    -- Advance the normal cadence. last_run_time / last_run_status are NOT
    -- touched: no execution happened (a miss is not a FAIL).
    UPDATE mubasher_oms.partitioning_job_table AS j
       SET next_run_time = v_next
     WHERE j.job_id = v_job.job_id;

    -- Phase 5D: close the (never dispatched / proven rolled back) attempt.
    IF v_att_id IS NOT NULL THEN
        PERFORM mubasher_oms.partops_remote_close_attempt(v_att_id, v_reason);
    END IF;

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
'Never runs CREATE/DROP and never changes last_run_time/last_run_status. '
'Phase 5D: works for every target of the hub; refuses (SKIPPED_ATTEMPT_ACTIVE) '
'while a remote attempt of the occurrence is DISPATCHING / TARGET_SUCCEEDED / '
'OUTCOME_UNKNOWN, and closes a PREPARED / TARGET_FAILED attempt.';


-- ----------------------------------------------------------------------------
-- 4. Claim: revalidate a remote occurrence / manual run and create or reuse
--    its attempt (PREPARED). Returns the execution snapshot.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_claim(
    p_job_id        numeric,
    p_expected      timestamp without time zone,
    p_kind          text,
    p_executor      text,
    p_attention_id  bigint DEFAULT NULL,
    p_confirm_drop  boolean DEFAULT FALSE
)
RETURNS TABLE (
    decision             text,
    message              text,
    attempt_id           bigint,
    attempt_state        text,
    database_target_id   integer,
    target_key           text,
    operation            text,
    table_schema         text,
    table_name           text,
    partition_unit       text,
    partition_period     integer,
    create_drop_interval text,
    db_config            jsonb,
    failure_count        integer,
    job_name             text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
-- ISO-8601 interval text ("P1M") parses identically under any IntervalStyle
-- on the target connection.
SET intervalstyle = 'iso_8601'
AS $function$
#variable_conflict use_column
DECLARE
    v_kind      text;
    v_job       mubasher_oms.partitioning_job_table%ROWTYPE;
    v_tgt       mubasher_oms.partops_database_target%ROWTYPE;
    v_att       mubasher_oms.partops_execution_attempt%ROWTYPE;
    v_found     boolean := FALSE;
    v_now       timestamp without time zone;
    v_next      timestamp without time zone;
    v_err       text;
    v_cfg       record;
    v_other     record;
    v_attn      mubasher_oms.partition_job_attention%ROWTYPE;
    v_r         record;
    v_new_id    bigint;
BEGIN
    v_kind := upper(btrim(COALESCE(p_kind, '')));
    IF v_kind NOT IN ('SCHEDULED', 'MANUAL') THEN
        RAISE EXCEPTION 'partops_remote_claim: unsupported kind %', p_kind USING ERRCODE = '22023';
    END IF;
    IF (v_kind = 'SCHEDULED') <> (p_expected IS NOT NULL) THEN
        RAISE EXCEPTION 'partops_remote_claim: expected_run_time is required for SCHEDULED only'
            USING ERRCODE = '22023';
    END IF;

    -- Same per-job lock as the local executor / reconcile.
    IF NOT pg_try_advisory_xact_lock(
           87421031,
           (abs(hashtext('partition_job_scheduled:' || p_job_id::text)::bigint) % 2147483647)::integer) THEN
        decision := 'SKIPPED_LOCKED';
        message := 'Another transaction is already processing this job_id.';
        RETURN NEXT;
        RETURN;
    END IF;

    SELECT * INTO v_job FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = p_job_id FOR UPDATE;
    IF NOT FOUND THEN
        decision := 'NOT_FOUND';
        message := 'Job row does not exist.';
        RETURN NEXT;
        RETURN;
    END IF;

    SELECT * INTO v_tgt FROM mubasher_oms.partops_database_target AS t
     WHERE t.target_id = v_job.database_target_id;
    database_target_id := v_job.database_target_id;
    target_key := v_tgt.target_key;
    job_name := v_job.job_name;
    v_now := clock_timestamp()::timestamp without time zone;

    IF v_tgt.is_control_database IS TRUE THEN
        decision := 'NOT_REMOTE';
        message := 'Job belongs to the control database; use the local executor.';
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_kind = 'SCHEDULED' THEN
        SELECT * INTO v_att FROM mubasher_oms.partops_execution_attempt AS x
         WHERE x.execution_kind = 'SCHEDULED'
           AND x.job_id = p_job_id
           AND x.expected_run_time = p_expected
           AND x.database_target_id = v_job.database_target_id
         FOR UPDATE;
        v_found := FOUND;
        IF v_found THEN
            attempt_id := v_att.attempt_id;
            attempt_state := v_att.state;
            failure_count := v_att.failure_count;
            IF v_att.state = 'TARGET_SUCCEEDED' THEN
                decision := 'FINALIZE_PENDING';
                message := 'Target commit already confirmed; only control finalization is pending.';
                RETURN NEXT;
                RETURN;
            ELSIF v_att.state = 'DISPATCHING' THEN
                decision := 'ALREADY_DISPATCHING';
                message := 'Attempt is already dispatching; never dispatched twice.';
                RETURN NEXT;
                RETURN;
            ELSIF v_att.state = 'OUTCOME_UNKNOWN' THEN
                decision := 'NEEDS_REVIEW';
                message := 'Target outcome of this occurrence is unknown; needs review.';
                RETURN NEXT;
                RETURN;
            ELSIF v_att.state = 'FINALIZED' THEN
                decision := 'ALREADY_FINALIZED';
                message := format('Occurrence already finalized (%s).', v_att.result);
                RETURN NEXT;
                RETURN;
            END IF;
        END IF;
    END IF;

    -- Any other attempt of this job that may be running or is unresolved.
    SELECT x.attempt_id, x.state INTO v_other
      FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.job_id = p_job_id
       AND (x.state IN ('OUTCOME_UNKNOWN', 'DISPATCHING', 'TARGET_SUCCEEDED')
            OR (x.execution_kind = 'MANUAL' AND x.state = 'PREPARED'))
       AND (NOT v_found OR x.attempt_id <> v_att.attempt_id)
     ORDER BY (x.state = 'OUTCOME_UNKNOWN') DESC, x.attempt_id DESC
     LIMIT 1;
    IF FOUND THEN
        attempt_id := COALESCE(attempt_id, v_other.attempt_id);
        IF v_other.state = 'OUTCOME_UNKNOWN' THEN
            decision := 'NEEDS_REVIEW';
            message := format('Attempt %s of this job has an unknown target outcome; '
                              'no new execution until it is resolved.', v_other.attempt_id);
        ELSE
            decision := 'BUSY';
            message := format('Attempt %s of this job is %s.', v_other.attempt_id, v_other.state);
        END IF;
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_tgt.is_enabled IS NOT TRUE THEN
        decision := 'SKIPPED_TARGET_DISABLED';
        message := format('Database target %s is disabled.', v_tgt.target_key);
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_kind = 'SCHEDULED' THEN
        IF v_job.is_enabled IS NOT TRUE OR v_job.next_run_time IS DISTINCT FROM p_expected THEN
            IF v_found THEN
                UPDATE mubasher_oms.partops_execution_attempt AS x
                   SET state = 'FINALIZED', finalized_at = v_now,
                       result = CASE WHEN v_job.is_enabled IS NOT TRUE
                                     THEN 'ABANDONED_DISABLED' ELSE 'ABANDONED_RESCHEDULED' END
                 WHERE x.attempt_id = v_att.attempt_id;
            END IF;
            IF v_job.is_enabled IS NOT TRUE THEN
                decision := 'SKIPPED_DISABLED';
                message := 'Job is disabled.';
            ELSE
                decision := 'SKIPPED_RESCHEDULED';
                message := format('Expected next_run_time %s but database has %s.',
                                  p_expected, v_job.next_run_time);
            END IF;
            RETURN NEXT;
            RETURN;
        END IF;
        IF v_job.next_run_time > v_now THEN
            decision := 'SKIPPED_NOT_DUE';
            message := format('Job is not due yet (next_run_time=%s, now=%s).', v_job.next_run_time, v_now);
            RETURN NEXT;
            RETURN;
        END IF;
        -- Phase 5A safety ceiling, unchanged: never execute later than the
        -- maximum execution lateness; reconcile as missed instead (no DDL).
        IF v_now > p_expected + mubasher_oms.partition_job_max_execution_lateness() THEN
            SELECT r.status, r.message INTO v_r
              FROM mubasher_oms.reconcile_missed_partition_job(
                       p_job_id, p_expected, 'MISSED_SCHEDULED_RUN',
                       'Exceeded maximum execution lateness; not executed.') AS r;
            decision := v_r.status;
            message := v_r.message;
            RETURN NEXT;
            RETURN;
        END IF;
    END IF;

    IF v_job.is_create IS NULL THEN
        IF v_kind = 'SCHEDULED' THEN
            -- Same fail-closed behaviour as run_partition_job_scheduled.
            UPDATE mubasher_oms.partitioning_job_table AS j
               SET last_run_time = v_now, last_run_status = 'FAIL', next_run_time = NULL
             WHERE j.job_id = v_job.job_id;
            INSERT INTO mubasher_oms.partitioning_job_table_log (
                job_id, job_name, last_run_status, job_runtime, job_error,
                execution_duration_ms, database_target_id)
            VALUES (v_job.job_id, v_job.job_name, 'FAIL', v_now,
                    'is_create is NULL; refusing to decide CREATE vs DROP.', NULL,
                    v_job.database_target_id);
            IF v_found THEN
                UPDATE mubasher_oms.partops_execution_attempt AS x
                   SET state = 'FINALIZED', finalized_at = v_now, result = 'FAILED_INVALID_SCHEDULE'
                 WHERE x.attempt_id = v_att.attempt_id;
            END IF;
            decision := 'FAILED_INVALID_SCHEDULE';
        ELSE
            decision := 'INVALID_JOB';
        END IF;
        message := 'is_create is NULL; refusing to decide CREATE vs DROP.';
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_kind = 'MANUAL' AND v_job.is_create IS FALSE AND p_confirm_drop IS NOT TRUE THEN
        decision := 'DROP_NOT_CONFIRMED';
        message := 'DROP jobs require explicit confirmation before manual execution.';
        RETURN NEXT;
        RETURN;
    END IF;

    IF v_kind = 'MANUAL' AND p_attention_id IS NOT NULL THEN
        SELECT * INTO v_attn FROM mubasher_oms.partition_job_attention AS a
         WHERE a.attention_id = p_attention_id FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_attn.job_id IS DISTINCT FROM p_job_id THEN
            decision := 'ATTENTION_REJECTED';
            message := 'Attention item not found for this job.';
        ELSIF v_attn.status IS DISTINCT FROM 'PENDING_REVIEW' THEN
            decision := 'ATTENTION_REJECTED';
            message := format('Attention item is already %s; nothing was executed.', v_attn.status);
        ELSIF v_attn.reason = 'REMOTE_OUTCOME_UNKNOWN' THEN
            decision := 'ATTENTION_REJECTED';
            message := 'An unknown remote outcome is never re-run; inspect the target and resolve it.';
        ELSIF v_job.is_create IS DISTINCT FROM v_attn.is_create
              OR v_job.table_schema IS DISTINCT FROM v_attn.table_schema
              OR v_job.table_name IS DISTINCT FROM v_attn.table_name THEN
            decision := 'ATTENTION_REJECTED';
            message := 'Job operation or target table changed since the missed run was detected; '
                       'nothing was executed. Review and acknowledge instead.';
        END IF;
        IF decision IS NOT NULL THEN
            RETURN NEXT;
            RETURN;
        END IF;
    END IF;

    -- db_config_para allowlist (same function as the local executors).
    FOR v_cfg IN SELECT key, value FROM jsonb_each_text(COALESCE(v_job.db_config_para, '{}'::jsonb)) LOOP
        IF NOT mubasher_oms.partition_job_config_param_allowed(v_cfg.key, v_cfg.value) THEN
            decision := 'CONFIG_REJECTED';
            message := format('db_config_para parameter "%s" (or its value) is not allowed by the PartOps allowlist.',
                              left(v_cfg.key, 60));
            RETURN NEXT;
            RETURN;
        END IF;
    END LOOP;

    IF v_kind = 'SCHEDULED' THEN
        -- Validate the next legitimate occurrence BEFORE any target work.
        SELECT c.o_next, c.o_error INTO v_next, v_err
          FROM mubasher_oms.partops_compute_next_run(v_job.job_schedule, p_expected, v_now) AS c;
        IF v_next IS NULL THEN
            UPDATE mubasher_oms.partitioning_job_table AS j
               SET last_run_time = v_now, last_run_status = 'FAIL', next_run_time = NULL
             WHERE j.job_id = v_job.job_id;
            INSERT INTO mubasher_oms.partitioning_job_table_log (
                job_id, job_name, last_run_status, job_runtime, job_error,
                execution_duration_ms, database_target_id)
            VALUES (v_job.job_id, v_job.job_name, 'FAIL', v_now,
                    left('Invalid next_run_time; fail closed: no CREATE/DROP. ' || COALESCE(v_err, ''), 2000),
                    NULL, v_job.database_target_id);
            IF v_found THEN
                UPDATE mubasher_oms.partops_execution_attempt AS x
                   SET state = 'FINALIZED', finalized_at = v_now, result = 'FAILED_INVALID_SCHEDULE'
                 WHERE x.attempt_id = v_att.attempt_id;
            END IF;
            decision := 'FAILED_INVALID_SCHEDULE';
            message := COALESCE(v_err, 'Invalid next_run_time.');
            RETURN NEXT;
            RETURN;
        END IF;

        -- Superseded open attempts of this job (other occurrence / target):
        -- never dispatched or proven rolled back → closed.
        UPDATE mubasher_oms.partops_execution_attempt AS x
           SET state = 'FINALIZED', finalized_at = v_now, result = 'ABANDONED_SUPERSEDED'
         WHERE x.job_id = p_job_id
           AND x.execution_kind = 'SCHEDULED'
           AND x.state IN ('PREPARED', 'TARGET_FAILED')
           AND (NOT v_found OR x.attempt_id <> v_att.attempt_id);
    END IF;

    IF v_found THEN
        UPDATE mubasher_oms.partops_execution_attempt AS x
           SET state = 'PREPARED',
               target_key_snapshot = v_tgt.target_key,
               operation = CASE WHEN v_job.is_create THEN 'CREATE' ELSE 'DROP' END,
               job_name_snapshot = v_job.job_name,
               table_schema_snapshot = v_job.table_schema,
               table_name_snapshot = v_job.table_name,
               partition_unit_snapshot = v_job.partition_unit,
               partition_period_snapshot = v_job.partition_period::integer,
               create_drop_interval_snapshot = v_job.create_drop_interval,
               db_config_snapshot = COALESCE(v_job.db_config_para, '{}'::jsonb),
               planned_next_run_time = v_next,
               executor_instance = left(p_executor, 64),
               dispatch_started_at = NULL
         WHERE x.attempt_id = v_att.attempt_id;
        v_new_id := v_att.attempt_id;
    ELSE
        INSERT INTO mubasher_oms.partops_execution_attempt AS x (
            job_id, database_target_id, target_key_snapshot, execution_kind,
            expected_run_time, operation, job_name_snapshot, table_schema_snapshot,
            table_name_snapshot, partition_unit_snapshot, partition_period_snapshot,
            create_drop_interval_snapshot, db_config_snapshot, planned_next_run_time,
            attention_id, state, executor_instance)
        VALUES (
            v_job.job_id, v_job.database_target_id, v_tgt.target_key, v_kind,
            p_expected, CASE WHEN v_job.is_create THEN 'CREATE' ELSE 'DROP' END,
            v_job.job_name, v_job.table_schema, v_job.table_name, v_job.partition_unit,
            v_job.partition_period::integer, v_job.create_drop_interval,
            COALESCE(v_job.db_config_para, '{}'::jsonb), v_next,
            p_attention_id, 'PREPARED', left(p_executor, 64))
        RETURNING x.attempt_id INTO v_new_id;
    END IF;

    SELECT * INTO v_att FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = v_new_id;
    decision := 'CLAIMED';
    message := NULL;
    attempt_id := v_att.attempt_id;
    attempt_state := v_att.state;
    operation := v_att.operation;
    table_schema := v_att.table_schema_snapshot;
    table_name := v_att.table_name_snapshot;
    partition_unit := v_att.partition_unit_snapshot;
    partition_period := v_att.partition_period_snapshot;
    create_drop_interval := v_att.create_drop_interval_snapshot::text;
    db_config := v_att.db_config_snapshot;
    failure_count := v_att.failure_count;
    RETURN NEXT;
END;
$function$;

COMMENT ON FUNCTION mubasher_oms.partops_remote_claim(numeric, timestamp without time zone, text, text, bigint, boolean) IS
    'Phase 5D: revalidate a remote occurrence (SCHEDULED) or manual run under the '
    'job lock and create/reuse its execution attempt (PREPARED). Decisions: CLAIMED, '
    'FINALIZE_PENDING, ALREADY_DISPATCHING, NEEDS_REVIEW, ALREADY_FINALIZED, BUSY, '
    'SKIPPED_*, MISSED_* (lateness ceiling), FAILED_INVALID_SCHEDULE, CONFIG_REJECTED, '
    'DROP_NOT_CONFIRMED, ATTENTION_REJECTED, INVALID_JOB, NOT_REMOTE, NOT_FOUND. No DDL.';


-- ----------------------------------------------------------------------------
-- 5. State transitions
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_mark_dispatching(
    p_attempt_id bigint,
    p_executor   text
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_state text;
BEGIN
    SELECT x.state INTO v_state FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Execution attempt % not found', p_attempt_id USING ERRCODE = 'P0002';
    END IF;
    IF v_state <> 'PREPARED' THEN
        RETURN v_state;  -- caller must NOT call the worker
    END IF;
    UPDATE mubasher_oms.partops_execution_attempt AS x
       SET state = 'DISPATCHING',
           dispatch_started_at = clock_timestamp()::timestamp without time zone,
           executor_instance = left(p_executor, 64)
     WHERE x.attempt_id = p_attempt_id;
    RETURN 'DISPATCHING';
END;
$function$;

CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_record_success(
    p_attempt_id  bigint,
    p_duration_ms bigint
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_state text;
BEGIN
    SELECT x.state INTO v_state FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Execution attempt % not found', p_attempt_id USING ERRCODE = 'P0002';
    END IF;
    IF v_state IN ('TARGET_SUCCEEDED', 'FINALIZED') THEN
        RETURN v_state;  -- idempotent
    END IF;
    IF v_state <> 'DISPATCHING' THEN
        RAISE EXCEPTION 'Execution attempt % is % (expected DISPATCHING)', p_attempt_id, v_state
            USING ERRCODE = '22023';
    END IF;
    UPDATE mubasher_oms.partops_execution_attempt AS x
       SET state = 'TARGET_SUCCEEDED',
           target_finished_at = clock_timestamp()::timestamp without time zone,
           execution_duration_ms = GREATEST(0, p_duration_ms),
           error_class = NULL, error_summary = NULL
     WHERE x.attempt_id = p_attempt_id;
    RETURN 'TARGET_SUCCEEDED';
END;
$function$;

CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_record_failure(
    p_attempt_id    bigint,
    p_error_class   text,
    p_error_summary text,
    p_duration_ms   bigint DEFAULT NULL
)
RETURNS TABLE (state text, result text, failure_count integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
#variable_conflict use_column
DECLARE
    v_att        mubasher_oms.partops_execution_attempt%ROWTYPE;
    v_now        timestamp without time zone := clock_timestamp()::timestamp without time zone;
    v_class      text := left(upper(COALESCE(NULLIF(btrim(p_error_class), ''), 'UNKNOWN_ERROR')), 40);
    v_summary    text := mubasher_oms.partops_safe_error(p_error_summary);
    v_dispatched boolean;
BEGIN
    SELECT * INTO v_att FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Execution attempt % not found', p_attempt_id USING ERRCODE = 'P0002';
    END IF;
    IF v_att.state NOT IN ('PREPARED', 'DISPATCHING') THEN
        state := v_att.state; result := v_att.result; failure_count := v_att.failure_count;
        RETURN NEXT;  -- idempotent / never downgrades SUCCEEDED or UNKNOWN
        RETURN;
    END IF;
    v_dispatched := v_att.state = 'DISPATCHING';

    UPDATE mubasher_oms.partops_execution_attempt AS x
       SET state = 'TARGET_FAILED',
           failure_count = x.failure_count + 1,
           target_finished_at = CASE WHEN v_dispatched THEN v_now ELSE x.target_finished_at END,
           error_class = v_class,
           error_summary = v_summary,
           execution_duration_ms = CASE WHEN v_dispatched THEN GREATEST(0, p_duration_ms) ELSE x.execution_duration_ms END
     WHERE x.attempt_id = p_attempt_id;

    IF v_dispatched THEN
        -- The worker ran and the target transaction was rolled back: history.
        INSERT INTO mubasher_oms.partitioning_job_table_log (
            job_id, job_name, last_run_status, job_runtime, job_error,
            execution_duration_ms, database_target_id, execution_attempt_id)
        VALUES (v_att.job_id, v_att.job_name_snapshot,
                CASE WHEN v_att.execution_kind = 'MANUAL' THEN 'MANUAL_FAIL' ELSE 'FAIL' END,
                COALESCE(v_att.dispatch_started_at, v_now),
                COALESCE(v_summary, v_class), GREATEST(0, p_duration_ms),
                v_att.database_target_id, v_att.attempt_id);
    END IF;

    IF v_att.execution_kind = 'MANUAL' THEN
        -- Manual runs are never retried automatically.
        UPDATE mubasher_oms.partops_execution_attempt AS x
           SET state = 'FINALIZED', finalized_at = v_now,
               result = CASE WHEN v_dispatched THEN 'MANUAL_FAIL' ELSE 'MANUAL_NOT_RUN' END
         WHERE x.attempt_id = p_attempt_id;
        IF v_dispatched AND v_att.attention_id IS NOT NULL THEN
            UPDATE mubasher_oms.partition_job_attention AS a
               SET manual_execution_result = 'MANUAL_FAIL',
                   manual_execution_message = left(COALESCE(v_summary, v_class), 2000),
                   manual_execution_duration_ms = GREATEST(0, p_duration_ms),
                   manual_executed_at = v_now
             WHERE a.attention_id = v_att.attention_id
               AND a.status = 'PENDING_REVIEW';
        END IF;
    END IF;

    SELECT x.state, x.result, x.failure_count INTO state, result, failure_count
      FROM mubasher_oms.partops_execution_attempt AS x WHERE x.attempt_id = p_attempt_id;
    RETURN NEXT;
END;
$function$;

-- Target outcome cannot be proven: NEVER retried. Evidence + pause the job.
CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_mark_unknown(
    p_attempt_id bigint,
    p_summary    text
)
RETURNS TABLE (decision text, attention_id bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
#variable_conflict use_column
DECLARE
    v_att     mubasher_oms.partops_execution_attempt%ROWTYPE;
    v_job     mubasher_oms.partitioning_job_table%ROWTYPE;
    v_now     timestamp without time zone := clock_timestamp()::timestamp without time zone;
    v_summary text := mubasher_oms.partops_safe_error(p_summary);
    v_detail  text;
    v_attn_id bigint;
BEGIN
    SELECT * INTO v_att FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Execution attempt % not found', p_attempt_id USING ERRCODE = 'P0002';
    END IF;
    IF v_att.state = 'OUTCOME_UNKNOWN' THEN
        SELECT a.attention_id INTO v_attn_id FROM mubasher_oms.partition_job_attention AS a
         WHERE a.execution_attempt_id = p_attempt_id ORDER BY a.attention_id LIMIT 1;
        decision := 'ALREADY_UNKNOWN'; attention_id := v_attn_id;
        RETURN NEXT;
        RETURN;
    END IF;
    IF v_att.state <> 'DISPATCHING' THEN
        decision := 'NOT_APPLICABLE_' || v_att.state; attention_id := NULL;
        RETURN NEXT;
        RETURN;
    END IF;

    UPDATE mubasher_oms.partops_execution_attempt AS x
       SET state = 'OUTCOME_UNKNOWN', target_finished_at = v_now,
           error_class = 'OUTCOME_UNKNOWN', error_summary = v_summary
     WHERE x.attempt_id = p_attempt_id;

    SELECT * INTO v_job FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = v_att.job_id FOR UPDATE;

    v_detail := left(format(
        'Remote %s on target %s (%s.%s): the target outcome could not be confirmed (%s). '
        'DO NOT re-run until the target state has been inspected. Automatic scheduling '
        'of this job is paused until the attempt is resolved.',
        v_att.operation, v_att.target_key_snapshot, v_att.table_schema_snapshot,
        v_att.table_name_snapshot, COALESCE(v_summary, 'no detail')), 1000);

    INSERT INTO mubasher_oms.partition_job_attention AS a (
        job_id, job_name, table_schema, table_name, is_create, reason,
        missed_run_time, detected_at, rescheduled_to, detail,
        database_target_id, execution_attempt_id)
    VALUES (
        v_att.job_id, v_att.job_name_snapshot, v_att.table_schema_snapshot,
        v_att.table_name_snapshot, v_att.operation = 'CREATE', 'REMOTE_OUTCOME_UNKNOWN',
        COALESCE(v_att.expected_run_time, v_att.dispatch_started_at, v_now), v_now, NULL,
        v_detail, v_att.database_target_id, v_att.attempt_id)
    ON CONFLICT ON CONSTRAINT partition_job_attention_job_missed_uq DO NOTHING
    RETURNING a.attention_id INTO v_attn_id;

    -- Pause automatic scheduling of this job (fail closed) until resolution.
    UPDATE mubasher_oms.partitioning_job_table AS j
       SET next_run_time = NULL
     WHERE j.job_id = v_att.job_id;

    INSERT INTO mubasher_oms.partitioning_job_table_log (
        job_id, job_name, last_run_status, job_runtime, job_error,
        execution_duration_ms, database_target_id, execution_attempt_id)
    VALUES (v_att.job_id, v_att.job_name_snapshot,
            CASE WHEN v_att.execution_kind = 'MANUAL' THEN 'MANUAL_OUTCOME_UNKNOWN' ELSE 'OUTCOME_UNKNOWN' END,
            COALESCE(v_att.dispatch_started_at, v_now),
            left('Target outcome could not be confirmed; needs review. ' || COALESCE(v_summary, ''), 2000),
            NULL, v_att.database_target_id, v_att.attempt_id);

    decision := 'MARKED_UNKNOWN';
    attention_id := v_attn_id;
    RETURN NEXT;
END;
$function$;


-- ----------------------------------------------------------------------------
-- 6. Finalization of a confirmed target commit (control DB only; NO DDL)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_finalize(p_attempt_id bigint)
RETURNS TABLE (decision text, result text, next_run_time timestamp without time zone, message text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
#variable_conflict use_column
DECLARE
    v_att   mubasher_oms.partops_execution_attempt%ROWTYPE;
    v_job   mubasher_oms.partitioning_job_table%ROWTYPE;
    v_now   timestamp without time zone := clock_timestamp()::timestamp without time zone;
    v_next  timestamp without time zone;
    v_err   text;
    v_res   text;
    v_ran   timestamp without time zone;
BEGIN
    SELECT * INTO v_att FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Execution attempt % not found', p_attempt_id USING ERRCODE = 'P0002';
    END IF;
    IF v_att.state = 'FINALIZED' THEN
        decision := 'ALREADY_FINALIZED'; result := v_att.result;
        RETURN NEXT;
        RETURN;
    END IF;
    IF v_att.state <> 'TARGET_SUCCEEDED' THEN
        decision := 'NOT_FINALIZABLE'; result := v_att.state;
        message := 'Only a TARGET_SUCCEEDED attempt can be finalized.';
        RETURN NEXT;
        RETURN;
    END IF;
    IF NOT pg_try_advisory_xact_lock(
           87421031,
           (abs(hashtext('partition_job_scheduled:' || v_att.job_id::text)::bigint) % 2147483647)::integer) THEN
        decision := 'RETRY_LATER';
        message := 'Job is locked by another transaction; finalization will be retried.';
        RETURN NEXT;
        RETURN;
    END IF;
    SELECT * INTO v_job FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = v_att.job_id FOR UPDATE;
    v_ran := COALESCE(v_att.dispatch_started_at, v_now);

    IF v_att.execution_kind = 'SCHEDULED' THEN
        v_res := 'SUCCESS';
        INSERT INTO mubasher_oms.partitioning_job_table_log (
            job_id, job_name, last_run_status, job_runtime, job_error,
            execution_duration_ms, database_target_id, execution_attempt_id)
        VALUES (v_att.job_id, v_att.job_name_snapshot, 'SUCCESS', v_ran, NULL,
                v_att.execution_duration_ms, v_att.database_target_id, v_att.attempt_id);
        IF v_job.next_run_time IS NOT DISTINCT FROM v_att.expected_run_time THEN
            v_next := v_att.planned_next_run_time;
            IF v_next IS NULL OR v_next <= v_now THEN
                SELECT c.o_next, c.o_error INTO v_next, v_err
                  FROM mubasher_oms.partops_compute_next_run(v_job.job_schedule, v_att.expected_run_time, v_now) AS c;
            END IF;
            -- is_enabled is never touched (an operator may have disabled it).
            UPDATE mubasher_oms.partitioning_job_table AS j
               SET last_run_time = v_ran, last_run_status = 'SUCCESS', next_run_time = v_next
             WHERE j.job_id = v_att.job_id;
            message := CASE WHEN v_next IS NULL
                            THEN 'Finalized; no valid future schedule (next_run_time cleared). ' || COALESCE(v_err, '')
                            ELSE format('Finalized; next run %s.', v_next) END;
        ELSE
            -- The job moved on meanwhile (edited): record the run, keep its schedule.
            UPDATE mubasher_oms.partitioning_job_table AS j
               SET last_run_time = v_ran, last_run_status = 'SUCCESS'
             WHERE j.job_id = v_att.job_id;
            v_next := v_job.next_run_time;
            message := 'Finalized; job schedule changed meanwhile and was left unchanged.';
        END IF;
    ELSE
        v_res := 'MANUAL_SUCCESS';
        INSERT INTO mubasher_oms.partitioning_job_table_log (
            job_id, job_name, last_run_status, job_runtime, job_error,
            execution_duration_ms, database_target_id, execution_attempt_id)
        VALUES (v_att.job_id, v_att.job_name_snapshot, 'MANUAL_SUCCESS', v_ran, NULL,
                v_att.execution_duration_ms, v_att.database_target_id, v_att.attempt_id);
        IF v_att.attention_id IS NOT NULL THEN
            UPDATE mubasher_oms.partition_job_attention AS a
               SET status = 'MANUALLY_RERUN',
                   reviewed_at = v_now,
                   resolution = left(format('Missed occurrence executed manually on target %s (remote).',
                                            v_att.target_key_snapshot), 500),
                   manual_execution_result = 'MANUAL_SUCCESS',
                   manual_execution_message = format('%s partition operation completed.', v_att.operation),
                   manual_execution_duration_ms = v_att.execution_duration_ms,
                   manual_executed_at = v_now
             WHERE a.attention_id = v_att.attention_id
               AND a.status = 'PENDING_REVIEW';
        END IF;
        v_next := v_job.next_run_time;  -- manual runs never change the schedule
        message := format('%s partition operation completed on target %s.',
                          v_att.operation, v_att.target_key_snapshot);
    END IF;

    UPDATE mubasher_oms.partops_execution_attempt AS x
       SET state = 'FINALIZED', finalized_at = v_now, result = v_res
     WHERE x.attempt_id = p_attempt_id;

    decision := 'FINALIZED';
    result := v_res;
    next_run_time := v_next;
    RETURN NEXT;
END;
$function$;


-- ----------------------------------------------------------------------------
-- 7. Scheduler start: crash recovery (single scheduler per hub)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_remote_recover_attempts(p_executor text)
RETURNS TABLE (attempt_id bigint, previous_state text, action text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
#variable_conflict use_column
DECLARE
    v_rec  record;
    v_now  timestamp without time zone := clock_timestamp()::timestamp without time zone;
BEGIN
    FOR v_rec IN
        SELECT x.attempt_id, x.state, x.execution_kind, x.expected_run_time,
               x.database_target_id, j.is_enabled, j.next_run_time,
               j.database_target_id AS job_target_id
          FROM mubasher_oms.partops_execution_attempt AS x
          LEFT JOIN mubasher_oms.partitioning_job_table AS j ON j.job_id = x.job_id
         WHERE x.state <> 'FINALIZED'
         ORDER BY x.attempt_id
    LOOP
        attempt_id := v_rec.attempt_id;
        previous_state := v_rec.state;
        IF v_rec.state = 'DISPATCHING' THEN
            -- The previous process died while the worker may have been running.
            PERFORM mubasher_oms.partops_remote_mark_unknown(
                v_rec.attempt_id,
                'Scheduler restarted while the target operation may have been in progress.');
            action := 'MARKED_UNKNOWN';
        ELSIF v_rec.state = 'TARGET_SUCCEEDED' THEN
            action := 'FINALIZE';  -- control finalization only; never re-executed
        ELSIF v_rec.state = 'OUTCOME_UNKNOWN' THEN
            action := 'NEEDS_REVIEW';
        ELSIF v_rec.execution_kind = 'MANUAL'
              OR v_rec.is_enabled IS NOT TRUE
              OR v_rec.next_run_time IS DISTINCT FROM v_rec.expected_run_time
              OR v_rec.job_target_id IS DISTINCT FROM v_rec.database_target_id THEN
            -- PREPARED / TARGET_FAILED: nothing applied (never dispatched or
            -- proven rolled back) and no longer live → closed.
            UPDATE mubasher_oms.partops_execution_attempt AS x
               SET state = 'FINALIZED', finalized_at = v_now, result = 'ABANDONED_RESTART'
             WHERE x.attempt_id = v_rec.attempt_id;
            action := 'ABANDONED';
        ELSE
            action := 'RESUMABLE';  -- re-claimed and revalidated by the scheduler
        END IF;
        RETURN NEXT;
    END LOOP;
END;
$function$;


-- ----------------------------------------------------------------------------
-- 8. Operator resolution of an unknown outcome (UI role; no DDL)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_resolve_remote_outcome(
    p_attempt_id bigint,
    p_resolution text,
    p_note       text
)
RETURNS TABLE (result text, next_run_time timestamp without time zone, attention_status text, message text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
#variable_conflict use_column
DECLARE
    v_att   mubasher_oms.partops_execution_attempt%ROWTYPE;
    v_job   mubasher_oms.partitioning_job_table%ROWTYPE;
    v_now   timestamp without time zone := clock_timestamp()::timestamp without time zone;
    v_res   text;
    v_note  text := left(NULLIF(btrim(COALESCE(p_note, '')), ''), 400);
    v_next  timestamp without time zone;
    v_err   text;
BEGIN
    v_res := CASE upper(btrim(COALESCE(p_resolution, '')))
                 WHEN 'APPLIED' THEN 'RESOLVED_APPLIED'
                 WHEN 'NOT_APPLIED' THEN 'RESOLVED_NOT_APPLIED'
             END;
    IF v_res IS NULL THEN
        RAISE EXCEPTION 'resolution must be APPLIED or NOT_APPLIED' USING ERRCODE = '22023';
    END IF;
    IF v_note IS NULL THEN
        RAISE EXCEPTION 'A note describing the inspected target state is required' USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_att FROM mubasher_oms.partops_execution_attempt AS x
     WHERE x.attempt_id = p_attempt_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Execution attempt % not found', p_attempt_id USING ERRCODE = 'P0002';
    END IF;
    IF v_att.state <> 'OUTCOME_UNKNOWN' THEN
        RAISE EXCEPTION 'Execution attempt % is % (only OUTCOME_UNKNOWN can be resolved)',
            p_attempt_id, v_att.state USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_job FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = v_att.job_id FOR UPDATE;

    UPDATE mubasher_oms.partops_execution_attempt AS x
       SET state = 'FINALIZED', finalized_at = v_now, result = v_res
     WHERE x.attempt_id = p_attempt_id;

    INSERT INTO mubasher_oms.partitioning_job_table_log (
        job_id, job_name, last_run_status, job_runtime, job_error,
        execution_duration_ms, database_target_id, execution_attempt_id)
    VALUES (v_att.job_id, v_att.job_name_snapshot, v_res, v_now,
            left('Operator resolution of unknown remote outcome: ' || v_note, 2000),
            NULL, v_att.database_target_id, v_att.attempt_id);

    IF v_res = 'RESOLVED_APPLIED' AND v_att.execution_kind = 'SCHEDULED' THEN
        UPDATE mubasher_oms.partitioning_job_table AS j
           SET last_run_time = COALESCE(v_att.dispatch_started_at, v_now), last_run_status = 'SUCCESS'
         WHERE j.job_id = v_att.job_id;
    END IF;

    -- Resume automatic scheduling (paused by mark_unknown) for enabled jobs.
    v_next := v_job.next_run_time;
    IF v_job.is_enabled IS TRUE AND v_job.next_run_time IS NULL THEN
        SELECT c.o_next, c.o_error INTO v_next, v_err
          FROM mubasher_oms.partops_compute_next_run(v_job.job_schedule, NULL, v_now) AS c;
        UPDATE mubasher_oms.partitioning_job_table AS j
           SET next_run_time = v_next
         WHERE j.job_id = v_att.job_id;
    END IF;

    UPDATE mubasher_oms.partition_job_attention AS a
       SET status = 'ACKNOWLEDGED',
           reviewed_at = v_now,
           resolution = left(v_res || ': ' || v_note, 500)
     WHERE a.execution_attempt_id = p_attempt_id
       AND a.status = 'PENDING_REVIEW';

    result := v_res;
    next_run_time := v_next;
    attention_status := 'ACKNOWLEDGED';
    message := CASE WHEN v_job.is_enabled IS TRUE AND v_next IS NULL
                    THEN 'Resolved; no valid future schedule: ' || COALESCE(v_err, '')
                    ELSE 'Resolved.' END;
    RETURN NEXT;
END;
$function$;


-- ----------------------------------------------------------------------------
-- 9. Move a job to another target (UI role). The job configuration guard
--    refuses while an attempt is open; the registry stays DBA-controlled.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.partops_set_job_target(
    p_job_id    numeric,
    p_target_id integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_old integer;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM mubasher_oms.partops_database_target AS t
                    WHERE t.target_id = p_target_id AND t.is_enabled IS TRUE) THEN
        RAISE EXCEPTION 'Database target % does not exist or is disabled', p_target_id
            USING ERRCODE = '22023';
    END IF;
    SELECT j.database_target_id INTO v_old FROM mubasher_oms.partitioning_job_table AS j
     WHERE j.job_id = p_job_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Job % not found', p_job_id USING ERRCODE = 'P0002';
    END IF;
    IF v_old IS DISTINCT FROM p_target_id THEN
        UPDATE mubasher_oms.partitioning_job_table AS j
           SET database_target_id = p_target_id
         WHERE j.job_id = p_job_id;
    END IF;
    RETURN v_old;
END;
$function$;


-- ----------------------------------------------------------------------------
-- 10. Ownership, PUBLIC revoked, narrow grants
-- ----------------------------------------------------------------------------
ALTER FUNCTION mubasher_oms.partops_remote_claim(numeric, timestamp without time zone, text, text, bigint, boolean) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_remote_mark_dispatching(bigint, text) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_remote_record_success(bigint, bigint) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_remote_record_failure(bigint, text, text, bigint) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_remote_mark_unknown(bigint, text) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_remote_finalize(bigint) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_remote_recover_attempts(text) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_resolve_remote_outcome(bigint, text, text) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.partops_set_job_target(numeric, integer) OWNER TO mubasher_oms;
ALTER FUNCTION mubasher_oms.reconcile_missed_partition_job(numeric, timestamp without time zone, text, text) OWNER TO mubasher_oms;

REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_claim(numeric, timestamp without time zone, text, text, bigint, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_mark_dispatching(bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_record_success(bigint, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_record_failure(bigint, text, text, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_mark_unknown(bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_finalize(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_remote_recover_attempts(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_resolve_remote_outcome(bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.partops_set_job_target(numeric, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION mubasher_oms.reconcile_missed_partition_job(numeric, timestamp without time zone, text, text) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_scheduler') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_get_upcoming_jobs(interval) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_claim(numeric, timestamp without time zone, text, text, bigint, boolean) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_mark_dispatching(bigint, text) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_record_success(bigint, bigint) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_record_failure(bigint, text, text, bigint) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_mark_unknown(bigint, text) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_finalize(bigint) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_recover_attempts(text) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_remote_close_attempt(bigint, text) TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.reconcile_missed_partition_job(numeric, timestamp without time zone, text, text) TO partition_job_scheduler';
    ELSE
        RAISE NOTICE 'Role partition_job_scheduler does not exist — scheduler grants skipped.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_ui') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_resolve_remote_outcome(bigint, text, text) TO partition_job_ui';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_set_job_target(numeric, integer) TO partition_job_ui';
    ELSE
        RAISE NOTICE 'Role partition_job_ui does not exist — UI grants skipped.';
    END IF;
END
$$;

INSERT INTO mubasher_oms.partops_framework_version
    (migration_id, framework_version, description)
VALUES
    ('20261117_04', '20261117_04',
     'Phase 5D: target-aware discovery and safe remote execution control functions '
     '(claim / dispatch / finalize / unknown outcome / recovery / resolution).')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;

-- Verification (read-only):
--   SELECT max(migration_id) FROM mubasher_oms.partops_framework_version;   -- 20261117_04
--   SELECT p.oid::regprocedure, p.prosecdef, p.proconfig FROM pg_proc p
--    WHERE p.pronamespace = 'mubasher_oms'::regnamespace AND p.proname LIKE 'partops_%';
