# CAPTURE REQUIRED — `cron_field_to_int_array`

**Status:** authoritative definition NOT in Git. Not reviewed. Not approved.
Related issue: ISSUE-003 (stays OPEN until a reviewed capture replaces this file).

| | |
|---|---|
| Expected identity | `mubasher_oms.cron_field_to_int_array(<signature UNKNOWN>)` |
| Purpose | Reported only in bootstrap notes as a helper of cron_to_interval_or_next_run. Existence, signature and use are UNVERIFIED. |
| If absent / wrong | Unknown until the cron helper is captured; capture all overloads and confirm whether the cron helper actually calls it. |
| Classification | EXISTING / LEGACY DATABASE FRAMEWORK OBJECT |

**Do not** write this function from callers, tests, comments or memory.

To resolve:

1. Run `sql/capture_reference_framework.sql` against the approved reference DB.
2. Copy the section-5 definition verbatim to `cron_field_to_int_array.sql` with the header from
   `README.md`; verify the md5.
3. Complete `../SECURITY_REVIEW_CHECKLIST.md` for it.
4. Update `../framework_manifest.json` (definition_in_git, status, security,
   owner, execute_roles), regenerate `../INVENTORY.md`, delete this file.
5. Run the disposable-DB workflow; integration tests for this object stop
   skipping with "FRAMEWORK DEFINITION NOT AVAILABLE".
