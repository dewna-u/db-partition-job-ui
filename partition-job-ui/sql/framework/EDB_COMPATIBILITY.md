# EDB compatibility — validation checklist

**Status: PostgreSQL 16 tested (disposable cluster, partial framework). EDB NOT yet tested.**

Nothing in this repository has been executed on EDB Postgres Advanced Server.
Do not claim EDB compatibility until the items below are verified on a
disposable EDB instance of the production version (with the captured
framework functions).

| # | Area | PartOps usage | PostgreSQL 16 | EDB risk / what to verify |
|---|---|---|---|---|
| 1 | Identity columns | `partition_job_attention.attention_id bigint GENERATED ALWAYS AS IDENTITY` | Tested | Requires server ≥ 10. Confirm production EDB major version. |
| 2 | `ON CONFLICT ON CONSTRAINT` | reconcile attention insert; version insert | Tested | ≥ 9.5. Verify. |
| 3 | PL/pgSQL syntax | all PartOps functions are `LANGUAGE plpgsql` (not SPL) | Tested | EDB supports plpgsql; verify no SPL-only reserved word conflicts (`status`, `message` OUT params). |
| 4 | Trigger syntax | `EXECUTE PROCEDURE` (not `EXECUTE FUNCTION`) | Tested | Portable; verify. |
| 5 | System catalogs | `pg_proc.prokind` (PG ≥ 11), `oidvectortypes`, `aclexplode`, `acldefault`, `pg_get_functiondef`, `to_regprocedure` | Tested | `prokind` needs ≥ 11 (preflight/capture). EDB adds `sys` catalog views; the `pg_*` ones must match. |
| 6 | DateStyle | allowlist accepts ISO only; scheduler renders result timestamps with `to_char` | Tested (ISO, DMY / ISO, MDY) | Production default may be `Redwood, SHOW_TIME`: `get_upcoming_partition_jobs` returns `timestamp` and psycopg decodes it in text format — **verify the role/database DateStyle is ISO** or the scheduler refresh fails (fails safe: refresh error + backoff). |
| 7 | `edb_redwood_date` | none in PartOps; captured functions may depend on it | n/a | Verify cron helper / workers under the production setting. |
| 8 | Interval parsing | `'1 day'`, `'3 months'`, `interval '6 hours'`, `p_frequency::interval` | Tested | Verify `IntervalStyle` and Oracle-style interval input are not enabled for the roles. |
| 9 | Advisory locks | `pg_try_advisory_xact_lock(int, int)`, `pg_advisory_lock` in tests | Tested | Verify identical behaviour and that EDB `DBMS_LOCK` is not used by captured workers on the same keys. |
| 10 | `hashtext()` | lock keys | Tested | Internal function; verify present and stable (key equality between scheduled and manual paths is what matters). |
| 11 | `current_database()` / identity check | scheduler + preflight | Tested | Verify. |
| 12 | Security attributes | DEFINER with `SET search_path = pg_catalog, mubasher_oms, pg_temp` | Tested | **Risk:** EDB Oracle-compatible built-ins (`NVL`, `DECODE`, `SYSDATE`, `TO_DATE` variants …) live in schema `sys`. If the captured `cron_to_interval_or_next_run` uses them unqualified, it fails when called from `update_data_to_partition_job_table` (pinned path without `sys`). Verify after capture; a fix (adding `sys` to the pinned path) needs its own reviewed change. |
| 13 | Empty string vs NULL | `NULLIF(btrim(x), '')`, `COALESCE` in lock keys, `\|\|` concatenation | Tested | Verify `edb_redwood_strings` (NULL concatenation semantics) does not change lock keys or messages. |
| 14 | `varchar` mapping | columns `character varying`; worker signatures `varchar` | Tested | EDB may report `varchar2` in catalogs for objects created in Redwood mode — preflight signature comparison would then FAIL; verify `oidvectortypes` output on EDB. |
| 15 | pgAgent | read-only listing, legacy-runner detection | Not installed in test | Verify pgAgent schema/version on EDB (pgAgent vs EDB job scheduler `DBMS_SCHEDULER`); legacy runners may be scheduled elsewhere. |
| 16 | DDL behaviour | executed only by captured workers | Not tested (workers not captured) | Partition DDL locks, `DETACH PARTITION`, `CASCADE`, global indexes on EDB partitioned tables. |
| 17 | `to_char(ts, 'YYYY-MM-DD HH24:MI:SS.US')` | result rendering | Tested | Verify format codes behave identically under Redwood settings. |
| 18 | Regex `~*` with `[[:space:]]` | allowlist | Tested | Verify. |

## How to validate (later)

1. Disposable EDB instance, same major version as production, production-like
   GUCs (`DateStyle`, `edb_redwood_*`).
2. Run `scripts/disposable_db/run_disposable_db_test.sh` with `PG_BIN` pointing at
   the EDB binaries (and captured definitions present).
3. Run `scripts/preflight_framework.py --scope all` against it.
4. Record results in this file (change "Tested" columns) — never by assumption.

## Phase 5D items to verify on EDB

* `partops_remote_claim` sets `IntervalStyle = 'iso_8601'` as a function
  attribute so the interval snapshot ("P1M") parses identically on the target
  connection; verify EDB accepts the function-level SET and ISO-8601 input.
* `#variable_conflict use_column` in PL/pgSQL functions (standard PL/pgSQL
  directive) and `INSERT ... AS alias ... RETURNING alias.col`.
* Custom transaction-local setting `partops.requested_target_id`
  (`current_setting(name, true)`), used by the new-job target hook.
* Autonomous transactions: if any captured legacy worker uses EDB
  `PRAGMA AUTONOMOUS_TRANSACTION`, the "proven rollback" classification does
  not hold for it (ISSUE-013).
