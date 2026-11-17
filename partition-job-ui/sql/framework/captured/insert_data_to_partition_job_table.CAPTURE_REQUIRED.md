# CAPTURE REQUIRED — `insert_data_to_partition_job_table`

**Status:** authoritative definition NOT in Git. Not reviewed. Not approved.
Related issue: ISSUE-008 (stays OPEN until a reviewed capture replaces this file).

| | |
|---|---|
| Expected identity | `mubasher_oms.insert_data_to_partition_job_table(character varying, boolean, character varying, character varying, jsonb, character varying, interval, timestamp without time zone, character varying, numeric, boolean, interval)` |
| Purpose | Create path for POST /api/jobs (database.create_partition_job, named notation p_job_name ... p_is_create_drop_interval). |
| If absent / wrong | API job creation fails (42883). Readiness and preflight report FAIL. |
| Classification | EXISTING / LEGACY DATABASE FRAMEWORK OBJECT |

**Do not** write this function from callers, tests, comments or memory.

To resolve:

1. Run `sql/capture_reference_framework.sql` against the approved reference DB.
2. Copy the section-5 definition verbatim to `insert_data_to_partition_job_table.sql` with the header from
   `README.md`; verify the md5.
3. Complete `../SECURITY_REVIEW_CHECKLIST.md` for it.
4. Update `../framework_manifest.json` (definition_in_git, status, security,
   owner, execute_roles), regenerate `../INVENTORY.md`, delete this file.
5. Run the disposable-DB workflow; integration tests for this object stop
   skipping with "FRAMEWORK DEFINITION NOT AVAILABLE".
