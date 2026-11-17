# CAPTURE REQUIRED — `cron_to_interval_or_next_run`

**Status:** authoritative definition NOT in Git. Not reviewed. Not approved.
Related issue: ISSUE-003 (stays OPEN until a reviewed capture replaces this file).

| | |
|---|---|
| Expected identity | `mubasher_oms.cron_to_interval_or_next_run(text) RETURNS mubasher_oms.cron_schedule_result` |
| Purpose | Authoritative schedule calculation for run_partition_job_scheduled, reconcile_missed_partition_job and update_data_to_partition_job_table. |
| If absent / wrong | Scheduled runs fail closed (FAILED_INVALID_SCHEDULE), missed runs fail closed (MISSED_INVALID_SCHEDULE), edits that need recalculation are rejected. Scheduler gate FAILs. |
| Classification | EXISTING / LEGACY DATABASE FRAMEWORK OBJECT |

**Do not** write this function from callers, tests, comments or memory.

To resolve:

1. Run `sql/capture_reference_framework.sql` against the approved reference DB.
2. Copy the section-5 definition verbatim to `cron_to_interval_or_next_run.sql` with the header from
   `README.md`; verify the md5.
3. Complete `../SECURITY_REVIEW_CHECKLIST.md` for it.
4. Update `../framework_manifest.json` (definition_in_git, status, security,
   owner, execute_roles), regenerate `../INVENTORY.md`, delete this file.
5. Run the disposable-DB workflow; integration tests for this object stop
   skipping with "FRAMEWORK DEFINITION NOT AVAILABLE".
