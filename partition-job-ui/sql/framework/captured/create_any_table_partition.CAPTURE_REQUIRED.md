# CAPTURE REQUIRED — `create_any_table_partition`

**Status:** authoritative definition NOT in Git. Not reviewed. Not approved.
Related issue: ISSUE-008 (stays OPEN until a reviewed capture replaces this file).

| | |
|---|---|
| Expected identity | `mubasher_oms.create_any_table_partition(character varying, character varying, character varying, integer, interval) RETURNS void` |
| Purpose | CREATE worker called by run_partition_job_scheduled and run_partition_job_manual (and therefore missed-run recovery). |
| If absent / wrong | Every CREATE execution fails (logged FAIL / MANUAL_FAIL; schedule still advances). Scheduler gate FAILs. |
| Classification | EXISTING / LEGACY DATABASE FRAMEWORK OBJECT |

**Do not** write this function from callers, tests, comments or memory.

To resolve:

1. Run `sql/capture_reference_framework.sql` against the approved reference DB.
2. Copy the section-5 definition verbatim to `create_any_table_partition.sql` with the header from
   `README.md`; verify the md5.
3. Complete `../SECURITY_REVIEW_CHECKLIST.md` for it.
4. Update `../framework_manifest.json` (definition_in_git, status, security,
   owner, execute_roles), regenerate `../INVENTORY.md`, delete this file.
5. Run the disposable-DB workflow; integration tests for this object stop
   skipping with "FRAMEWORK DEFINITION NOT AVAILABLE".
