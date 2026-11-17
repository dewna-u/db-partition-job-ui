-- ============================================================================
-- capture_reference_framework.sql                      *** READ-ONLY ***
-- ============================================================================
-- READ-ONLY CAPTURE OF THE PARTOPS DATABASE FRAMEWORK FROM AN APPROVED
-- REFERENCE DATABASE. IT NEVER CREATES, ALTERS, DROPS, INSERTS, UPDATES,
-- DELETES, TRUNCATES, GRANTS, REVOKES OR CALLS ANY FRAMEWORK/WRITE FUNCTION.
--
-- Everything runs inside ONE transaction opened with
--     BEGIN TRANSACTION READ ONLY;
-- so the server itself rejects any write, and the script ends with ROLLBACK.
--
-- Run ONLY against a database a DBA has approved as the authoritative
-- reference (see sql/framework/README.md, "Capture workflow"). Do not run it
-- against production unless your change process explicitly allows read-only
-- catalog queries there.
--
-- Usage (psql 12+; output is plain text for review, then split into files):
--   psql "host=<ref-host> dbname=<ref-db> user=<read-only-dba>" -X \
--        -v ON_ERROR_STOP=1 -P pager=off \
--        -f sql/capture_reference_framework.sql \
--        -o capture_<ref-db>_<YYYYMMDD>.txt
--
-- Object list = sql/framework/framework_manifest.json (keep in sync; unit-
-- tested). Catalog functions used: pg_get_functiondef, pg_get_triggerdef,
-- pg_get_viewdef, pg_get_constraintdef, pg_get_indexdef, format_type,
-- aclexplode, has_*_privilege. None of them modify data.
-- ============================================================================

\set ON_ERROR_STOP 1
BEGIN TRANSACTION READ ONLY;

\echo '=== 0. Capture context'
SELECT current_database()                    AS capture_database,
       current_user                          AS capture_user,
       session_user                          AS capture_session_user,
       inet_server_addr()::text              AS server_address,
       inet_server_port()                    AS server_port,
       current_setting('server_version')     AS server_version,
       version()                             AS version_string,
       current_setting('transaction_read_only') AS transaction_read_only,
       clock_timestamp()                     AS captured_at;

\echo '=== 1. Schema mubasher_oms: owner and privileges'
SELECT n.nspname                   AS schema_name,
       pg_get_userbyid(n.nspowner) AS owner,
       n.nspacl::text              AS acl
  FROM pg_namespace n
 WHERE n.nspname IN ('mubasher_oms', 'pgagent');

\echo '=== 2. Roles PartOps expects'
SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
       ARRAY(SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid
              WHERE m.member = r.oid ORDER BY 1) AS member_of
  FROM pg_roles r
 WHERE r.rolname IN ('mubasher_oms', 'partition_job_ui', 'partition_job_scheduler')
 ORDER BY r.rolname;

\echo '=== 3. Framework function metadata (all overloads of every manifest name + similar names)'
SELECT n.nspname                                   AS schema_name,
       p.proname                                   AS function_name,
       oidvectortypes(p.proargtypes)               AS identity_arg_types,
       pg_get_function_identity_arguments(p.oid)   AS identity_arguments,
       pg_get_function_result(p.oid)               AS result_type,
       l.lanname                                   AS language,
       p.prokind                                   AS kind,
       p.provolatile                               AS volatility,
       p.prosecdef                                 AS security_definer,
       pg_get_userbyid(p.proowner)                 AS owner,
       p.proconfig                                 AS set_clauses,
       p.proacl::text                              AS acl,
       (p.proacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(p.proacl) a
                                     WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'))
                                                   AS public_can_execute,
       md5(pg_get_functiondef(p.oid))              AS definition_md5
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  JOIN pg_language l  ON l.oid = p.prolang
 WHERE n.nspname = 'mubasher_oms'
   AND (p.proname IN (
          'insert_data_to_partition_job_table',
          'cron_to_interval_or_next_run',
          'cron_field_to_int_array',
          'create_any_table_partition',
          'drop_any_table_partition',
          'run_partition_create_jobs',
          'run_partition_drop_jobs',
          'run_partition_job_manual',
          'update_data_to_partition_job_table',
          'get_upcoming_partition_jobs',
          'run_partition_job_scheduled',
          'reconcile_missed_partition_job',
          'run_partition_job_attention_recovery',
          'acknowledge_partition_job_attention',
          'partition_job_max_execution_lateness',
          'partition_job_config_param_allowed',
          'fn_partition_job_attention_guard',
          'fn_set_default_db_config_para',
          'partops_control_target_id',
          'fn_partops_execution_attempt_guard',
          'fn_partops_job_execution_guard',
          'fn_partops_job_target_on_insert',
          'partops_job_open_attempt_state',
          'partops_compute_next_run',
          'partops_safe_error',
          'partops_get_upcoming_jobs',
          'partops_remote_close_attempt',
          'partops_remote_claim',
          'partops_remote_mark_dispatching',
          'partops_remote_record_success',
          'partops_remote_record_failure',
          'partops_remote_mark_unknown',
          'partops_remote_finalize',
          'partops_remote_recover_attempts',
          'partops_resolve_remote_outcome',
          'partops_set_job_target')
        OR p.proname ILIKE '%partition%'
        OR p.proname ILIKE '%cron%')
 ORDER BY p.proname, p.oid;

\echo '=== 4. Explicit EXECUTE grants per framework function'
SELECT p.proname                                   AS function_name,
       oidvectortypes(p.proargtypes)               AS identity_arg_types,
       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee,
       pg_get_userbyid(a.grantor)                  AS grantor,
       a.privilege_type,
       a.is_grantable
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
  CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
 WHERE n.nspname = 'mubasher_oms'
   AND (p.proname ILIKE '%partition%' OR p.proname ILIKE '%cron%')
 ORDER BY 1, 2, 3;

\echo '=== 5. AUTHORITATIVE DEFINITIONS — one record per function (copy each into sql/framework/captured/)'
\echo '===    (functions only; procedures/aggregates are listed in section 3 if present)'
\x on
SELECT n.nspname || '.' || p.proname || '(' || oidvectortypes(p.proargtypes) || ')' AS object_identity,
       md5(pg_get_functiondef(p.oid))                                              AS definition_md5,
       pg_get_functiondef(p.oid)                                                   AS definition
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'mubasher_oms'
   AND p.prokind IN ('f', 'p')
   AND (p.proname IN (
          'insert_data_to_partition_job_table',
          'cron_to_interval_or_next_run',
          'cron_field_to_int_array',
          'create_any_table_partition',
          'drop_any_table_partition',
          'run_partition_create_jobs',
          'run_partition_drop_jobs',
          'fn_set_default_db_config_para')
        OR p.proname ILIKE 'cron%')
 ORDER BY p.proname, p.oid;
\x off

\echo '=== 6. Tables / sequences: owner, privileges, columns'
SELECT n.nspname AS schema_name, c.relname AS relation, c.relkind,
       pg_get_userbyid(c.relowner) AS owner, c.relacl::text AS acl
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mubasher_oms'
   AND (c.relname IN ('partitioning_job_table', 'partitioning_job_table_log',
                      'seq_partitioning_job_id', 'seq_partitioning_job_log_id',
                      'partition_job_attention', 'partops_framework_version',
                      'partops_database_target', 'partops_execution_attempt')
        OR c.relname ILIKE '%partitioning_job%')
 ORDER BY 2;

SELECT c.relname AS table_name, a.attnum, a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type, a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS column_default
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE n.nspname = 'mubasher_oms'
   AND c.relname IN ('partitioning_job_table', 'partitioning_job_table_log')
   AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY c.relname, a.attnum;

\echo '=== 7. Constraints and indexes'
SELECT c.relname AS table_name, con.conname, con.contype,
       pg_get_constraintdef(con.oid) AS definition
  FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mubasher_oms'
   AND c.relname IN ('partitioning_job_table', 'partitioning_job_table_log')
 ORDER BY 1, 2;

SELECT c.relname AS table_name, i.relname AS index_name, pg_get_indexdef(x.indexrelid) AS definition
  FROM pg_index x
  JOIN pg_class c ON c.oid = x.indrelid
  JOIN pg_class i ON i.oid = x.indexrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mubasher_oms'
   AND c.relname IN ('partitioning_job_table', 'partitioning_job_table_log')
 ORDER BY 1, 2;

\echo '=== 8. Triggers on framework tables'
SELECT c.relname AS table_name, t.tgname AS trigger_name, t.tgenabled,
       pg_get_triggerdef(t.oid, true) AS definition
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE NOT t.tgisinternal
   AND n.nspname = 'mubasher_oms'
   AND c.relname IN ('partitioning_job_table', 'partitioning_job_table_log', 'partition_job_attention')
 ORDER BY 1, 2;

\echo '=== 9. Composite type cron_schedule_result'
SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod) AS data_type,
       pg_get_userbyid(t.typowner) AS owner
  FROM pg_type t
  JOIN pg_namespace n ON n.oid = t.typnamespace
  JOIN pg_attribute a ON a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped
 WHERE n.nspname = 'mubasher_oms' AND t.typname = 'cron_schedule_result'
 ORDER BY a.attnum;

\echo '=== 10. Views in mubasher_oms that mention partition jobs (definitions)'
SELECT c.relname AS view_name, pg_get_viewdef(c.oid, true) AS definition
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'mubasher_oms' AND c.relkind IN ('v', 'm')
   AND (c.relname ILIKE '%partition%' OR pg_get_viewdef(c.oid, true) ILIKE '%partitioning_job%')
 ORDER BY 1;

\echo '=== 11. Function dependencies recorded by the catalog (types/tables; PL/pgSQL calls are NOT tracked)'
SELECT p.proname AS function_name, d.deptype,
       COALESCE(dc.relname, dt.typname, dp.proname) AS depends_on
  FROM pg_depend d
  JOIN pg_proc p ON p.oid = d.objid AND d.classid = 'pg_proc'::regclass
  JOIN pg_namespace n ON n.oid = p.pronamespace
  LEFT JOIN pg_class dc ON d.refclassid = 'pg_class'::regclass AND dc.oid = d.refobjid
  LEFT JOIN pg_type  dt ON d.refclassid = 'pg_type'::regclass  AND dt.oid = d.refobjid
  LEFT JOIN pg_proc  dp ON d.refclassid = 'pg_proc'::regclass  AND dp.oid = d.refobjid
 WHERE n.nspname = 'mubasher_oms'
   AND (p.proname ILIKE '%partition%' OR p.proname ILIKE '%cron%')
 ORDER BY 1, 3;

\echo '=== 12. Extensions'
SELECT extname, extversion, pg_get_userbyid(extowner) AS owner
  FROM pg_extension ORDER BY 1;

\echo '=== 13. pgAgent steps calling framework routines (read-only; only if pgagent exists)'
SELECT to_regclass('pgagent.pga_jobstep') IS NOT NULL AS has_pgagent \gset
\if :has_pgagent
SELECT j.jobid, j.jobname, j.jobenabled, s.jstid, s.jstname, s.jstenabled, s.jstdbname,
       s.jstcode
  FROM pgagent.pga_job j JOIN pgagent.pga_jobstep s ON s.jstjobid = j.jobid
 WHERE s.jstcode ILIKE '%run_partition_%' OR s.jstcode ILIKE '%_any_table_partition%'
 ORDER BY 1, 4;
\else
\echo 'pgAgent not installed in this database.'
\endif

\echo '=== 14. PartOps framework version (if this reference already has Phase 5B)'
SELECT to_regclass('mubasher_oms.partops_framework_version') IS NOT NULL AS has_version \gset
\if :has_version
SELECT migration_id, framework_version, applied_at, applied_by
  FROM mubasher_oms.partops_framework_version ORDER BY migration_id;
\else
\echo 'partops_framework_version not present (expected on a pre-5B reference).'
\endif

\echo '=== 15. Session settings that influence function behaviour'
SELECT name, setting FROM pg_settings
 WHERE name IN ('DateStyle', 'IntervalStyle', 'TimeZone', 'search_path', 'lc_time',
                'edb_redwood_date', 'edb_redwood_strings', 'standard_conforming_strings')
 ORDER BY 1;

ROLLBACK;
\echo '=== Capture complete (transaction rolled back; nothing was modified).'
