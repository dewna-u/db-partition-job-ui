"""Phase 5D — the TARGET half of one remote execution attempt.

Two databases, no distributed transaction
-----------------------------------------
A remote job touches the CONTROL database (jobs, ledger, history, schedule)
and one managed TARGET database (partition DDL). PostgreSQL offers no atomic
commit across separate databases and PartOps does NOT use prepared (two-phase)
transactions. Instead every step is recorded in the control-database ledger
(``partops_execution_attempt``) with short, separate control transactions,
and the target outcome is classified conservatively:

    target transaction                         control ledger (separate txns)
    ------------------                         ------------------------------
    connect + identity check                   (attempt PREPARED by the claim)
    pg_try_advisory_xact_lock(87421032, key)
    set_config(allowlisted, is_local=true)
                                               mark_dispatching → DISPATCHING
    SELECT <schema>.create/drop worker(...)
    COMMIT
                                               record_success  → TARGET_SUCCEEDED
                                               (finalize later → FINALIZED)

Classification (``TargetOutcome.kind``):

* ``NOT_DISPATCHED`` — the worker was never called (no connection, identity
  mismatch, table lock busy, config rejected, control ledger unavailable).
  Proven: nothing applied. Retry-eligible (bounded).
* ``FAILED`` — the worker raised a server-side SQL error on a healthy
  connection and ROLLBACK succeeded. Proven: nothing committed. Retry-eligible.
* ``SUCCEEDED`` — COMMIT returned successfully. Never executed again; only the
  control finalization may be retried.
* ``UNKNOWN`` — anything else after DISPATCHING: connection lost during the
  worker, ROLLBACK failed, any error from COMMIT. Commit status cannot be
  proven, so the attempt is NEVER retried automatically (needs review).

This module performs no scheduling decisions and holds no state.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from database_targets import DatabaseTarget, TargetConnectionError, open_target_connection
from validators import is_allowed_db_config_parameter

logger = logging.getLogger(__name__)

NOT_DISPATCHED = "NOT_DISPATCHED"
FAILED = "FAILED"
SUCCEEDED = "SUCCEEDED"
UNKNOWN = "UNKNOWN"

# Same key derivation as run_partition_job_scheduled / run_partition_job_manual
# (namespace 87421032) so any PartOps-era local executor in the target database
# and remote executions serialise on the same table.
TARGET_LOCK_SQL = (
    "SELECT pg_try_advisory_xact_lock(87421032, "
    # '%%' = literal modulo (the statement is executed with parameters).
    "(abs(hashtext('partition_job_target:' || %s || '.' || %s)::bigint) %% 2147483647)::integer)"
)
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SECRETISH_RE = re.compile(r"(?i)(password|passwd|secret|token)\S*")
# SQLSTATE classes that mean the session / server went away (not a statement error).
_CONNECTION_SQLSTATE_PREFIXES = ("08", "57P0")


@dataclass(frozen=True)
class RemoteClaim:
    """Execution snapshot returned by ``partops_remote_claim`` (CLAIMED)."""

    attempt_id: int
    job_id: int
    database_target_id: int
    target_key: str
    operation: str
    table_schema: str
    table_name: str
    partition_unit: Optional[str]
    partition_period: Optional[int]
    create_drop_interval: Optional[str]
    db_config: Dict[str, Any] = field(default_factory=dict)
    failure_count: int = 0
    job_name: Optional[str] = None

    @classmethod
    def from_row(cls, job_id: int, row: Dict[str, Any]) -> "RemoteClaim":
        op = str(row.get("operation") or "").upper()
        if op not in ("CREATE", "DROP"):
            raise ValueError("claim returned an invalid operation")
        return cls(
            attempt_id=int(row["attempt_id"]),
            job_id=int(job_id),
            database_target_id=int(row["database_target_id"]),
            target_key=str(row.get("target_key") or ""),
            operation=op,
            table_schema=str(row.get("table_schema") or ""),
            table_name=str(row.get("table_name") or ""),
            partition_unit=row.get("partition_unit"),
            partition_period=(int(row["partition_period"])
                              if row.get("partition_period") is not None else None),
            create_drop_interval=row.get("create_drop_interval"),
            db_config=dict(row.get("db_config") or {}),
            failure_count=int(row.get("failure_count") or 0),
            job_name=row.get("job_name"),
        )


@dataclass
class TargetOutcome:
    kind: str
    error_class: Optional[str] = None
    summary: Optional[str] = None
    duration_ms: Optional[int] = None
    # False when the control ledger could not be updated after the target
    # result was known; the scheduler retries ONLY that ledger write.
    ledger_recorded: bool = True
    ledger_action: Optional[str] = None  # success | failure | unknown


class ControlLedger:
    """Control-database callbacks (each one short control transaction)."""

    def __init__(
        self,
        mark_dispatching: Callable[[int], str],
        record_success: Callable[[int, Optional[int]], Any],
        record_failure: Callable[[int, str, Optional[str], Optional[int]], Any],
        mark_unknown: Callable[[int, Optional[str]], Any],
    ) -> None:
        self.mark_dispatching = mark_dispatching
        self.record_success = record_success
        self.record_failure = record_failure
        self.mark_unknown = mark_unknown


def sanitize_error(exc: BaseException) -> str:
    """SQLSTATE + first line of the server message; never credentials."""
    sqlstate = getattr(exc, "sqlstate", None)
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    if primary is None and sqlstate:
        primary = str(exc)
    text = (str(primary or "").strip().splitlines() or [""])[0]
    text = _SECRETISH_RE.sub("[redacted]", text)[:300]
    prefix = "SQLSTATE {}".format(sqlstate) if sqlstate else type(exc).__name__
    return "{}: {}".format(prefix, text) if text else prefix


def _connection_usable(conn: Any) -> bool:
    return not bool(getattr(conn, "closed", False)) and not bool(getattr(conn, "broken", False))


def is_proven_statement_error(exc: BaseException, conn: Any) -> bool:
    """True only for a server-side statement error on a still-healthy session.

    Such an error aborts the target transaction before any COMMIT was sent;
    with a successful ROLLBACK afterwards nothing can have been committed.
    """
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    if not sqlstate:
        return False
    if sqlstate.startswith(_CONNECTION_SQLSTATE_PREFIXES):
        return False
    return _connection_usable(conn)


def _safe_rollback(conn: Any) -> bool:
    try:
        conn.rollback()
        return True
    except Exception:  # noqa: BLE001
        return False


def _ledger(action: Callable[[], Any], outcome: TargetOutcome, name: str, attempt_id: int) -> TargetOutcome:
    try:
        action()
    except Exception:  # noqa: BLE001 — control DB unavailable; scheduler retries
        logger.exception("event=remote_ledger_write_failed action=%s attempt_id=%s", name, attempt_id)
        outcome.ledger_recorded = False
        outcome.ledger_action = name
    return outcome


def _worker_sql(schema: str, operation: str) -> str:
    if not _SCHEMA_RE.match(schema):
        raise ValueError("framework schema must be a plain identifier")
    if operation == "CREATE":
        return ("SELECT {}.create_any_table_partition("
                "%s::character varying, %s::character varying, %s::character varying, "
                "%s::integer, %s::interval)").format(schema)
    if operation == "DROP":
        return ("SELECT {}.drop_any_table_partition("
                "%s::character varying, %s::character varying, %s::interval)").format(schema)
    raise ValueError("unsupported operation")


def _worker_params(claim: RemoteClaim) -> tuple:
    if claim.operation == "CREATE":
        return (claim.table_schema, claim.table_name, claim.partition_unit,
                claim.partition_period, claim.create_drop_interval)
    return (claim.table_schema, claim.table_name, claim.create_drop_interval)


def run_target_transaction(
    target: DatabaseTarget,
    claim: RemoteClaim,
    ledger: ControlLedger,
    *,
    schema: str = "mubasher_oms",
    connect: Optional[Callable[..., Any]] = None,
) -> TargetOutcome:
    """Execute one claimed attempt on ``target``. Blocking; call via a thread."""
    attempt = claim.attempt_id

    def not_dispatched(error_class: str, summary: str, record: bool = True) -> TargetOutcome:
        out = TargetOutcome(NOT_DISPATCHED, error_class=error_class, summary=summary)
        if not record:
            return out
        return _ledger(lambda: ledger.record_failure(attempt, error_class, summary, None),
                       out, "failure", attempt)

    if target.target_id != claim.database_target_id:
        return not_dispatched("TARGET_MISMATCH", "claim target differs from routing target")
    try:
        worker_sql = _worker_sql(schema, claim.operation)
    except ValueError as exc:
        return not_dispatched("INVALID_REQUEST", str(exc))
    for key, value in claim.db_config.items():
        if not is_allowed_db_config_parameter(str(key), str(value)):
            return not_dispatched("CONFIG_REJECTED",
                                  "db_config_para parameter {!r} is not allowlisted".format(str(key)[:60]))

    try:
        cm = open_target_connection(target, "remote execution", connect=connect)
        conn = cm.__enter__()
    except TargetConnectionError as exc:
        return not_dispatched(exc.kind, str(exc))
    except Exception as exc:  # noqa: BLE001
        return not_dispatched("CONNECT_ERROR", type(exc).__name__)

    try:
        return _run_on_connection(conn, target, claim, ledger, worker_sql, not_dispatched)
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.exception("event=remote_connection_close_error target=%s", target.target_key)


def _run_on_connection(conn, target, claim, ledger, worker_sql, not_dispatched) -> TargetOutcome:
    attempt = claim.attempt_id
    # --- before dispatch: nothing can be applied -----------------------------
    try:
        with conn.cursor() as cur:
            cur.execute(TARGET_LOCK_SQL, (claim.table_schema or "", claim.table_name or ""))
            row = cur.fetchone()
            if not row or not row[0]:
                _safe_rollback(conn)
                return not_dispatched("TARGET_LOCKED", "target table busy on {}".format(target.target_key))
            for key, value in claim.db_config.items():
                cur.execute("SELECT set_config(%s, %s, true)", (str(key), str(value)))
    except Exception as exc:  # noqa: BLE001 — worker not called yet
        _safe_rollback(conn)
        return not_dispatched("PRE_DISPATCH_ERROR", sanitize_error(exc))

    try:
        state = ledger.mark_dispatching(attempt)
    except Exception:  # noqa: BLE001 — control DB unavailable: do not dispatch
        logger.exception("event=remote_mark_dispatching_failed attempt_id=%s", attempt)
        _safe_rollback(conn)
        return TargetOutcome(NOT_DISPATCHED, error_class="CONTROL_UNAVAILABLE",
                             summary="control ledger unavailable before dispatch")
    if state != "DISPATCHING":
        _safe_rollback(conn)
        return not_dispatched("STATE_CONFLICT", "attempt is {}".format(state), record=False)

    # --- DISPATCHING: from here on, doubt means UNKNOWN ----------------------
    started = time.monotonic()

    def elapsed() -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    def unknown(summary: str) -> TargetOutcome:
        out = TargetOutcome(UNKNOWN, error_class="OUTCOME_UNKNOWN", summary=summary, duration_ms=elapsed())
        logger.error("event=remote_outcome_unknown attempt_id=%s target=%s (%s) — NOT retried",
                     attempt, target.target_key, summary)
        return _ledger(lambda: ledger.mark_unknown(attempt, summary), out, "unknown", attempt)

    try:
        with conn.cursor() as cur:
            cur.execute(worker_sql, _worker_params(claim))
    except Exception as exc:  # noqa: BLE001
        summary = sanitize_error(exc)
        if is_proven_statement_error(exc, conn) and _safe_rollback(conn):
            out = TargetOutcome(FAILED, error_class="WORKER_ERROR", summary=summary, duration_ms=elapsed())
            return _ledger(lambda: ledger.record_failure(attempt, "WORKER_ERROR", summary, out.duration_ms),
                           out, "failure", attempt)
        return unknown("worker call interrupted; rollback not proven: " + summary)

    try:
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — commit status cannot be proven
        return unknown("COMMIT did not confirm: " + sanitize_error(exc))

    out = TargetOutcome(SUCCEEDED, duration_ms=elapsed())
    return _ledger(lambda: ledger.record_success(attempt, out.duration_ms), out, "success", attempt)
