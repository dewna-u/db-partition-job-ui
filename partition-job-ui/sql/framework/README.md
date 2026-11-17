# PartOps database framework

The **framework** is every PostgreSQL/EDB object PartOps needs in the target
database. It falls into two groups:

| Group | Examples | Where its definition lives |
|---|---|---|
| **PartOps-owned** | `run_partition_job_scheduled`, `get_upcoming_partition_jobs`, `reconcile_missed_partition_job`, `run_partition_job_manual`, `update_data_to_partition_job_table`, attention table + functions, `partops_framework_version` | This repository (`sql/realtime_scheduler_v1.sql`, `sql/migrations/`). Reviewed, tested, versioned. |
| **Existing / legacy framework** | `cron_to_interval_or_next_run`, `create_any_table_partition`, `drop_any_table_partition`, `insert_data_to_partition_job_table`, (`cron_field_to_int_array`), job/log tables, `cron_schedule_result` | The approved **reference database**. Tables/type are reconstructed by `sql/bootstrap_partition_job_framework.sql`; the **functions are NOT in Git** and must be captured — never re-written from memory or callers. |

Machine-readable source of truth: [`framework_manifest.json`](framework_manifest.json).
Human-readable inventory (generated): [`INVENTORY.md`](INVENTORY.md).

```text
sql/framework/
├── README.md                      this file
├── framework_manifest.json        required objects, signatures, owner/security/grant expectations
├── INVENTORY.md                   GENERATED from the manifest (scripts/framework_inventory.py)
├── SECURITY_REVIEW_CHECKLIST.md   mandatory review of every captured definition
├── EDB_COMPATIBILITY.md           PostgreSQL 16 tested / EDB NOT yet tested — checklist
├── INTEGRATION_TEST_PLAN.md       end-to-end cases runnable once definitions are captured
└── captured/
    ├── README.md                  file naming + approval rules
    └── <name>.CAPTURE_REQUIRED.md placeholder per missing authoritative definition
                                   (replaced by <name>.sql only after capture + review)
```

## Framework version

`mubasher_oms.partops_framework_version` (migration `20261110_03`) holds one row
per applied PartOps framework migration. Compatibility rule:

```text
application expects  = framework_manifest.json "framework_version"   (20261110_03)
database reports     = SELECT max(migration_id) FROM mubasher_oms.partops_framework_version
equal  → PASS        database older / table missing → FAIL ("outdated/unknown")
database newer → WARNING
```

* Written **only** by reviewed migrations (the migration verifies its objects
  exist before recording). PartOps never writes it at runtime and never
  "upgrades" a database because versions differ.
* The version covers PartOps-owned objects only. Legacy functions are checked
  separately by preflight (presence, signature, grants) and by capture review.

## Control database vs managed targets (Phase 5C)

Architecture: ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.

* **Control database** (`is_control_database = TRUE`, target `db01`): the
  PartOps database holding the registry, jobs, logs, attention items and the
  framework version. The preflight evaluates it with the full `scheduler`
  scope, including `framework_version` and the new `control_target` check
  (exactly one enabled control row naming the connected database).
* **Managed target**: a database that receives partition DDL. Its health check
  uses the `target_execution` scope only (schema + the CREATE/DROP workers);
  no version or registry checks there.
* In Phase 5C the control database is the only executable target. Remote
  execution (Phase 5D) will need the workers installed on each target —
  from captured authoritative definitions only (Phase 5B.1, deferred).

## Remote execution (Phase 5D)

* **Control database** owns job definitions, registry, schedule, history,
  attention and the execution-attempt ledger. **Managed targets** hold only
  the database-specific execution framework: schema `mubasher_oms` with the
  legacy CREATE/DROP workers (USAGE + EXECUTE for the scheduler role). No
  PartOps tables or job metadata are copied to targets.
* Target-side work per attempt: identity check → advisory lock 87421032 →
  allowlisted `set_config(..., true)` → worker → COMMIT. Missing workers =
  `target_execution` preflight FAIL = execution blocked; uncaptured
  definitions stay WARNINGs (never "security reviewed").
* Test-only stub workers exist ONLY under `sql/tests/disposable/stubs/` for
  throwaway databases; they are not production definitions (enforced by tests).
* Details: `../../REMOTE_EXECUTION.md`.

## Validation (read-only)

* **DBA preflight:** `python scripts/preflight_framework.py --env-file <env>
  --expect-database <db> [--scope scheduler|api|all]` → PASS / WARNING / FAIL
  per check (object absent, wrong signature, wrong owner, wrong security mode,
  unsafe search_path, missing EXECUTE, unexpected PUBLIC EXECUTE, missing
  table/column, missing Phase 5A objects, framework version, legacy runners
  scheduled in pgAgent). Exit 2 on FAIL. `--snapshot-out` / `--snapshot-in`
  allow offline review.
* **Scheduler start gate:** the scheduler evaluates the `scheduler` scope at
  startup (as its own role, incl. EXECUTE on every function it calls directly
  or indirectly). While anything FAILs it dispatches **nothing** (no execution,
  no reconcile) and re-checks every `PARTITION_SCHEDULER_FRAMEWORK_RECHECK_SECONDS`
  (default 300). Status: `framework_status` on `/internal/scheduler/status`,
  metric `partops.scheduler.framework_ready`.
* **System page:** readiness category *Database* includes the manifest check.

## Capture workflow (DBA — PartOps never does this itself)

1. Identify and record the **approved reference database** (name, host, who approved).
2. Run the READ-ONLY capture: `sql/capture_reference_framework.sql` (header has
   the exact `psql` command). It runs in `BEGIN TRANSACTION READ ONLY` and ends
   with `ROLLBACK`.
3. Save the output locally (it contains code, not data; check for credentials
   embedded in function bodies before sharing — if any are found, STOP).
4. For each missing object, copy the section-5 definition verbatim into
   `sql/framework/captured/<name>.sql` with the header block from
   `captured/README.md` (source DB, capture time, md5).
5. Compare owner / `prosecdef` / `proconfig` (search_path) / EXECUTE grants
   (sections 3–4) with `framework_manifest.json`; record differences.
6. Complete `SECURITY_REVIEW_CHECKLIST.md` for every captured function.
7. Update the manifest entry: `definition_in_git: "yes"`, `status: "versioned"`,
   `source_files`, `security`, `expected_owner`, `execute_roles`; delete the
   `.CAPTURE_REQUIRED.md` placeholder; run
   `python scripts/framework_inventory.py --write`.
8. Run the backend tests and the disposable-database workflow
   (`scripts/disposable_db/run_disposable_db_test.sh`), which then also runs the
   full integration tests.
9. Review the diff; commit **only after human approval**. Close the matching
   issue only when the reviewed definition is in Git.

## Installation order on a NEW hub database (after capture)

1. Roles (`mubasher_oms`, `partition_job_ui`, `partition_job_scheduler`) — DBA.
2. `sql/bootstrap_partition_job_framework.sql` (tables/sequences/type/trigger; refuses on conflicts).
3. Captured legacy functions from `sql/framework/captured/*.sql` (reviewed).
4. `sql/realtime_scheduler_v1.sql` (its dependency gate requires step 3).
5. Migrations in order: `20260929_01`, `20260929_02`, `20260929_03`, `20261013_02`,
   `20261110_01`, `20261110_02`, `20261110_03` (`20260929_04`, `20261007_01`,
   `20261013_01` are historical/superseded by the canonical v1 bodies).
6. App-role grants not done by migrations (see manifest `table_privileges`,
   `usage_roles`, `execute_roles` for legacy functions).
7. `scripts/preflight_framework.py --scope all` → no FAIL.

Existing databases: apply only the missing migrations (RUNBOOK §16).
