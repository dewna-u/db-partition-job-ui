-- DISPOSABLE TEST DATABASE ONLY — run exclusively by
-- scripts/disposable_db/run_disposable_db_test.sh against a throwaway cluster.
-- Never run against production, reference or shared databases.
-- Disposable smoke DB setup: roles + bootstrap ownership + pre-5A grants.
-- NOTE: framework functions cron_to_interval_or_next_run / create_any_table_partition /
-- drop_any_table_partition / insert_data_to_partition_job_table are NOT
-- version-controlled and are intentionally NOT created (no invented bodies).
\set ON_ERROR_STOP 1
CREATE ROLE mubasher_oms NOLOGIN;
CREATE ROLE partition_job_ui LOGIN;
CREATE ROLE partition_job_scheduler LOGIN;
CREATE SCHEMA mubasher_oms AUTHORIZATION mubasher_oms;
