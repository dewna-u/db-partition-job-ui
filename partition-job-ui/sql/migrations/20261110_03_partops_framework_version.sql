-- =============================================================================
-- Migration: Phase 5B — PartOps database framework version record
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production. Apply after 20261110_02.
--
-- Creates mubasher_oms.partops_framework_version: one row per applied PartOps
-- framework migration that declares a framework version. Purpose is
-- COMPATIBILITY CHECKING ONLY:
--
--     application expects framework version  = sql/framework/framework_manifest.json
--                                               "framework_version"
--     database reports framework version     = max(migration_id) in this table
--
-- The application and scheduler only ever READ this table (SELECT). Nothing in
-- PartOps writes it at runtime and nothing is "auto-upgraded" when versions
-- differ: a DBA applies the reviewed migration, which records its own row.
--
-- The row is recorded ONLY after this migration verifies that the PartOps-owned
-- objects of this framework version exist (fails closed otherwise). It does NOT
-- claim that the legacy/reference functions (cron helper, workers, insert
-- function) are present or reviewed — preflight checks those separately.
--
-- Security: owner mubasher_oms; PUBLIC revoked; SELECT only for app roles.
-- Idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING).
--
-- Rollback (compatibility record only; no business data):
--   DELETE FROM mubasher_oms.partops_framework_version WHERE migration_id = '20261110_03';
--   -- or, to remove the mechanism entirely:
--   DROP TABLE IF EXISTS mubasher_oms.partops_framework_version;
-- =============================================================================

BEGIN;

-- Fail closed: the version row must describe objects that really exist.
DO $$
DECLARE
    v_missing text := '';
BEGIN
    IF to_regclass('mubasher_oms.partition_job_attention') IS NULL THEN
        v_missing := v_missing || ' partition_job_attention';
    END IF;
    IF to_regprocedure('mubasher_oms.reconcile_missed_partition_job(numeric,timestamp without time zone,text,text)') IS NULL THEN
        v_missing := v_missing || ' reconcile_missed_partition_job';
    END IF;
    IF to_regprocedure('mubasher_oms.run_partition_job_scheduled(numeric,timestamp without time zone)') IS NULL THEN
        v_missing := v_missing || ' run_partition_job_scheduled';
    END IF;
    IF to_regprocedure('mubasher_oms.run_partition_job_manual(numeric)') IS NULL THEN
        v_missing := v_missing || ' run_partition_job_manual';
    END IF;
    IF to_regprocedure('mubasher_oms.run_partition_job_attention_recovery(bigint)') IS NULL THEN
        v_missing := v_missing || ' run_partition_job_attention_recovery';
    END IF;
    IF to_regprocedure('mubasher_oms.acknowledge_partition_job_attention(bigint,text)') IS NULL THEN
        v_missing := v_missing || ' acknowledge_partition_job_attention';
    END IF;
    IF to_regprocedure('mubasher_oms.partition_job_max_execution_lateness()') IS NULL THEN
        v_missing := v_missing || ' partition_job_max_execution_lateness';
    END IF;
    IF to_regprocedure('mubasher_oms.partition_job_config_param_allowed(text,text)') IS NULL THEN
        v_missing := v_missing || ' partition_job_config_param_allowed';
    END IF;
    IF v_missing <> '' THEN
        RAISE EXCEPTION
            'Framework version not recorded: PartOps objects missing:% — apply '
            '20261110_01 and 20261110_02 first.', v_missing;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS mubasher_oms.partops_framework_version (
    migration_id      character varying(32) NOT NULL,
    framework_version character varying(32) NOT NULL,
    description       character varying(500) NOT NULL,
    applied_at        timestamp without time zone NOT NULL
        DEFAULT (clock_timestamp()::timestamp without time zone),
    applied_by        name NOT NULL DEFAULT session_user,
    CONSTRAINT partops_framework_version_pkey PRIMARY KEY (migration_id),
    CONSTRAINT partops_framework_version_id_ck
        CHECK (migration_id ~ '^[0-9]{8}_[0-9]{2}$')
);

COMMENT ON TABLE mubasher_oms.partops_framework_version IS
    'Applied PartOps framework migrations. Compatibility check only: the '
    'database framework version is max(migration_id). Written only by reviewed '
    'migrations, never by the application.';

ALTER TABLE mubasher_oms.partops_framework_version OWNER TO mubasher_oms;
REVOKE ALL ON TABLE mubasher_oms.partops_framework_version FROM PUBLIC;

INSERT INTO mubasher_oms.partops_framework_version
    (migration_id, framework_version, description)
VALUES
    ('20261110_03', '20261110_03',
     'Phase 5A (attention, missed-run reconcile, bounded retry SQL, manual target '
     'lock, db_config allowlist) + Phase 5B version record.')
ON CONFLICT (migration_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_ui') THEN
        EXECUTE 'GRANT SELECT ON mubasher_oms.partops_framework_version TO partition_job_ui';
    ELSE
        RAISE NOTICE 'Role partition_job_ui does not exist — grant skipped.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'partition_job_scheduler') THEN
        EXECUTE 'GRANT SELECT ON mubasher_oms.partops_framework_version TO partition_job_scheduler';
    ELSE
        RAISE NOTICE 'Role partition_job_scheduler does not exist — grant skipped.';
    END IF;
END
$$;

COMMIT;

-- Verification (read-only):
--   SELECT max(migration_id) AS database_framework_version
--     FROM mubasher_oms.partops_framework_version;
