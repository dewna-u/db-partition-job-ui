-- ============================================================================
-- Remove the automatic lock_timeout default from db_config_para
-- ============================================================================
--
-- Problem
--   mubasher_oms.partitioning_job_table.db_config_para is meant to hold ONLY the
--   session configuration that belongs to the imported partition function, or
--   that the user typed deliberately. A BEFORE INSERT OR UPDATE trigger was
--   appending {"lock_timeout": "30s"} whenever the key was absent, so an
--   application payload of {} was stored as {"lock_timeout": "30s"}.
--
-- Change
--   Replace the trigger FUNCTION body so it only normalises NULL to '{}'::jsonb.
--   The trigger itself is NOT dropped or recreated, and no existing row is
--   modified. Only future INSERT / UPDATE behaviour changes.
--
-- Scope
--   This file does NOT touch: the trigger definition, the table structure,
--   insert_data_to_partition_job_table(), its signature, any privileges, or the
--   runtime lock_timeout that partition execution functions may set for
--   themselves (see the note at the bottom — that is a separate concern).
--
-- Run manually, inside a transaction, after completing STEP 1.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- STEP 1 — PRE-FLIGHT (read-only). Run this first and keep the output.
-- ----------------------------------------------------------------------------
-- 1a. Capture the CURRENT function definition so you can roll back precisely,
--     and so you can confirm its attributes before replacing it.
--
--     IMPORTANT: CREATE OR REPLACE FUNCTION resets any attribute that the new
--     statement does not restate. If the output below shows SECURITY DEFINER,
--     a non-default volatility, a `SET search_path = ...` clause, or a COST /
--     LEAKPROOF setting, add the same clauses to STEP 2 before running it.
--     The replacement in STEP 2 assumes the project default: SECURITY INVOKER
--     (i.e. no SECURITY DEFINER) and no attached SET clauses.

SELECT pg_get_functiondef(p.oid) AS current_definition,
       p.prosecdef              AS is_security_definer,
       p.provolatile            AS volatility,
       p.proconfig              AS attached_set_clauses,
       pg_get_userbyid(p.proowner) AS owner
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'mubasher_oms'
   AND p.proname = 'fn_set_default_db_config_para';

-- 1b. Confirm which triggers use it, and on which columns. Expected: one
--     BEFORE INSERT OR UPDATE OF db_config_para trigger on
--     mubasher_oms.partitioning_job_table. This migration leaves it untouched.

SELECT t.tgname            AS trigger_name,
       c.relname           AS table_name,
       pg_get_triggerdef(t.oid) AS trigger_definition
  FROM pg_trigger t
  JOIN pg_class c     ON c.oid = t.tgrelid
  JOIN pg_proc p      ON p.oid = t.tgfoid
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE NOT t.tgisinternal
   AND n.nspname = 'mubasher_oms'
   AND p.proname = 'fn_set_default_db_config_para';

-- 1c. Find every OTHER database object that mentions lock_timeout, so nothing
--     else is silently re-injecting it. Review the results; do not change
--     anything found here as part of this migration.

SELECT n.nspname AS schema_name,
       p.proname AS function_name,
       p.prokind AS kind
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'mubasher_oms'
   AND pg_get_functiondef(p.oid) ILIKE '%lock_timeout%'
 ORDER BY p.proname;

-- 1d. Check for a column DEFAULT or CHECK constraint that could also add it.

SELECT column_name, column_default, is_nullable, data_type
  FROM information_schema.columns
 WHERE table_schema = 'mubasher_oms'
   AND table_name   = 'partitioning_job_table'
   AND column_name  = 'db_config_para';

-- 1e. How many existing rows carry the injected default? Read-only census.
--     Existing rows are deliberately NOT modified by this migration: some of
--     them may genuinely need lock_timeout. Report separately if cleanup is
--     wanted, and handle it as its own reviewed change.

SELECT count(*) FILTER (WHERE db_config_para ? 'lock_timeout')        AS rows_with_lock_timeout,
       count(*) FILTER (WHERE db_config_para = '{}'::jsonb)          AS rows_with_empty_config,
       count(*) FILTER (WHERE db_config_para IS NULL)                AS rows_with_null_config,
       count(*)                                                      AS total_rows
  FROM mubasher_oms.partitioning_job_table;


-- ----------------------------------------------------------------------------
-- STEP 2 — THE CHANGE. Review STEP 1 output first, then run this.
-- ----------------------------------------------------------------------------

BEGIN;

-- Only the function body changes. NULL is still normalised to an empty JSON
-- object so downstream `db_config_para ->> 'key'` lookups never hit NULL, but
-- no key is ever invented.
CREATE OR REPLACE FUNCTION mubasher_oms.fn_set_default_db_config_para()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Normalise NULL to an empty object for consistency.
    IF NEW.db_config_para IS NULL THEN
        NEW.db_config_para := '{}'::jsonb;
    END IF;

    -- Deliberately no default injection. db_config_para must contain only the
    -- configuration extracted from the original partition function or supplied
    -- explicitly by the user. A missing key means "not configured", which is a
    -- meaningful state and must be preserved.
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION mubasher_oms.fn_set_default_db_config_para() IS
    'Normalises partitioning_job_table.db_config_para from NULL to an empty '
    'JSON object. Never injects default settings: the stored configuration must '
    'reflect only the original partition function or explicit user input.';

-- ---------------------------------------------------------------------------
-- STEP 3 — VERIFY INSIDE THE SAME TRANSACTION, BEFORE COMMIT.
-- ---------------------------------------------------------------------------
-- These four probes cover the required cases. They INSERT temporary rows and
-- are rolled back below, so nothing is persisted. Adjust the column list if
-- partitioning_job_table has additional NOT NULL columns without defaults.

-- Uncomment to run the probes:
--
-- CREATE TEMP TABLE _db_config_probe AS
-- SELECT 'case'::text AS case_label, '{}'::jsonb AS stored WITH NO DATA;
--
-- WITH probe AS (
--     INSERT INTO mubasher_oms.partitioning_job_table
--         (job_name, is_enabled, table_schema, table_name, db_config_para,
--          job_schedule, frequency, next_run_time, partition_unit,
--          partition_period, is_create, create_drop_interval)
--     VALUES
--         ('_probe_case1', false, 'mubasher_oms', '_probe', '{}'::jsonb,
--          '0 0 2 26 * *', '1 month'::interval, now(), 'month', 1, true,
--          '2 months'::interval),
--         ('_probe_case2', false, 'mubasher_oms', '_probe',
--          '{"datestyle": "ISO, DMY"}'::jsonb,
--          '0 0 2 26 * *', '1 month'::interval, now(), 'month', 1, true,
--          '2 months'::interval),
--         ('_probe_case3', false, 'mubasher_oms', '_probe',
--          '{"lock_timeout": "10s"}'::jsonb,
--          '0 0 2 26 * *', '1 month'::interval, now(), 'month', 1, true,
--          '2 months'::interval),
--         ('_probe_case4', false, 'mubasher_oms', '_probe', NULL,
--          '0 0 2 26 * *', '1 month'::interval, now(), 'month', 1, true,
--          '2 months'::interval)
--     RETURNING job_name, db_config_para
-- )
-- SELECT job_name, db_config_para FROM probe ORDER BY job_name;
--
-- Expected result:
--   _probe_case1  {}
--   _probe_case2  {"datestyle": "ISO, DMY"}
--   _probe_case3  {"lock_timeout": "10s"}
--   _probe_case4  {}
--
-- If case 1 or case 2 comes back containing lock_timeout, another object is
-- still injecting it. Re-check STEP 1c and STEP 1d before committing.

-- Replace with COMMIT once STEP 3 matches the expected result.
ROLLBACK;


-- ----------------------------------------------------------------------------
-- ROLLBACK OF THIS CHANGE
-- ----------------------------------------------------------------------------
-- Prefer the exact definition captured in STEP 1a. The previous behaviour was
-- equivalent to:
--
-- CREATE OR REPLACE FUNCTION mubasher_oms.fn_set_default_db_config_para()
-- RETURNS trigger
-- LANGUAGE plpgsql
-- AS $$
-- BEGIN
--     IF NEW.db_config_para IS NULL THEN
--         NEW.db_config_para := '{}'::jsonb;
--     END IF;
--
--     IF NOT (NEW.db_config_para ? 'lock_timeout') THEN
--         NEW.db_config_para :=
--             NEW.db_config_para || '{"lock_timeout": "30s"}'::jsonb;
--     END IF;
--
--     RETURN NEW;
-- END;
-- $$;


-- ----------------------------------------------------------------------------
-- SEPARATE CONCERN — RUNTIME lock_timeout (NOT changed here)
-- ----------------------------------------------------------------------------
-- STORED CONFIGURATION and RUNTIME SAFETY are two different things:
--
--   * partitioning_job_table.db_config_para  — what this migration fixes.
--   * set_config('lock_timeout', '30s', true) executed inside an execution
--     function such as mubasher_oms.create_any_table_partition() — a guard that
--     stops an ALTER TABLE from waiting indefinitely for a lock.
--
-- If STEP 1c shows create_any_table_partition() or drop_any_table_partition()
-- setting lock_timeout internally, that is INTENTIONAL runtime protection and
-- is deliberately left alone. Removing it would let partition DDL block on a
-- lock indefinitely. It does not affect what is stored in db_config_para.
