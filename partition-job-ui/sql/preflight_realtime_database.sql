-- ============================================================================
-- preflight_realtime_database.sql
-- ============================================================================
-- READ-ONLY. Safe to run against the NEW production-copy database.
--
-- Purpose
--   Confirm identity of the connected database and whether partition-job
--   framework objects are absent, present, or conflicting — BEFORE any DDL.
--
-- Rules
--   * No CREATE / ALTER / DROP / TRUNCATE / DELETE / UPDATE / INSERT
--   * No GRANT / REVOKE
--   * Do not modify application data
--
-- Prerequisites
--   Connect with a role that can read catalogs (SELECT on pg_catalog is enough
--   for most checks). DDL-capability probe is informational only.
-- ============================================================================


-- 1–5. Connection identity and server version
SELECT
    current_database()                         AS connected_database,
    current_user                               AS connected_user,
    session_user                               AS session_user,
    inet_server_addr()::text                   AS server_address,
    inet_server_port()                         AS server_port,
    version()                                  AS server_version,
    current_setting('server_version')          AS server_version_num_text,
    current_setting('is_superuser')            AS is_superuser;


-- 6–7. Schema mubasher_oms
SELECT
    n.nspname                                  AS schema_name,
    pg_get_userbyid(n.nspowner)                AS schema_owner,
    TRUE                                       AS schema_exists
  FROM pg_namespace n
 WHERE n.nspname = 'mubasher_oms';

SELECT
    CASE
        WHEN to_regnamespace('mubasher_oms') IS NULL
            THEN 'MISSING — schema mubasher_oms does not exist'
        ELSE 'PRESENT — schema mubasher_oms exists'
    END AS mubasher_oms_schema_status;


-- 8–10. Intended framework tables / sequences / type
SELECT
    obj.object_kind,
    obj.object_name,
    CASE
        WHEN obj.object_kind = 'table'
            THEN to_regclass(format('mubasher_oms.%I', obj.object_name)) IS NOT NULL
        WHEN obj.object_kind = 'sequence'
            THEN to_regclass(format('mubasher_oms.%I', obj.object_name)) IS NOT NULL
        WHEN obj.object_kind = 'type'
            THEN to_regtype(format('mubasher_oms.%I', obj.object_name)) IS NOT NULL
        ELSE FALSE
    END AS exists_in_mubasher_oms
  FROM (
        VALUES
            ('table',    'partitioning_job_table'),
            ('table',    'partitioning_job_table_log'),
            ('sequence', 'seq_partitioning_job_id'),
            ('sequence', 'seq_partitioning_job_log_id'),
            ('type',     'cron_schedule_result')
       ) AS obj(object_kind, object_name)
 ORDER BY 1, 2;


-- Column shape check if the config table already exists (read-only).
SELECT
    c.column_name,
    c.data_type,
    c.udt_name,
    c.is_nullable,
    c.column_default
  FROM information_schema.columns c
 WHERE c.table_schema = 'mubasher_oms'
   AND c.table_name = 'partitioning_job_table'
 ORDER BY c.ordinal_position;

SELECT
    c.column_name,
    c.data_type,
    c.udt_name,
    c.is_nullable,
    c.column_default
  FROM information_schema.columns c
 WHERE c.table_schema = 'mubasher_oms'
   AND c.table_name = 'partitioning_job_table_log'
 ORDER BY c.ordinal_position;


-- 11–13. Expected framework functions and similar-name conflicts
WITH expected(proname) AS (
    VALUES
        ('cron_field_to_int_array'),
        ('cron_to_interval_or_next_run'),
        ('create_any_table_partition'),
        ('drop_any_table_partition'),
        ('insert_data_to_partition_job_table'),
        ('fn_set_default_db_config_para'),
        ('run_partition_job_manual'),
        ('run_partition_create_jobs'),
        ('run_partition_drop_jobs'),
        ('get_upcoming_partition_jobs'),
        ('run_partition_job_scheduled')
)
SELECT
    e.proname                                  AS expected_function,
    COALESCE(
        string_agg(
            format(
                '%s(%s) owner=%s definer=%s',
                n.nspname || '.' || p.proname,
                pg_get_function_identity_arguments(p.oid),
                pg_get_userbyid(p.proowner),
                p.prosecdef
            ),
            ' | '
            ORDER BY n.nspname, p.oid
        ),
        'ABSENT'
    ) AS matches_in_database
  FROM expected e
  LEFT JOIN pg_proc p
         ON p.proname = e.proname
  LEFT JOIN pg_namespace n
         ON n.oid = p.pronamespace
 GROUP BY e.proname
 ORDER BY e.proname;

-- Similar-name conflicts (partition / cron / scheduler wording) anywhere.
SELECT
    n.nspname                                  AS schema_name,
    p.proname                                  AS function_name,
    pg_get_function_identity_arguments(p.oid)  AS identity_args,
    pg_get_userbyid(p.proowner)                AS owner,
    p.prosecdef                                AS is_security_definer
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE p.proname ILIKE '%partition%job%'
    OR p.proname ILIKE '%cron_to_interval%'
    OR p.proname ILIKE '%create_any_table_partition%'
    OR p.proname ILIKE '%drop_any_table_partition%'
    OR p.proname ILIKE '%upcoming_partition%'
    OR p.proname ILIKE '%partition_job_scheduled%'
 ORDER BY 1, 2, 3;

-- Relation-level similar names
SELECT
    n.nspname                                  AS schema_name,
    c.relname                                  AS relation_name,
    c.relkind                                  AS kind,
    pg_get_userbyid(c.relowner)                AS owner
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relname ILIKE '%partitioning_job%'
    OR c.relname ILIKE '%seq_partitioning_job%'
    OR c.relname ILIKE 'cron_schedule_result'
 ORDER BY 1, 2;


-- 14. Target runtime roles
SELECT
    r.rolname,
    r.rolcanlogin                              AS can_login,
    r.rolsuper                                 AS is_superuser,
    r.rolcreatedb                              AS can_createdb,
    r.rolcreaterole                            AS can_createrole
  FROM pg_roles r
 WHERE r.rolname IN (
           'partition_job_ui',
           'partition_job_scheduler',
           current_user
       )
 ORDER BY r.rolname;


-- 15. Informational DDL capability of the current user (no DDL performed)
SELECT
    current_user                               AS checked_user,
    current_setting('is_superuser') = 'on'     AS is_superuser,
    has_database_privilege(current_database(), 'CREATE')
                                               AS can_create_in_database,
    CASE
        WHEN to_regnamespace('mubasher_oms') IS NULL THEN NULL
        ELSE has_schema_privilege('mubasher_oms', 'CREATE')
    END                                        AS can_create_in_mubasher_oms,
    CASE
        WHEN to_regnamespace('mubasher_oms') IS NULL THEN NULL
        ELSE has_schema_privilege('mubasher_oms', 'USAGE')
    END                                        AS can_usage_mubasher_oms;


-- Guidance summary (read-only text)
SELECT
    'If ANY intended framework object is PRESENT with an unexpected definition, '
    'STOP and compare with pg_get_functiondef / information_schema before applying '
    'bootstrap or realtime migrations. Do not overwrite unknown function bodies '
    'until definitions are compared.'
        AS deployment_warning;

SELECT
    'Expected for a fresh NEW DB (prod-copy without this framework): all intended '
    'partition-job tables/sequences/functions ABSENT; schema mubasher_oms may already '
    'exist with unrelated application objects — do NOT drop it.'
        AS expected_fresh_state;
