-- =============================================================================
-- Migration: Phase 5A (part 1) — missed-run attention model + safety helpers
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production. Apply in a DBA window,
-- BEFORE 20261110_02_missed_run_reconcile_retry_and_manual_lock.sql.
--
-- Creates:
--   * mubasher_oms.partition_job_attention
--       Evidence of scheduled occurrences that were NOT executed automatically
--       (missed historical occurrence, or retry budget exhausted) and the DBA
--       review outcome. Separate from partitioning_job_table.last_run_status,
--       which keeps meaning "result of an actual execution".
--   * mubasher_oms.fn_partition_job_attention_guard() + BEFORE UPDATE trigger
--       Evidence columns are immutable; only PENDING_REVIEW rows may change.
--   * mubasher_oms.partition_job_max_execution_lateness()
--       DB safety ceiling (default 6 hours): the database never EXECUTES a
--       scheduled occurrence later than this, whatever the caller claims.
--       The primary historical-vs-owned decision is made by the scheduler.
--   * mubasher_oms.partition_job_config_param_allowed(text, text)
--       Allowlist for db_config_para keys/values applied via set_config().
--
-- Security:
--   * Table and functions owned by mubasher_oms; PUBLIC privileges revoked.
--   * partition_job_scheduler: SELECT, INSERT on the attention table;
--     SELECT on partitioning_job_table_log (read-only, failed-jobs gauge).
--   * partition_job_ui: SELECT + column-level UPDATE of review columns only.
--     No DELETE for anyone. No privilege change on partitioning_job_table.
--
-- Idempotent: safe to re-run (IF NOT EXISTS / CREATE OR REPLACE / DROP TRIGGER
-- IF EXISTS). Changing the ceiling later: CREATE OR REPLACE the helper.
--
-- Rollback (only if 20261110_02 has been rolled back first):
--   DROP TRIGGER IF EXISTS trg_partition_job_attention_guard
--       ON mubasher_oms.partition_job_attention;
--   DROP FUNCTION IF EXISTS mubasher_oms.fn_partition_job_attention_guard();
--   DROP FUNCTION IF EXISTS mubasher_oms.partition_job_config_param_allowed(text, text);
--   DROP FUNCTION IF EXISTS mubasher_oms.partition_job_max_execution_lateness();
--   REVOKE SELECT ON mubasher_oms.partitioning_job_table_log FROM partition_job_scheduler;
--   -- Keep the table unless the evidence is explicitly no longer needed:
--   -- DROP TABLE IF EXISTS mubasher_oms.partition_job_attention;
-- =============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. Attention table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mubasher_oms.partition_job_attention (
    attention_id                 bigint GENERATED ALWAYS AS IDENTITY,
    job_id                       numeric NOT NULL,
    -- Snapshot of the job at detection time (recovery refuses if it changed).
    job_name                     character varying,
    table_schema                 character varying,
    table_name                   character varying,
    is_create                    boolean,
    reason                       character varying(40) NOT NULL,
    missed_run_time              timestamp without time zone NOT NULL,
    detected_at                  timestamp without time zone NOT NULL
        DEFAULT (clock_timestamp()::timestamp without time zone),
    rescheduled_to               timestamp without time zone,
    detail                       character varying(1000),
    status                       character varying(20) NOT NULL
        DEFAULT 'PENDING_REVIEW',
    reviewed_at                  timestamp without time zone,
    resolution                   character varying(500),
    manual_execution_result      character varying(40),
    manual_execution_message     character varying(2000),
    manual_execution_duration_ms bigint,
    manual_executed_at           timestamp without time zone,
    CONSTRAINT partition_job_attention_pkey PRIMARY KEY (attention_id),
    CONSTRAINT partition_job_attention_job_fk
        FOREIGN KEY (job_id)
        REFERENCES mubasher_oms.partitioning_job_table (job_id),
    CONSTRAINT partition_job_attention_job_missed_uq
        UNIQUE (job_id, missed_run_time),
    CONSTRAINT partition_job_attention_reason_ck
        CHECK (reason IN ('MISSED_SCHEDULED_RUN', 'RETRY_EXHAUSTED')),
    CONSTRAINT partition_job_attention_status_ck
        CHECK (status IN ('PENDING_REVIEW', 'MANUALLY_RERUN', 'ACKNOWLEDGED')),
    CONSTRAINT partition_job_attention_reviewed_ck
        CHECK (status = 'PENDING_REVIEW' OR reviewed_at IS NOT NULL)
);

COMMENT ON TABLE mubasher_oms.partition_job_attention IS
    'Scheduled partition-job occurrences that PartOps did NOT execute '
    'automatically (missed historical occurrence or exhausted retry budget). '
    'Requires DBA review: run the missed occurrence manually or acknowledge. '
    'Not an execution log: last_run_status is never set to MISSED.';
COMMENT ON COLUMN mubasher_oms.partition_job_attention.missed_run_time IS
    'The scheduled occurrence (old next_run_time) that was not executed.';
COMMENT ON COLUMN mubasher_oms.partition_job_attention.rescheduled_to IS
    'Future next_run_time computed by cron_to_interval_or_next_run at '
    'detection. NULL = no valid future schedule (fail closed).';

CREATE INDEX IF NOT EXISTS partition_job_attention_pending_idx
    ON mubasher_oms.partition_job_attention (job_id, missed_run_time)
    WHERE status = 'PENDING_REVIEW';

CREATE INDEX IF NOT EXISTS partition_job_attention_detected_idx
    ON mubasher_oms.partition_job_attention (detected_at DESC, attention_id DESC);

ALTER TABLE mubasher_oms.partition_job_attention OWNER TO mubasher_oms;
REVOKE ALL ON TABLE mubasher_oms.partition_job_attention FROM PUBLIC;


-- ----------------------------------------------------------------------------
-- 2. Integrity guard: evidence immutable; terminal states final
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.fn_partition_job_attention_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
BEGIN
    IF NEW.attention_id    IS DISTINCT FROM OLD.attention_id
       OR NEW.job_id          IS DISTINCT FROM OLD.job_id
       OR NEW.job_name        IS DISTINCT FROM OLD.job_name
       OR NEW.table_schema    IS DISTINCT FROM OLD.table_schema
       OR NEW.table_name      IS DISTINCT FROM OLD.table_name
       OR NEW.is_create       IS DISTINCT FROM OLD.is_create
       OR NEW.reason          IS DISTINCT FROM OLD.reason
       OR NEW.missed_run_time IS DISTINCT FROM OLD.missed_run_time
       OR NEW.detected_at     IS DISTINCT FROM OLD.detected_at
       OR NEW.rescheduled_to  IS DISTINCT FROM OLD.rescheduled_to
       OR NEW.detail          IS DISTINCT FROM OLD.detail THEN
        RAISE EXCEPTION
            'partition_job_attention %: detection evidence is immutable',
            OLD.attention_id
            USING ERRCODE = '22023';
    END IF;

    IF OLD.status IS DISTINCT FROM 'PENDING_REVIEW' THEN
        RAISE EXCEPTION
            'partition_job_attention % is already % and cannot be changed',
            OLD.attention_id, OLD.status
            USING ERRCODE = '22023';
    END IF;

    -- A manual re-run can only be recorded together with its successful result.
    IF NEW.status = 'MANUALLY_RERUN'
       AND (NEW.manual_execution_result IS DISTINCT FROM 'MANUAL_SUCCESS'
            OR NEW.manual_executed_at IS NULL) THEN
        RAISE EXCEPTION
            'partition_job_attention %: MANUALLY_RERUN requires a recorded '
            'MANUAL_SUCCESS result',
            OLD.attention_id
            USING ERRCODE = '22023';
    END IF;

    RETURN NEW;
END;
$function$;

ALTER FUNCTION mubasher_oms.fn_partition_job_attention_guard() OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.fn_partition_job_attention_guard() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_partition_job_attention_guard
    ON mubasher_oms.partition_job_attention;

CREATE TRIGGER trg_partition_job_attention_guard
    BEFORE UPDATE ON mubasher_oms.partition_job_attention
    FOR EACH ROW
    EXECUTE PROCEDURE mubasher_oms.fn_partition_job_attention_guard();


-- ----------------------------------------------------------------------------
-- 3. Maximum execution lateness (DB safety ceiling — NOT the missed-run rule)
-- ----------------------------------------------------------------------------
-- The PRIMARY "historically missed" decision is made by the scheduler from
-- ownership: an occurrence it first discovers already overdue (beyond its
-- small jitter tolerance) is reconciled via reconcile_missed_partition_job and
-- never executed; an occurrence it discovered on time ("owned") may execute
-- late after waiting for a concurrency slot / target lock / retry backoff.
--
-- This ceiling is defence in depth against stale or buggy callers: the
-- database refuses to EXECUTE any scheduled occurrence more than this long
-- after its scheduled time, whatever the caller claims, and reconciles it as
-- missed instead. It also bounds how long an owned occurrence can wait.
-- DBA-tunable via CREATE OR REPLACE. Default 6 hours.
CREATE OR REPLACE FUNCTION mubasher_oms.partition_job_max_execution_lateness()
RETURNS interval
LANGUAGE sql
IMMUTABLE
AS $function$
    SELECT interval '6 hours'
$function$;

COMMENT ON FUNCTION mubasher_oms.partition_job_max_execution_lateness() IS
    'Safety ceiling: run_partition_job_scheduled never executes an occurrence '
    'later than scheduled time + this interval (reconciled as missed instead). '
    'Not the primary missed-run rule; the scheduler classifies historical vs '
    'owned occurrences.';

ALTER FUNCTION mubasher_oms.partition_job_max_execution_lateness() OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.partition_job_max_execution_lateness() FROM PUBLIC;


-- ----------------------------------------------------------------------------
-- 4. db_config_para allowlist (mirrored in validators.py — keep in sync)
-- ----------------------------------------------------------------------------
-- Only performance / timeout session parameters. Never identity- or
-- resolution-changing settings (role, session_authorization, search_path, …).
-- DateStyle is allowed ONLY with ISO output (legacy partition routines use
-- 'ISO, DMY'); other styles (e.g. 'Redwood, SHOW_TIME') break psycopg parsing.
CREATE OR REPLACE FUNCTION mubasher_oms.partition_job_config_param_allowed(
    p_key   text,
    p_value text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $function$
    SELECT CASE
        WHEN p_key IS NULL OR p_value IS NULL THEN FALSE
        WHEN length(p_value) < 1 OR length(p_value) > 64 THEN FALSE
        WHEN lower(p_key) = 'datestyle' THEN
            p_value ~* '^[[:space:]]*iso[[:space:]]*(,[[:space:]]*(dmy|mdy|ymd)[[:space:]]*)?$'
        WHEN lower(p_key) IN (
            'lock_timeout',
            'statement_timeout',
            'idle_in_transaction_session_timeout',
            'work_mem',
            'maintenance_work_mem',
            'effective_io_concurrency',
            'random_page_cost',
            'max_parallel_maintenance_workers'
        ) THEN
            p_value ~ '^[A-Za-z0-9][A-Za-z0-9 ._-]*$'
        ELSE FALSE
    END
$function$;

COMMENT ON FUNCTION mubasher_oms.partition_job_config_param_allowed(text, text) IS
    'PartOps db_config_para allowlist. Enforced before every set_config() in '
    'run_partition_job_scheduled / run_partition_job_manual and on edit.';

ALTER FUNCTION mubasher_oms.partition_job_config_param_allowed(text, text)
    OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.partition_job_config_param_allowed(text, text)
    FROM PUBLIC;


-- ----------------------------------------------------------------------------
-- 5. Narrow grants (roles must already exist — DBA creates them)
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_scheduler') THEN
        EXECUTE 'GRANT SELECT, INSERT ON mubasher_oms.partition_job_attention TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partition_job_max_execution_lateness() TO partition_job_scheduler';
        -- Read-only, for partops.jobs.failed_count: a later MANUAL_SUCCESS
        -- clears a failed scheduled run from the gauge (no state rewritten).
        EXECUTE 'GRANT SELECT ON mubasher_oms.partitioning_job_table_log TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partition_job_config_param_allowed(text, text) TO partition_job_scheduler';
    ELSE
        RAISE NOTICE 'Role partition_job_scheduler does not exist — scheduler grants skipped.';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_ui') THEN
        EXECUTE 'GRANT SELECT ON mubasher_oms.partition_job_attention TO partition_job_ui';
        EXECUTE 'GRANT UPDATE (status, reviewed_at, resolution, '
                'manual_execution_result, manual_execution_message, '
                'manual_execution_duration_ms, manual_executed_at) '
                'ON mubasher_oms.partition_job_attention TO partition_job_ui';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partition_job_config_param_allowed(text, text) TO partition_job_ui';
    ELSE
        RAISE NOTICE 'Role partition_job_ui does not exist — UI grants skipped.';
    END IF;
END
$$;

COMMIT;

-- Verification (read-only):
--   SELECT to_regclass('mubasher_oms.partition_job_attention');
--   SELECT mubasher_oms.partition_job_max_execution_lateness();
--   SELECT mubasher_oms.partition_job_config_param_allowed('lock_timeout', '30s');   -- true
--   SELECT mubasher_oms.partition_job_config_param_allowed('search_path', 'x');      -- false
--   SELECT mubasher_oms.partition_job_config_param_allowed('datestyle', 'ISO, DMY'); -- true
