-- DISPOSABLE TEST DATABASE ONLY — run exclusively by
-- scripts/disposable_db/run_disposable_db_test.sh against a throwaway cluster.
-- Never run against production, reference or shared databases.
\set ON_ERROR_STOP 1
-- Production-like ownership and the pre-5A app-role grants
-- (v1 grant block + manual-run needs), since v1 itself cannot be applied
-- without the unversioned framework functions.
ALTER TABLE mubasher_oms.partitioning_job_table OWNER TO mubasher_oms;
ALTER TABLE mubasher_oms.partitioning_job_table_log OWNER TO mubasher_oms;
ALTER SEQUENCE mubasher_oms.seq_partitioning_job_id OWNER TO mubasher_oms;
ALTER SEQUENCE mubasher_oms.seq_partitioning_job_log_id OWNER TO mubasher_oms;
ALTER TYPE mubasher_oms.cron_schedule_result OWNER TO mubasher_oms;
GRANT USAGE ON SCHEMA mubasher_oms TO partition_job_ui, partition_job_scheduler;
GRANT SELECT ON mubasher_oms.partitioning_job_table TO partition_job_ui, partition_job_scheduler;
GRANT UPDATE (last_run_time, last_run_status, next_run_time)
    ON mubasher_oms.partitioning_job_table TO partition_job_scheduler;
GRANT SELECT, INSERT ON mubasher_oms.partitioning_job_table_log TO partition_job_ui;
GRANT INSERT ON mubasher_oms.partitioning_job_table_log TO partition_job_scheduler;
GRANT USAGE ON SEQUENCE mubasher_oms.seq_partitioning_job_log_id
    TO partition_job_ui, partition_job_scheduler;
