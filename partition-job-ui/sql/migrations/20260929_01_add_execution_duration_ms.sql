-- =============================================================================
-- Migration: add real execution duration to partitioning_job_table_log
-- =============================================================================
-- PREPARED ONLY — do NOT auto-apply to production.
-- Apply manually after review (psql / DBA change window).
--
-- Column:     execution_duration_ms BIGINT NULL
-- Table:      mubasher_oms.partitioning_job_table_log
-- Nullable:   YES (required so historical rows stay valid)
-- Existing:   unchanged; NULL means "Not recorded"
-- Semantics:  how long the attempt took (ms). job_runtime remains WHEN it ran.
--
-- Rollback:
--   ALTER TABLE mubasher_oms.partitioning_job_table_log
--     DROP COLUMN IF EXISTS execution_duration_ms;
-- =============================================================================

BEGIN;

ALTER TABLE mubasher_oms.partitioning_job_table_log
  ADD COLUMN IF NOT EXISTS execution_duration_ms BIGINT;

COMMENT ON COLUMN mubasher_oms.partitioning_job_table_log.execution_duration_ms IS
  'Wall-clock duration of the partition create/drop attempt in milliseconds. '
  'NULL for historical rows recorded before duration capture was deployed. '
  'job_runtime remains the execution timestamp, not the duration.';

COMMIT;
