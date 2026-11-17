# PartOps — Phase 5D: target-aware scheduler and safe remote execution

**ONE PARTOPS INSTANCE PER HUB / APPROXIMATELY TWO TARGET DATABASES PER HUB /
NO GLOBAL MULTI-HUB CONTROL PLANE.**

This document is the design reference for running partition jobs on managed
target databases (db02, …) from the hub's PartOps. The control database
(db01) keeps the proven Phase 5A executor unchanged.

## 1. Architecture

```text
HUB A  (another hub = a completely separate installation)

                    PartOps  (UI + API + scheduler, one per hub)
                        │
                        │  short CONTROL transactions only
                        ▼
   ┌──────────────── CONTROL DATABASE  (= target db01) ────────────────┐
   │ job definitions · target registry · next_run_time · last status   │
   │ execution-attempt ledger · history log · attention / review       │
   │ framework version · scheduler coordination (job advisory locks)   │
   └───────────────────────────────────────────────────────────────────┘
          │ db01 jobs: run_partition_job_scheduled          │ db02+ jobs: remote orchestrator
          │ (local executor, unchanged Phase 5A path)       │ (separate TARGET transaction)
          ▼                                                  ▼
     DB-01 (control)                                    DB-02 (managed target)
     legacy workers                                     legacy workers only — NO PartOps
                                                        tables, NO copy of job metadata
```

* Discovery: `partops_get_upcoming_jobs(lookahead)` reads the CONTROL DB only
  and returns jobs of every **enabled** target with `database_target_id` and
  `target_key`. Target databases are never queried for discovery.
* db01 jobs → `run_partition_job_scheduled` (no regression risk: the Phase
  5A/5B/5C executor is untouched). db02+ jobs → the remote orchestrator below.
* Manual runs of db02+ jobs go through the **same** engine inside the scheduler
  (`POST /internal/scheduler/manual-run`, localhost); the API never runs remote
  DDL itself. db01 manual runs keep `run_partition_job_manual`.

## 2. Two databases, no distributed transaction

There is no atomic commit across the control and target databases, and
PartOps does **not** use prepared (two-phase) transactions, dblink or FDW.
Every step is recorded in the control ledger `partops_execution_attempt` with
separate, short control transactions; no control transaction or row lock is
held while target DDL runs.

```text
CONTROL (short txns)                         TARGET (one txn)
────────────────────                         ────────────────
1 partops_remote_claim        → PREPARED
  (job lock, revalidate, snapshot,
   allowlist, next occurrence validated)
                                             2 connect + current_database() check
                                               (+ cached target_execution preflight)
                                             3 pg_try_advisory_xact_lock(87421032, schema.table)
                                             4 set_config(allowlisted, is_local = true)
5 partops_remote_mark_dispatching → DISPATCHING
                                             6 SELECT mubasher_oms.<create|drop>_any_table_partition(...)
                                             7 COMMIT
8 partops_remote_record_success → TARGET_SUCCEEDED
9 partops_remote_finalize       → FINALIZED
  (history SUCCESS, last_run_*, next_run_time;
   is_enabled never touched)
```

## 3. Attempt state machine

```text
             claim                  mark_dispatching          COMMIT ok → record_success
  (none) ──────────▶ PREPARED ─────────────────────▶ DISPATCHING ─────────────────▶ TARGET_SUCCEEDED
                       │  ▲                                │  │                            │ finalize
     not dispatched /  │  │ re-claim (bounded retry)       │  │ outcome cannot be proven   ▼
     proven rollback   ▼  │                                │  └──────────────▶ OUTCOME_UNKNOWN ──▶ FINALIZED
                    TARGET_FAILED ◀────────────────────────┘                   (operator resolution:
                       │        worker SQL error + ROLLBACK ok                  APPLIED / NOT_APPLIED)
                       ▼
                    FINALIZED (retry exhausted / abandoned / manual)
```

The trigger `trg_partops_execution_attempt_guard` allows only these
transitions. In particular DISPATCHING and OUTCOME_UNKNOWN can **never** go
back to PREPARED (= re-dispatch).

## 4. Classification rules (conservative)

| Situation | Class | Automatic action |
|---|---|---|
| cannot connect / auth failed / identity mismatch / target disabled | NOT_DISPATCHED (proven) | bounded retry; target cooldown |
| preflight (`target_execution`) failed | NOT_DISPATCHED | bounded retry (missing worker = blocked) |
| target table lock busy | NOT_DISPATCHED | contention cooldown (5/10/20/30 s, ≤ 1 h) |
| control ledger unavailable before dispatch | NOT_DISPATCHED (target rolled back) | bounded retry |
| worker raised a server-side SQL error on a healthy session **and** ROLLBACK succeeded | FAILED (proven) | bounded retry (15/60/180 s, 4 attempts) → `RETRY_EXHAUSTED` attention, schedule advances, `last_run_status = FAIL` |
| connection lost while the worker ran; ROLLBACK failed; any error from COMMIT; process died while DISPATCHING | **UNKNOWN** | **never retried** — `REMOTE_OUTCOME_UNKNOWN` attention, job scheduling paused, edits/re-enable blocked |
| COMMIT confirmed but control write / finalization failed | SUCCEEDED | retry **control finalization only** (persisted; survives restart); DDL never re-run |

If the code cannot prove a rollback, the outcome is UNKNOWN.

## 5. Unknown outcome workflow

* Attempt → OUTCOME_UNKNOWN; history row `OUTCOME_UNKNOWN`; attention item
  `REMOTE_OUTCOME_UNKNOWN` (target, job, operation, table, expected run,
  dispatch time, reason) with "DO NOT re-run until the target state has been
  inspected".
* The job's `next_run_time` is set to NULL (paused). The claim refuses any new
  scheduled or manual execution of the job (`NEEDS_REVIEW`). Target / table /
  operation / schedule / config changes and re-enabling are refused in SQL
  (`55006`); disabling is allowed.
* The attention item cannot be "re-run" or acknowledged before the attempt is
  resolved.
* Resolution (API now, UI in Phase 5E): `POST /api/execution-attempts/{id}/resolve`
  `{confirm, resolution: APPLIED|NOT_APPLIED, note, confirm_target (DROP)}` →
  `partops_resolve_remote_outcome`: attempt FINALIZED (`RESOLVED_*`), history
  row, attention ACKNOWLEDGED, scheduling resumed (next legitimate occurrence).
  Nothing is executed.

## 6. Crash / restart recovery

At scheduler start, after the framework gate and control-target check and
before any dispatch, `partops_remote_recover_attempts` runs (single scheduler
per hub, enforced by the process lock file):

| State found | Action |
|---|---|
| DISPATCHING | → OUTCOME_UNKNOWN (+ attention, job paused). Never redispatched. |
| TARGET_SUCCEEDED | control finalization only (retried until done) |
| OUTCOME_UNKNOWN | stays for review |
| PREPARED / TARGET_FAILED still live | resumable: the next claim revalidates and re-snapshots |
| PREPARED / TARGET_FAILED stale, or any manual PREPARED | FINALIZED `ABANDONED_RESTART` (nothing was applied) |

If recovery cannot run, the scheduler stays blocked.

## 7. Missed-run semantics (unchanged Phase 5A)

Ownership is decided at first sighting in the scheduler for every target:
historical occurrences are only reconciled (`reconcile_missed_partition_job`,
now target-aware, never while an attempt is in flight). Owned occurrences may
wait for a global slot, a target slot, a table lock or a bounded retry. The
6-hour DB lateness ceiling is enforced again inside the claim.

## 8. Concurrency

* Global: `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS`. Per target:
  `partops_database_target.max_concurrent_jobs` (default 1). Both enforced for
  scheduled and manual runs (db01 included).
* Acquisition order is always **target slot → global slot** (release in reverse).
  A task waiting for a saturated target holds no global slot, so one busy
  target never consumes global capacity another target could use, and there is
  no lock-order cycle.
* Central table identity `(target_id, schema, table)` (in-process); inside each
  target the shared advisory key `87421032 / partition_job_target:schema.table`
  serialises CREATE vs DROP on the same table (same key as the local executors).
* **Compatibility note:** db01 now also honours its own `max_concurrent_jobs`
  (default 1). A hub that runs `PARTITION_SCHEDULER_MAX_CONCURRENT_JOBS > 1` for
  db01 jobs must raise db01's `max_concurrent_jobs` (DBA) to keep that
  parallelism; the scheduler logs `control_target_limit_below_global`.

## 9. Target failure isolation

A connection-level failure (unreachable, auth, identity, config) puts only that
target into a capped exponential cooldown (15 s → 300 s; configurable). Its
owned occurrences are re-queued to the end of the cooldown without using a
slot or a connection; the first dispatch afterwards is the probe (one retry
attempt). Other targets are unaffected. The per-target `target_execution`
preflight is cached (TTL 300 s, bounded, invalidated on failure).

## 10. Edit / disable during remote execution

* The attempt snapshots WHAT runs (operation, table, partition args, config,
  validated next occurrence) under the job lock; the target always executes
  the snapshot.
* While an attempt is DISPATCHING / TARGET_SUCCEEDED / OUTCOME_UNKNOWN, the
  job's target, table, operation, schedule, partition args and config cannot
  change (`trg_partops_job_execution_guard`, SQLSTATE 55006 → HTTP 409).
* Disabling never interrupts DDL already running; finalization never changes
  `is_enabled`. Re-enabling later uses the existing stale-next-run protection.
* Moving a job to another target requires `confirm_dangerous=true`, an enabled
  and healthy target, and no open attempt (checked in the API and in SQL).

## 11. Security

* Ledger: SELECT only for app roles; every write through SECURITY DEFINER
  functions (owner `mubasher_oms`, `search_path = pg_catalog, mubasher_oms,
  pg_temp`, PUBLIC revoked). Scheduler-only: claim / dispatch / record /
  finalize / unknown / recover / close. UI-only: resolve, set target.
* No dynamic SQL with job data: worker calls, advisory lock and `set_config`
  are fixed statements with bound parameters; db_config is allowlisted twice
  (claim in SQL, engine in Python). Target identity verified before use.
* No credentials, secret ids, hosts or connection strings in the ledger,
  history, attention, API or metrics; error text is bounded and redacted.
* Registry writes remain DBA-only.

## 12. Observability (low cardinality: `target`, `outcome`, `status` only)

`partops.target.active_jobs|waiting_jobs|max_concurrent_jobs|available`,
`partops.remote.execution_attempt{outcome}`, `partops.remote.known_failure`,
`partops.remote.outcome_unknown`, `partops.remote.finalization_retry`,
`partops.remote.unknown_outcomes` (gauge), `partops.target.healthy`,
`partops.job.execution{status,target}`. Attempt ids appear only in logs.

## 13. Limits caused by the deferred Phase 5B.1

The legacy workers are external contracts (definitions not in Git). The
"proven rollback" rule assumes they are ordinary transactional functions.
If a captured worker turns out to use autonomous transactions (EDB `PRAGMA
AUTONOMOUS_TRANSACTION`), dblink or other non-transactional side effects, the
classification must be revisited (ISSUE-013). Disposable tests use TEST-ONLY
stub workers and validate PartOps orchestration only.
