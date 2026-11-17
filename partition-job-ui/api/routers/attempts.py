"""Phase 5D — remote execution attempts (control-database ledger).

GET  /api/execution-attempts                  list (filters: state, open, job_id)
GET  /api/execution-attempts/{id}             one attempt
POST /api/execution-attempts/{id}/resolve     operator resolution of OUTCOME_UNKNOWN

No endpoint re-runs anything: an unknown target outcome is resolved only after
the operator inspected the target (APPLIED / NOT_APPLIED + note). Responses
never contain credentials, hosts or secret identifiers.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import ok, raise_for_domain
from database import (
    DatabaseError,
    get_execution_attempt,
    list_execution_attempts,
    resolve_remote_outcome,
)
from scheduler_client import notify_scheduler_refresh

router = APIRouter(tags=["execution-attempts"])


class ResolveBody(BaseModel):
    confirm: bool = False
    resolution: str = Field(..., pattern="^(APPLIED|NOT_APPLIED)$")
    note: str = Field(..., min_length=3, max_length=400)
    # DROP: operator must type the exact "schema.table" that was inspected.
    confirm_target: Optional[str] = Field(default=None, max_length=200)


@router.get("/execution-attempts")
def list_attempts(
    state: Optional[str] = Query(default=None, max_length=20),
    open: bool = Query(default=False),
    job_id: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=1000),
):
    try:
        attempts, available = list_execution_attempts(
            state=state.upper() if state else None, open_only=open, job_id=job_id, limit=limit)
    except DatabaseError as exc:
        raise_for_domain(exc)
    return ok({
        "attempts": attempts,
        "available": available,
        "message": None if available else
        "Remote execution attempts require migrations 20261117_03 / 20261117_04.",
    })


@router.get("/execution-attempts/{attempt_id}")
def get_attempt(attempt_id: int):
    try:
        attempt = get_execution_attempt(attempt_id)
    except DatabaseError as exc:
        raise_for_domain(exc)
    if attempt is None:
        raise HTTPException(status_code=404, detail=f"Execution attempt {attempt_id} not found.")
    return ok({"attempt": attempt})


@router.post("/execution-attempts/{attempt_id}/resolve")
def resolve_attempt(attempt_id: int, body: ResolveBody):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Resolving an unknown outcome requires confirm=true.")
    try:
        attempt = get_execution_attempt(attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail=f"Execution attempt {attempt_id} not found.")
        if attempt.get("state") != "OUTCOME_UNKNOWN":
            raise HTTPException(status_code=409,
                                detail=f"Execution attempt {attempt_id} is {attempt.get('state')}; "
                                       "only OUTCOME_UNKNOWN can be resolved.")
        if attempt.get("operation") == "DROP":
            target = "{}.{}".format(attempt.get("table_schema") or "", attempt.get("table_name") or "")
            if (body.confirm_target or "").strip() != target:
                raise HTTPException(status_code=400, detail=(
                    f"Type the exact table '{target}' you inspected on target "
                    f"{attempt.get('target_key')} to resolve this DROP."))
        result = resolve_remote_outcome(attempt_id, body.resolution, body.note)
    except HTTPException:
        raise
    except DatabaseError as exc:
        raise_for_domain(exc)
    refresh_ok, refresh_message = notify_scheduler_refresh()
    return ok({
        "result": result,
        "refresh_ok": refresh_ok,
        "refresh_message": refresh_message,
        "message": "Unknown outcome resolved. Nothing was executed; automatic scheduling resumes "
                   "for enabled jobs.",
    })
