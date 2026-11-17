"""Phase 5D — API side of manual runs on MANAGED (non-control) targets.

The API never connects to a managed target to run DDL itself. It asks the
scheduler, which owns the single remote execution engine (claim → target
transaction → ledger → finalization) and its crash recovery. Control-database
jobs keep the existing ``run_partition_job_manual`` SQL path.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from database import DatabaseConflictError, DatabaseError, list_database_targets
from scheduler_client import (
    REMOTE_RUN_TIMEOUT,
    REMOTE_RUN_UNAVAILABLE,
    request_remote_manual_run,
)

# Nothing was executed; the request conflicts with current state.
_CONFLICT_STATUSES = frozenset({
    "MANUAL_TARGET_LOCKED", "MANUAL_TARGET_BUSY", "MANUAL_TARGET_UNAVAILABLE",
    "MANUAL_NOT_RUN", "BUSY", "NEEDS_REVIEW", "SKIPPED_TARGET_DISABLED",
    "SKIPPED_LOCKED", "DROP_NOT_CONFIRMED", "CONFIG_REJECTED", "ATTENTION_REJECTED",
    "INVALID_JOB", "NOT_REMOTE", "SCHEDULER_NOT_READY",
})


def is_remote_job(job: dict[str, Any]) -> bool:
    """True when the job's target is a registered non-control target."""
    target_id = job.get("database_target_id")
    if target_id is None:
        return False
    try:
        targets, _invalid, available = list_database_targets()
    except DatabaseError:
        raise
    if not available:
        return False
    for target in targets:
        if target.target_id == int(target_id):
            return not target.is_control_database
    # Unknown / misconfigured target: never run it locally.
    raise DatabaseConflictError(
        f"Job's database target {target_id} is not usable (misconfigured or missing)."
    )


def run_remote_manual(
    job_id: int, confirm_drop: bool, attention_id: Optional[int] = None
) -> dict[str, Any]:
    """Run via the scheduler engine; map outcomes to API results / errors."""
    result = request_remote_manual_run(job_id, confirm_drop=confirm_drop, attention_id=attention_id)
    status = str(result.get("status") or REMOTE_RUN_UNAVAILABLE)
    message = str(result.get("message") or "") or None
    if status in ("MANUAL_SUCCESS", "MANUAL_FINALIZE_PENDING"):
        return result
    if status == "MANUAL_FAIL":
        raise DatabaseError(
            (message or "Manual partition job execution failed on the target.")
            + " The target transaction was rolled back; the failure was recorded."
        )
    if status == "MANUAL_OUTCOME_UNKNOWN":
        raise HTTPException(status_code=502, detail=message or (
            "The target outcome could not be confirmed. DO NOT re-run until the target "
            "state has been inspected; resolve it under Needs review."))
    if status == REMOTE_RUN_TIMEOUT:
        raise HTTPException(status_code=504, detail=message)
    if status == REMOTE_RUN_UNAVAILABLE:
        raise HTTPException(status_code=503, detail=message)
    if status in _CONFLICT_STATUSES:
        raise DatabaseConflictError(message or f"Nothing was executed ({status}).")
    raise DatabaseConflictError(message or f"Nothing was executed ({status}).")
