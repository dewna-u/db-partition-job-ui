# CAPTURE REQUIRED — `drop_any_table_partition`

**Status:** authoritative definition NOT in Git. Not reviewed. Not approved.
Related issue: ISSUE-008 (stays OPEN until a reviewed capture replaces this file).

| | |
|---|---|
| Expected identity | `mubasher_oms.drop_any_table_partition(character varying, character varying, interval) RETURNS void` |
| Purpose | DROP worker (destructive) called by run_partition_job_scheduled and run_partition_job_manual. |
| If absent / wrong | Every DROP execution fails. Scheduler gate FAILs. Its behaviour (which partitions are dropped, CASCADE, detach) is unknown until captured and reviewed. |
| Classification | EXISTING / LEGACY DATABASE FRAMEWORK OBJECT |

**Do not** write this function from callers, tests, comments or memory.

To resolve:

1. Run `sql/capture_reference_framework.sql` against the approved reference DB.
2. Copy the section-5 definition verbatim to `drop_any_table_partition.sql` with the header from
   `README.md`; verify the md5.
3. Complete `../SECURITY_REVIEW_CHECKLIST.md` for it.
4. Update `../framework_manifest.json` (definition_in_git, status, security,
   owner, execute_roles), regenerate `../INVENTORY.md`, delete this file.
5. Run the disposable-DB workflow; integration tests for this object stop
   skipping with "FRAMEWORK DEFINITION NOT AVAILABLE".
