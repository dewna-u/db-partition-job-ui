# Security review checklist — captured framework definitions

Complete one copy of this checklist **per captured function** (paste it into
the review notes / PR). A function is not approved until every item is
answered and a reviewer signs off. Uncaptured functions are **never** marked
approved.

| Function | Captured file | Reviewer | Date | Result |
|---|---|---|---|---|
| `insert_data_to_partition_job_table` | — | — | — | NOT CAPTURED |
| `cron_to_interval_or_next_run` | — | — | — | NOT CAPTURED |
| `cron_field_to_int_array` | — | — | — | NOT CAPTURED (existence unverified) |
| `create_any_table_partition` | — | — | — | NOT CAPTURED |
| `drop_any_table_partition` | — | — | — | NOT CAPTURED |

## A. Identity and privileges

- [ ] Signature matches `framework_manifest.json` exactly (identity arg types, result type). Overloads listed and explained.
- [ ] `SECURITY INVOKER` vs `SECURITY DEFINER` recorded. If DEFINER: is it necessary? What does the owner role gain the caller?
- [ ] Owner role recorded; owner is not a superuser unless justified; owner matches the manifest expectation.
- [ ] `proconfig`: DEFINER functions have a fixed `search_path` ending in `pg_temp`, without `public`/`$user`. INVOKER functions: note which unqualified names they rely on (PartOps calls workers/cron helper WITHOUT pinning search_path on purpose).
- [ ] EXECUTE grants: PUBLIC revoked? Only `partition_job_scheduler` / `partition_job_ui` as listed in the manifest? Any grant WITH GRANT OPTION?
- [ ] Privileges the caller role needs on target business tables for DDL (INVOKER workers run DDL as `partition_job_scheduler` / `partition_job_ui`). Document the minimum.

## B. SQL construction

- [ ] All dynamic SQL listed (`EXECUTE`, `format()`, string concatenation).
- [ ] Every identifier is quoted with `format('%I')` / `quote_ident()`; every literal with `%L` / `quote_literal()` / `USING`. No raw concatenation of schema/table/partition names.
- [ ] Inputs validated (schema/table exist, are partitioned, are not system schemas; `partition_unit` / interval values bounded).
- [ ] No execution of SQL text stored in tables or passed by callers.

## C. Side effects

- [ ] Exact DDL executed (CREATE TABLE … PARTITION OF, ATTACH/DETACH, DROP TABLE, CASCADE?). For DROP: which partitions qualify; can it drop a non-partition table; is `CASCADE` used; can it drop the parent?
- [ ] `SET` / `SET LOCAL` / `set_config` calls: which settings, `is_local` true? Does it change `DateStyle`, `search_path`, `role`, `session_authorization`? (PartOps renders result timestamps with `to_char` and restricts db_config_para; a worker changing DateStyle must not affect correctness.)
- [ ] Role switching (`SET ROLE`, `SET SESSION AUTHORIZATION`) — must be absent.
- [ ] GRANT / REVOKE / ALTER OWNER executed at runtime — list and justify.
- [ ] Writes to tables other than the target partitions and PartOps tables.

## D. Transactions, errors, concurrency

- [ ] Assumes it runs inside the caller's transaction (no COMMIT/ROLLBACK inside; procedures with transaction control are incompatible with PartOps' `SELECT fn()` call path).
- [ ] Exception handling: are errors swallowed (would mark a failed DDL as SUCCESS)? PartOps treats "no exception" as success.
- [ ] Lock acquisition: lock level on the parent table, `lock_timeout` sensitivity, behaviour under concurrent DML.
- [ ] Own advisory locks? Conflicts with PartOps namespaces 87421031 / 87421032?
- [ ] Idempotency: running the same occurrence twice (manual recovery after a partial run) is safe.

## E. Compatibility with PartOps callers

- [ ] `create_any_table_partition(schema, table, unit, period::integer, interval)` argument meaning matches `partitioning_job_table` columns.
- [ ] `drop_any_table_partition(schema, table, interval)` retention semantics match the UI wording ("Retention").
- [ ] `cron_to_interval_or_next_run(text)` returns `(schedule_interval, next_run_time)` with exactly one non-NULL for valid six-field cron; `next_run_time` strictly in the future relative to `clock_timestamp()`; time zone assumptions documented.
- [ ] Works under the DEFINER `search_path = pg_catalog, mubasher_oms, pg_temp` of `update_data_to_partition_job_table` (which calls the cron helper as owner). **EDB:** if it uses Oracle-compatible built-ins from the `sys` schema unqualified, this will fail — see EDB_COMPATIBILITY.md.
- [ ] `insert_data_to_partition_job_table`: does it validate `db_config_para` (PartOps allowlist), compute/accept `next_run_time`, return a value? Parameter names match the named-notation call in `database.py`.

## F. Sign-off

- [ ] Integration tests for this function pass on a disposable database.
- [ ] Manifest updated (`definition_in_git: yes`, `status: versioned`, security/owner/grants).
- [ ] Related issue updated; closed only after the reviewed definition is committed.

Reviewer: ____________  Date: ________  Result: APPROVED / CHANGES REQUIRED

## Phase 5D (remote execution) review items

- [ ] `partops_execution_attempt`: app roles SELECT only; no INSERT/UPDATE/DELETE.
- [ ] Every `partops_remote_*`, `partops_resolve_remote_outcome`,
      `partops_set_job_target`: SECURITY DEFINER, owner `mubasher_oms`,
      `search_path = pg_catalog, mubasher_oms, pg_temp`, PUBLIC revoked,
      EXECUTE only for the documented role.
- [ ] Internal helpers `partops_compute_next_run`, `partops_safe_error`: no
      app-role EXECUTE (manifest `"internal": true`).
- [ ] No credential, secret id, host or connection string column in the
      ledger, attention or history; error text bounded and redacted.
- [ ] Target-side statements are fixed and parameterised (worker call,
      advisory lock, `set_config(..., true)`); db_config allowlisted twice.
- [ ] Guards: ledger state machine trigger; job configuration guard (55006);
      unknown-outcome attention cannot be re-run or closed before resolution.
- [ ] Scheduler `/internal/scheduler/manual-run` bound to localhost / private
      interface only (same trust boundary as the API; API still unauthenticated).
