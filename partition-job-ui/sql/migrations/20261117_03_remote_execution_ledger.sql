-- =============================================================================
-- Migration: Phase 5D (part 1) — remote execution attempt ledger (CONTROL DB)
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production. Apply in a DBA window with
-- the scheduler STOPPED, after 20261117_02, as the schema owner.
--
-- Architecture: ONE PARTOPS INSTANCE PER HUB / ~2 TARGET DATABASES PER HUB /
-- NO GLOBAL MULTI-HUB CONTROL PLANE. This migration runs in the CONTROL
-- database only. Managed target databases get NO PartOps tables.
--
-- Why a ledger: a remote job touches TWO databases (control + target). There is
-- no atomic transaction across them and PartOps does NOT use distributed or
-- prepared transactions. The ledger records, in the control database, how far
-- each remote attempt got, so that an attempt whose target outcome cannot be
-- proven is NEVER re-executed automatically (OUTCOME_UNKNOWN → review) and an
-- attempt whose target commit is known is only FINALIZED, never re-run.
--
-- Creates / changes:
--   * mubasher_oms.partops_execution_attempt (+ state-transition guard)
--   * partition_job_attention: reason REMOTE_OUTCOME_UNKNOWN; database_target_id
--     and execution_attempt_id evidence columns (immutable); unknown-outcome
--     items cannot be "re-run" and can only be closed after resolution
--   * partitioning_job_table_log: database_target_id, execution_attempt_id
--     (NULL = control database / pre-5D row; no backfill, no table rewrite)
--   * partops_job_open_attempt_state(job_id) + BEFORE UPDATE guard on
--     partitioning_job_table: configuration that defines WHAT is executed
--     WHERE cannot change while a remote attempt is in flight or unresolved
--   * BEFORE INSERT hook: a new job may request its target via the
--     transaction-local setting partops.requested_target_id (validated)
--
-- App roles get SELECT on the ledger only. Every ledger write goes through the
-- SECURITY DEFINER functions of 20261117_04.
-- Idempotent: safe to re-run.
--
-- Rollback (only after 20261117_04 has been rolled back; see RUNBOOK §10.8):
--   DROP TRIGGER IF EXISTS trg_partops_job_execution_guard ON mubasher_oms.partitioning_job_table;
--   DROP TRIGGER IF EXISTS trg_partops_job_target_on_insert ON mubasher_oms.partitioning_job_table;
--   DROP FUNCTION IF EXISTS mubasher_oms.fn_partops_job_execution_guard();
--   DROP FUNCTION IF EXISTS mubasher_oms.fn_partops_job_target_on_insert();
--   DROP FUNCTION IF EXISTS mubasher_oms.partops_job_open_attempt_state(numeric);
--   -- keep evidence unless explicitly no longer needed:
--   -- ALTER TABLE ... partition_job_attention DROP COLUMN execution_attempt_id, DROP COLUMN database_target_id;
--   -- DROP TABLE mubasher_oms.partops_execution_attempt;
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('mubasher_oms.partops_framework_version') IS NULL
       OR NOT EXISTS (SELECT 1 FROM mubasher_oms.partops_framework_version
                       WHERE migration_id = '20261117_02') THEN
        RAISE EXCEPTION 'Apply 20261117_02_target_aware_executors.sql first.';
    END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- 1. Execution attempt ledger
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mubasher_oms.partops_execution_attempt (
    attempt_id                    bigint GENERATED ALWAYS AS IDENTITY,
    job_id                        numeric NOT NULL,
    database_target_id            integer NOT NULL,
    target_key_snapshot           character varying(32) NOT NULL,
    execution_kind                character varying(10) NOT NULL,
    -- Scheduled occurrence (NULL for manual runs).
    expected_run_time             timestamp without time zone,
    operation                     character varying(6) NOT NULL,
    -- Snapshot of WHAT is executed (taken under the job row lock at claim).
    job_name_snapshot             character varying,
    table_schema_snapshot         character varying NOT NULL,
    table_name_snapshot           character varying NOT NULL,
    partition_unit_snapshot       character varying,
    partition_period_snapshot     integer,
    create_drop_interval_snapshot interval,
    db_config_snapshot            jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Validated future occurrence computed BEFORE dispatch (scheduled only).
    planned_next_run_time         timestamp without time zone,
    -- Manual recovery of a missed-run attention item (optional).
    attention_id                  bigint,
    state                         character varying(20) NOT NULL DEFAULT 'PREPARED',
    failure_count                 integer NOT NULL DEFAULT 0,
    -- Which scheduler process claimed it (diagnostics only; no host/secret).
    executor_instance             character varying(64),
    created_at                    timestamp without time zone NOT NULL
        DEFAULT (clock_timestamp()::timestamp without time zone),
    updated_at                    timestamp without time zone NOT NULL
        DEFAULT (clock_timestamp()::timestamp without time zone),
    dispatch_started_at           timestamp without time zone,
    target_finished_at            timestamp without time zone,
    finalized_at                  timestamp without time zone,
    result                        character varying(40),
    error_class                   character varying(40),
    error_summary                 character varying(500),
    execution_duration_ms         bigint,
    CONSTRAINT partops_execution_attempt_pkey PRIMARY KEY (attempt_id),
    CONSTRAINT partops_execution_attempt_job_fk
        FOREIGN KEY (job_id) REFERENCES mubasher_oms.partitioning_job_table (job_id),
    CONSTRAINT partops_execution_attempt_target_fk
        FOREIGN KEY (database_target_id)
        REFERENCES mubasher_oms.partops_database_target (target_id) ON DELETE RESTRICT,
    CONSTRAINT partops_execution_attempt_kind_ck
        CHECK (execution_kind IN ('SCHEDULED', 'MANUAL')),
    CONSTRAINT partops_execution_attempt_operation_ck
        CHECK (operation IN ('CREATE', 'DROP')),
    CONSTRAINT partops_execution_attempt_state_ck
        CHECK (state IN ('PREPARED', 'DISPATCHING', 'TARGET_SUCCEEDED',
                         'TARGET_FAILED', 'OUTCOME_UNKNOWN', 'FINALIZED')),
    CONSTRAINT partops_execution_attempt_expected_ck
        CHECK ((execution_kind = 'SCHEDULED') = (expected_run_time IS NOT NULL)),
    CONSTRAINT partops_execution_attempt_finalized_ck
        CHECK (state <> 'FINALIZED' OR (finalized_at IS NOT NULL AND result IS NOT NULL)),
    CONSTRAINT partops_execution_attempt_dispatch_ck
        CHECK (state NOT IN ('DISPATCHING', 'TARGET_SUCCEEDED', 'OUTCOME_UNKNOWN')
               OR dispatch_started_at IS NOT NULL),
    CONSTRAINT partops_execution_attempt_failures_ck CHECK (failure_count >= 0)
);

COMMENT ON TABLE mubasher_oms.partops_execution_attempt IS
    'Phase 5D: one row per remote execution attempt (scheduled occurrence or '
    'manual run) of a job on a managed target database. State machine: '
    'PREPARED → DISPATCHING → TARGET_SUCCEEDED → FINALIZED; proven target '
    'rollback → TARGET_FAILED (bounded retry → PREPARED, or FINALIZED); '
    'unprovable outcome → OUTCOME_UNKNOWN (never retried; operator resolution → '
    'FINALIZED). Written only by SECURITY DEFINER functions. No credentials.';
COMMENT ON COLUMN mubasher_oms.partops_execution_attempt.state IS
    'PREPARED: claimed, nothing sent to the target. DISPATCHING: the worker may '
    'be running on the target. TARGET_SUCCEEDED: target COMMIT confirmed; '
    'control finalization pending (never re-executed). TARGET_FAILED: target '
    'rolled back (proven); retry-eligible. OUTCOME_UNKNOWN: commit could not be '
    'proven either way; needs review. FINALIZED: terminal.';

-- One attempt row per scheduled occurrence (retries reuse it).
CREATE UNIQUE INDEX IF NOT EXISTS partops_execution_attempt_occurrence_uq
    ON mubasher_oms.partops_execution_attempt (database_target_id, job_id, expected_run_time)
    WHERE execution_kind = 'SCHEDULED';
-- At most one in-flight manual attempt per job.
CREATE UNIQUE INDEX IF NOT EXISTS partops_execution_attempt_manual_active_uq
    ON mubasher_oms.partops_execution_attempt (job_id)
    WHERE execution_kind = 'MANUAL'
      AND state IN ('PREPARED', 'DISPATCHING', 'TARGET_SUCCEEDED');
CREATE INDEX IF NOT EXISTS partops_execution_attempt_open_idx
    ON mubasher_oms.partops_execution_attempt (state, job_id)
    WHERE state <> 'FINALIZED';
CREATE INDEX IF NOT EXISTS partops_execution_attempt_job_idx
    ON mubasher_oms.partops_execution_attempt (job_id, created_at DESC);

ALTER TABLE mubasher_oms.partops_execution_attempt OWNER TO mubasher_oms;
REVOKE ALL ON TABLE mubasher_oms.partops_execution_attempt FROM PUBLIC;

-- ----------------------------------------------------------------------------
-- 2. Ledger guard: legal state transitions; identity immutable
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.fn_partops_execution_attempt_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
BEGIN
    IF NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.execution_kind IS DISTINCT FROM OLD.execution_kind
       OR NEW.expected_run_time IS DISTINCT FROM OLD.expected_run_time
       OR NEW.database_target_id IS DISTINCT FROM OLD.database_target_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.attention_id IS DISTINCT FROM OLD.attention_id THEN
        RAISE EXCEPTION 'partops_execution_attempt %: identity is immutable', OLD.attempt_id
            USING ERRCODE = '22023';
    END IF;

    -- The execution snapshot may only be refreshed while re-claiming
    -- (TARGET_FAILED/PREPARED → PREPARED): nothing was applied on the target.
    IF (NEW.operation IS DISTINCT FROM OLD.operation
        OR NEW.table_schema_snapshot IS DISTINCT FROM OLD.table_schema_snapshot
        OR NEW.table_name_snapshot IS DISTINCT FROM OLD.table_name_snapshot
        OR NEW.partition_unit_snapshot IS DISTINCT FROM OLD.partition_unit_snapshot
        OR NEW.partition_period_snapshot IS DISTINCT FROM OLD.partition_period_snapshot
        OR NEW.create_drop_interval_snapshot IS DISTINCT FROM OLD.create_drop_interval_snapshot
        OR NEW.db_config_snapshot IS DISTINCT FROM OLD.db_config_snapshot
        OR NEW.planned_next_run_time IS DISTINCT FROM OLD.planned_next_run_time)
       AND NOT (NEW.state = 'PREPARED' AND OLD.state IN ('PREPARED', 'TARGET_FAILED')) THEN
        RAISE EXCEPTION
            'partops_execution_attempt %: execution snapshot can only change while re-claiming',
            OLD.attempt_id
            USING ERRCODE = '22023';
    END IF;

    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
           (OLD.state = 'PREPARED'         AND NEW.state IN ('DISPATCHING', 'TARGET_FAILED', 'FINALIZED'))
        OR (OLD.state = 'DISPATCHING'      AND NEW.state IN ('TARGET_SUCCEEDED', 'TARGET_FAILED', 'OUTCOME_UNKNOWN'))
        OR (OLD.state = 'TARGET_SUCCEEDED' AND NEW.state = 'FINALIZED')
        OR (OLD.state = 'TARGET_FAILED'    AND NEW.state IN ('PREPARED', 'FINALIZED'))
        OR (OLD.state = 'OUTCOME_UNKNOWN'  AND NEW.state = 'FINALIZED')
    ) THEN
        RAISE EXCEPTION 'partops_execution_attempt %: illegal transition % -> %',
            OLD.attempt_id, OLD.state, NEW.state
            USING ERRCODE = '22023';
    END IF;

    IF OLD.state = 'FINALIZED' THEN
        RAISE EXCEPTION 'partops_execution_attempt % is FINALIZED', OLD.attempt_id
            USING ERRCODE = '22023';
    END IF;

    -- An attempt that reached the target can never become "never dispatched".
    IF OLD.dispatch_started_at IS NOT NULL
       AND NEW.dispatch_started_at IS DISTINCT FROM OLD.dispatch_started_at
       AND NEW.state IS DISTINCT FROM 'PREPARED' THEN
        RAISE EXCEPTION 'partops_execution_attempt %: dispatch time is immutable', OLD.attempt_id
            USING ERRCODE = '22023';
    END IF;

    NEW.updated_at := clock_timestamp()::timestamp without time zone;
    RETURN NEW;
END;
$function$;

ALTER FUNCTION mubasher_oms.fn_partops_execution_attempt_guard() OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.fn_partops_execution_attempt_guard() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_partops_execution_attempt_guard
    ON mubasher_oms.partops_execution_attempt;
CREATE TRIGGER trg_partops_execution_attempt_guard
    BEFORE UPDATE ON mubasher_oms.partops_execution_attempt
    FOR EACH ROW
    EXECUTE PROCEDURE mubasher_oms.fn_partops_execution_attempt_guard();

-- ----------------------------------------------------------------------------
-- 3. Attention: remote unknown outcome evidence
-- ----------------------------------------------------------------------------
ALTER TABLE mubasher_oms.partition_job_attention
    ADD COLUMN IF NOT EXISTS database_target_id integer,
    ADD COLUMN IF NOT EXISTS execution_attempt_id bigint;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'partition_job_attention_target_fk'
                      AND conrelid = 'mubasher_oms.partition_job_attention'::regclass) THEN
        ALTER TABLE mubasher_oms.partition_job_attention
            ADD CONSTRAINT partition_job_attention_target_fk
            FOREIGN KEY (database_target_id)
            REFERENCES mubasher_oms.partops_database_target (target_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'partition_job_attention_attempt_fk'
                      AND conrelid = 'mubasher_oms.partition_job_attention'::regclass) THEN
        ALTER TABLE mubasher_oms.partition_job_attention
            ADD CONSTRAINT partition_job_attention_attempt_fk
            FOREIGN KEY (execution_attempt_id)
            REFERENCES mubasher_oms.partops_execution_attempt (attempt_id);
    END IF;
END
$$;

ALTER TABLE mubasher_oms.partition_job_attention
    DROP CONSTRAINT IF EXISTS partition_job_attention_reason_ck;
ALTER TABLE mubasher_oms.partition_job_attention
    ADD CONSTRAINT partition_job_attention_reason_ck
    CHECK (reason IN ('MISSED_SCHEDULED_RUN', 'RETRY_EXHAUSTED', 'REMOTE_OUTCOME_UNKNOWN'));

COMMENT ON COLUMN mubasher_oms.partition_job_attention.database_target_id IS
    'Phase 5D: managed target of the occurrence (NULL = control database / pre-5D).';
COMMENT ON COLUMN mubasher_oms.partition_job_attention.execution_attempt_id IS
    'Phase 5D: remote execution attempt (REMOTE_OUTCOME_UNKNOWN / exhausted remote retries).';

-- Same guard as 20261110_01 plus: new evidence columns immutable; an unknown
-- remote outcome can never be "re-run" and can only be closed after the
-- attempt itself was resolved (partops_resolve_remote_outcome).
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
       OR NEW.detail          IS DISTINCT FROM OLD.detail
       OR NEW.database_target_id   IS DISTINCT FROM OLD.database_target_id
       OR NEW.execution_attempt_id IS DISTINCT FROM OLD.execution_attempt_id THEN
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

    IF NEW.status = 'MANUALLY_RERUN'
       AND (NEW.manual_execution_result IS DISTINCT FROM 'MANUAL_SUCCESS'
            OR NEW.manual_executed_at IS NULL) THEN
        RAISE EXCEPTION
            'partition_job_attention %: MANUALLY_RERUN requires a recorded '
            'MANUAL_SUCCESS result',
            OLD.attention_id
            USING ERRCODE = '22023';
    END IF;

    IF OLD.reason = 'REMOTE_OUTCOME_UNKNOWN' THEN
        IF NEW.status = 'MANUALLY_RERUN' OR NEW.manual_execution_result IS NOT NULL THEN
            RAISE EXCEPTION
                'partition_job_attention %: an unknown remote outcome is never re-run; '
                'inspect the target and resolve the execution attempt instead',
                OLD.attention_id
                USING ERRCODE = '22023';
        END IF;
        IF NEW.status <> 'PENDING_REVIEW'
           AND EXISTS (SELECT 1 FROM mubasher_oms.partops_execution_attempt AS x
                        WHERE x.attempt_id = OLD.execution_attempt_id
                          AND x.state <> 'FINALIZED') THEN
            RAISE EXCEPTION
                'partition_job_attention %: resolve execution attempt % first '
                '(target outcome still unknown)',
                OLD.attention_id, OLD.execution_attempt_id
                USING ERRCODE = '22023';
        END IF;
    END IF;

    RETURN NEW;
END;
$function$;

ALTER FUNCTION mubasher_oms.fn_partition_job_attention_guard() OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.fn_partition_job_attention_guard() FROM PUBLIC;

-- ----------------------------------------------------------------------------
-- 4. History: which target / attempt (NULL = control database, pre-5D)
-- ----------------------------------------------------------------------------
ALTER TABLE mubasher_oms.partitioning_job_table_log
    ADD COLUMN IF NOT EXISTS database_target_id integer,
    ADD COLUMN IF NOT EXISTS execution_attempt_id bigint;

COMMENT ON COLUMN mubasher_oms.partitioning_job_table_log.database_target_id IS
    'Phase 5D: target the run executed on. NULL = control database (local '
    'executors and all pre-5D rows).';
COMMENT ON COLUMN mubasher_oms.partitioning_job_table_log.execution_attempt_id IS
    'Phase 5D: partops_execution_attempt of a remote run (NULL for local runs).';

-- ----------------------------------------------------------------------------
-- 5. Open-attempt state of a job + configuration guard
-- ----------------------------------------------------------------------------
-- Returns the most significant non-final attempt state of a job, or NULL.
--   OUTCOME_UNKNOWN > DISPATCHING > TARGET_SUCCEEDED > PREPARED/TARGET_FAILED
-- PREPARED / TARGET_FAILED are reported only while still "live" (manual, or the
-- job is still enabled at that very occurrence): nothing was applied on the
-- target for them, and a later claim revalidates and re-snapshots the job.
CREATE OR REPLACE FUNCTION mubasher_oms.partops_job_open_attempt_state(p_job_id numeric)
RETURNS text
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
    SELECT x.state
      FROM mubasher_oms.partops_execution_attempt AS x
      LEFT JOIN mubasher_oms.partitioning_job_table AS j ON j.job_id = x.job_id
     WHERE x.job_id = p_job_id
       AND x.state <> 'FINALIZED'
       AND (x.state IN ('OUTCOME_UNKNOWN', 'DISPATCHING', 'TARGET_SUCCEEDED')
            OR x.execution_kind = 'MANUAL'
            OR (j.is_enabled IS TRUE AND j.next_run_time = x.expected_run_time))
     ORDER BY CASE x.state
                  WHEN 'OUTCOME_UNKNOWN' THEN 1
                  WHEN 'DISPATCHING' THEN 2
                  WHEN 'TARGET_SUCCEEDED' THEN 3
                  ELSE 4
              END,
              x.attempt_id DESC
     LIMIT 1
$function$;

COMMENT ON FUNCTION mubasher_oms.partops_job_open_attempt_state(numeric) IS
    'Phase 5D: most significant open remote execution attempt state of a job '
    '(NULL = none). Used by the job configuration guard and the API.';

ALTER FUNCTION mubasher_oms.partops_job_open_attempt_state(numeric) OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.partops_job_open_attempt_state(numeric) FROM PUBLIC;

-- Configuration that defines WHAT runs WHERE must not change while a remote
-- target operation may be running, its commit is confirmed but not yet
-- recorded, or its outcome is unknown. Disabling is always allowed (it never
-- interrupts target DDL already in progress); re-enabling is refused while an
-- outcome is unknown. Moving a job to another target additionally requires
-- that no live PREPARED / TARGET_FAILED attempt exists.
CREATE OR REPLACE FUNCTION mubasher_oms.fn_partops_job_execution_guard()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_state text;
BEGIN
    v_state := mubasher_oms.partops_job_open_attempt_state(OLD.job_id);
    IF v_state IS NULL THEN
        RETURN NEW;
    END IF;

    IF v_state IN ('OUTCOME_UNKNOWN', 'DISPATCHING', 'TARGET_SUCCEEDED')
       AND (NEW.database_target_id IS DISTINCT FROM OLD.database_target_id
            OR NEW.table_schema IS DISTINCT FROM OLD.table_schema
            OR NEW.table_name IS DISTINCT FROM OLD.table_name
            OR NEW.is_create IS DISTINCT FROM OLD.is_create
            OR NEW.job_schedule IS DISTINCT FROM OLD.job_schedule
            OR NEW.partition_unit IS DISTINCT FROM OLD.partition_unit
            OR NEW.partition_period IS DISTINCT FROM OLD.partition_period
            OR NEW.create_drop_interval IS DISTINCT FROM OLD.create_drop_interval
            OR NEW.db_config_para IS DISTINCT FROM OLD.db_config_para) THEN
        RAISE EXCEPTION
            'Job % has a remote execution attempt in state %; its target, table, '
            'operation, schedule and configuration cannot change until it is '
            'finalized or resolved.', OLD.job_id, v_state
            USING ERRCODE = '55006';
    END IF;

    IF v_state = 'OUTCOME_UNKNOWN'
       AND OLD.is_enabled IS NOT TRUE AND NEW.is_enabled IS TRUE THEN
        RAISE EXCEPTION
            'Job % cannot be re-enabled: a remote execution outcome is unknown. '
            'Inspect the target and resolve the attempt first.', OLD.job_id
            USING ERRCODE = '55006';
    END IF;

    IF NEW.database_target_id IS DISTINCT FROM OLD.database_target_id THEN
        RAISE EXCEPTION
            'Job % has an open execution attempt (%); its database target cannot '
            'change now.', OLD.job_id, v_state
            USING ERRCODE = '55006';
    END IF;
    RETURN NEW;
END;
$function$;

ALTER FUNCTION mubasher_oms.fn_partops_job_execution_guard() OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.fn_partops_job_execution_guard() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_partops_job_execution_guard
    ON mubasher_oms.partitioning_job_table;
CREATE TRIGGER trg_partops_job_execution_guard
    BEFORE UPDATE ON mubasher_oms.partitioning_job_table
    FOR EACH ROW
    WHEN (NEW.database_target_id IS DISTINCT FROM OLD.database_target_id
          OR NEW.table_schema IS DISTINCT FROM OLD.table_schema
          OR NEW.table_name IS DISTINCT FROM OLD.table_name
          OR NEW.is_create IS DISTINCT FROM OLD.is_create
          OR NEW.job_schedule IS DISTINCT FROM OLD.job_schedule
          OR NEW.partition_unit IS DISTINCT FROM OLD.partition_unit
          OR NEW.partition_period IS DISTINCT FROM OLD.partition_period
          OR NEW.create_drop_interval IS DISTINCT FROM OLD.create_drop_interval
          OR NEW.db_config_para IS DISTINCT FROM OLD.db_config_para
          OR NEW.is_enabled IS DISTINCT FROM OLD.is_enabled)
    EXECUTE PROCEDURE mubasher_oms.fn_partops_job_execution_guard();

-- ----------------------------------------------------------------------------
-- 6. New job → requested target (the legacy insert function is unaware of
--    targets; the API sets partops.requested_target_id for its transaction)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mubasher_oms.fn_partops_job_target_on_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, mubasher_oms, pg_temp
AS $function$
DECLARE
    v_raw    text;
    v_target integer;
BEGIN
    v_raw := NULLIF(btrim(COALESCE(current_setting('partops.requested_target_id', true), '')), '');
    IF v_raw IS NULL THEN
        RETURN NEW;  -- column DEFAULT: control database
    END IF;
    IF v_raw !~ '^[0-9]{1,9}$' THEN
        RAISE EXCEPTION 'partops.requested_target_id must be a target id' USING ERRCODE = '22023';
    END IF;
    SELECT t.target_id INTO v_target
      FROM mubasher_oms.partops_database_target AS t
     WHERE t.target_id = v_raw::integer AND t.is_enabled IS TRUE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Database target % does not exist or is disabled', v_raw
            USING ERRCODE = '22023';
    END IF;
    NEW.database_target_id := v_target;
    RETURN NEW;
END;
$function$;

ALTER FUNCTION mubasher_oms.fn_partops_job_target_on_insert() OWNER TO mubasher_oms;
REVOKE ALL ON FUNCTION mubasher_oms.fn_partops_job_target_on_insert() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_partops_job_target_on_insert
    ON mubasher_oms.partitioning_job_table;
CREATE TRIGGER trg_partops_job_target_on_insert
    BEFORE INSERT ON mubasher_oms.partitioning_job_table
    FOR EACH ROW
    EXECUTE PROCEDURE mubasher_oms.fn_partops_job_target_on_insert();

-- ----------------------------------------------------------------------------
-- 7. Grants: read-only ledger for both app roles
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_scheduler') THEN
        EXECUTE 'GRANT SELECT ON mubasher_oms.partops_execution_attempt TO partition_job_scheduler';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_job_open_attempt_state(numeric) TO partition_job_scheduler';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_ui') THEN
        EXECUTE 'GRANT SELECT ON mubasher_oms.partops_execution_attempt TO partition_job_ui';
        EXECUTE 'GRANT EXECUTE ON FUNCTION mubasher_oms.partops_job_open_attempt_state(numeric) TO partition_job_ui';
    END IF;
END
$$;

INSERT INTO mubasher_oms.partops_framework_version
    (migration_id, framework_version, description)
VALUES
    ('20261117_03', '20261117_03',
     'Phase 5D: remote execution attempt ledger, unknown-outcome attention, '
     'target/attempt history columns, job configuration guard.')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
