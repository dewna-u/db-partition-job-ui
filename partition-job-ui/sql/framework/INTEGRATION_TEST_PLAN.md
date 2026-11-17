# Full integration test plan (disposable database only)

**Status:** the cases below are written (`sql/tests/disposable/40_integration_full.sql`,
`test_framework_integration.py`) but **cannot run yet**. Each one is skipped with

```text
FRAMEWORK DEFINITION NOT AVAILABLE: <missing captured objects>
```

until the authoritative definitions are captured into `sql/framework/captured/`.
They are **not** reported as passing. Requires a disposable PostgreSQL/EDB
cluster (`scripts/disposable_db/run_disposable_db_test.sh`), synthetic tables
only, no production data.

Assertions are deliberately at the **PartOps contract level** (status values,
log rows, `next_run_time`, attention rows, locks, grants). Worker-internal
behaviour (partition names, bounds) is only checked as "partition count did not
decrease after CREATE / did not increase after DROP". The synthetic fixture
(range-partitioned `itest.orders` / `itest.trades`) **must be adapted** to what
the captured workers actually expect — marked `FIXTURE ADAPTATION REQUIRED`.

| ID | Area | Case | Expected (PartOps contract) |
|---|---|---|---|
| C1 | CREATE | scheduled CREATE, owned, due | `EXECUTED`; one log `SUCCESS` with duration; `last_run_status=SUCCESS`; `next_run_time` > now and = cron helper; partitions not fewer |
| C2 | CREATE | manual CREATE | `MANUAL_SUCCESS`; log `MANUAL_SUCCESS`; `next_run_time`/`last_run_*` unchanged |
| C3 | CREATE | missed CREATE (historical reconcile) | `MISSED_RESCHEDULED`; one attention row PENDING; **no** log row; partitions unchanged; `last_run_*` unchanged |
| C4 | CREATE | manual recovery of C3 | `MANUAL_SUCCESS` → attention `MANUALLY_RERUN`; `next_run_time` unchanged |
| D1 | DROP | scheduled DROP | as C1 with partitions not more than before |
| D2 | DROP | manual DROP | as C2 |
| D3 | DROP | missed DROP | as C3 (no partition removed) |
| D4 | DROP | manual recovery DROP | as C4 |
| S1 | Scheduler | future occurrence | `SKIPPED_NOT_DUE`, nothing changed |
| S2 | Scheduler | historical occurrence skipped | = C3/D3 |
| S3 | Scheduler | retry exhaustion → DB | reconcile `RETRY_EXHAUSTED` creates one attention row with that reason |
| S4 | Scheduler | target lock | held 87421032 key → `SKIPPED_TARGET_LOCKED` / `MANUAL_TARGET_LOCKED` (also in smoke) |
| S5 | Scheduler | next_run_time advancement | after C1, `next_run_time` equals the cron helper result evaluated in the same transaction window |
| SEC1 | Security | correct roles | scheduler executes scheduled/reconcile; UI executes manual/recovery/ack/update |
| SEC2 | Security | incorrect roles / PUBLIC | scheduler cannot run recovery; UI cannot run scheduled/reconcile; a no-grant login cannot execute any |
| SEC3 | Security | invalid db_config | job with `{"search_path": …}`: scheduled → `FAILED` (no worker), manual → `MANUAL_FAIL` |
| T1 | Transaction | worker fails | job on non-existent table: scheduled `FAILED` + log error + schedule advanced; manual `MANUAL_FAIL` committed |
| T2 | Transaction | DDL rollback | failing worker leaves partition count unchanged |
| T3 | Transaction | history log correctness | exactly one log row per execution attempt; none for skips/missed |
| T4 | Transaction | attention correctness | one row per (job, missed time); evidence immutable; single resolution |

Already covered without the legacy functions (`20_phase5a_smoke.sql`,
`30_target_lock.sql`): catalog/security matrix, allowlist, historical
reconcile (fail-closed path), DB ceiling, owned-late not missed, attention
immutability, recovery failure path, config drift, manual allowlist, target
locks, failed-jobs gauge, framework version, preflight and capture script.
Python-level retry/backoff/ownership behaviour is covered by the unit suite.

## Phase 5C — target registry and routing (disposable)

Implemented in `scripts/disposable_db/run_disposable_db_test.sh` (stages 5c, 6c):

| Step | File | Proves |
|---|---|---|
| 5c | `sql/tests/disposable/15_pre5c_jobs.sql` | jobs exist before the registry |
| 5c | `20261117_01` + `20261117_02` applied twice | idempotent |
| U1 | `25_phase5c_targets.sql` | default target `db01` = `current_database()`; pre-5C jobs backfilled; config unchanged |
| U2 | 〃 | new rows default to the control target |
| U3 | 〃 | remote-target job: not discovered, `SKIPPED_REMOTE_TARGET` (scheduled + reconcile), no attention/log/schedule change |
| U4 | 〃 | manual run → `MANUAL_REMOTE_TARGET`, nothing logged; registry read-only for the UI role |
| U5 | 〃 | one control row; no secret values; FK protects targets with jobs |
| 6c | `target_routing_check.py` | control vs target routing to two real databases, identity check, per-target health (`db03` DISABLED, `db04` UNREACHABLE) isolated |

No legacy worker or stub is involved: guards return before any worker call.
With captured definitions available (Phase 5B.1), add: install the workers on
`partops_target2` and expect `HEALTHY_WITH_WARNINGS`/`HEALTHY` there.

## Phase 5D — remote execution on real databases (disposable)

Stage 11 of `scripts/disposable_db/run_disposable_db_test.sh` installs the
TEST-ONLY stub workers / cron helper (`sql/tests/disposable/stubs/`, never
production) into the throwaway control DB and `partops_target2`, then runs
`scripts/disposable_db/remote_execution_check.py`:

| Part | Proves |
|---|---|
| A0 | target-aware discovery from the control DB; disabled target excluded |
| A1 | db02 routing, target COMMIT, TARGET_SUCCEEDED → FINALIZED, history with target/attempt, next run |
| A2 | worker error + proven rollback → bounded retry on the same attempt → RETRY_EXHAUSTED |
| A3 | connection lost around COMMIT → OUTCOME_UNKNOWN, paused job, NEEDS_REVIEW, 55006 guards, resolution |
| A4 | commit ok + finalization missing → recovery finalizes only (DDL executed once) |
| A5 | restart while DISPATCHING → OUTCOME_UNKNOWN, never redispatched |
| A6 | db01 job via the local executor touches db01 only |
| A7 | disabled target refused; unreachable target → NOT_DISPATCHED (retry-eligible) |
| A8 | target-side table lock; same schema.table on another DB not blocked |
| A9 | manual remote run (schedule unchanged); DROP confirmation |
| A10 | app roles cannot write the ledger; claim is scheduler-only |
| B1 | real scheduler: global 2 / db01 1 / db02 1 — both targets concurrently, limits held, db04 down isolated |
| B2 | real scheduler: global 3 / db01 1 / db02 2 — limits held |

Not covered by disposable tests: WRONG_DATABASE on a live connection (needs a
proxy/alias; unit-tested), EDB, the real legacy workers (Phase 5B.1 deferred).
