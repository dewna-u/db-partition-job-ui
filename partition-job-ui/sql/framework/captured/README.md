# Captured authoritative definitions

This directory holds **verbatim** definitions captured from the approved
reference database with `sql/capture_reference_framework.sql`.

**Never** add a hand-written, reconstructed or "equivalent" body here. If the
real definition is not available, the `<name>.CAPTURE_REQUIRED.md` placeholder
stays and the related issue stays OPEN.

## File naming

`<function_name>.sql` — one function (all overloads PartOps uses) per file.
Applied in this order by the disposable-DB workflow and the installation
procedure: `cron_field_to_int_array.sql` (if captured), then
`cron_to_interval_or_next_run.sql`, `create_any_table_partition.sql`,
`drop_any_table_partition.sql`, `insert_data_to_partition_job_table.sql`.

## Mandatory header (copy into each captured file)

```sql
-- CAPTURED AUTHORITATIVE DEFINITION — DO NOT EDIT THE BODY
-- object:            mubasher_oms.<name>(<identity arg types>)
-- source database:   <reference db name / host>  (approved by: <name>, <date>)
-- captured at:       <timestamp from section 0 of the capture output>
-- definition md5:    <md5 from section 5>  (must match md5 of the body below)
-- owner / security:  <owner> / SECURITY <INVOKER|DEFINER> / proconfig=<...>
-- execute grants:    <from section 4>
-- security review:   sql/framework/SECURITY_REVIEW_CHECKLIST.md — <PENDING|APPROVED by ..., date>
```

Then the exact `pg_get_functiondef` output, followed by any ALTER OWNER /
REVOKE / GRANT statements that reproduce the reviewed reference privileges.

A captured file is not "approved" until its security review line says
APPROVED. Placeholders and capture scripts never close an issue.
