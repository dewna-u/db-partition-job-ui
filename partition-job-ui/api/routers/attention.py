"""Phase 5A — missed-run attention review (DBA workflow).

GET  /api/attention                      list (default: pending review)
POST /api/attention/{id}/acknowledge     acknowledge WITHOUT executing
POST /api/attention/{id}/recover         run the missed occurrence manually

Neither action changes the job's normal next_run_time.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import ok, raise_for_domain
from api.remote_manual import is_remote_job, run_remote_manual
from database import (
    ATTENTION_STATUS_PENDING,
    DatabaseConflictError,
    DatabaseError,
    get_partition_job,
    acknowledge_partition_job_attention,
    get_partition_job_attention,
    get_partition_job_attention_item,
    recover_partition_job_attention,
)

router = APIRouter(tags=["attention"])

_STATUS_FILTERS = {
    "pending": ATTENTION_STATUS_PENDING,
    "acknowledged": "ACKNOWLEDGED",
    "manually_rerun": "MANUALLY_RERUN",
    "all": None,
}


class AcknowledgeBody(BaseModel):
    confirm: bool = False
    note: Optional[str] = Field(default=None, max_length=500)


class RecoverBody(BaseModel):
    confirm: bool = False
    confirm_drop: bool = False
    # DROP recovery: operator must type the exact "schema.table" target.
    confirm_target: Optional[str] = Field(default=None, max_length=200)


def attention_target(item: dict[str, Any]) -> str:
    return "{}.{}".format(item.get("table_schema") or "", item.get("table_name") or "")


def requires_strong_confirmation(item: dict[str, Any]) -> bool:
    """DROP (or unknown operation) recovery is destructive → stronger confirm."""
    return item.get("is_create") is not True


def _load_pending(attention_id: int) -> dict[str, Any]:
    item = get_partition_job_attention_item(attention_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"Attention item {attention_id} not found."
        )
    if item.get("status") != ATTENTION_STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Attention item {attention_id} is already "
                f"{item.get('status')}; nothing was changed."
            ),
        )
    return item


@router.get("/attention")
def list_attention(
    status: str = Query(default="pending"),
    limit: int = Query(default=200, ge=1, le=1000),
):
    key = (status or "pending").strip().lower()
    if key not in _STATUS_FILTERS:
        raise HTTPException(
            status_code=400,
            detail="status must be one of: pending, acknowledged, manually_rerun, all.",
        )
    try:
        items, available = get_partition_job_attention(_STATUS_FILTERS[key], limit)
    except DatabaseError as exc:
        raise_for_domain(exc)
    pending = sum(1 for i in items if i.get("status") == ATTENTION_STATUS_PENDING)
    return ok(
        {
            "items": items,
            "available": available,
            "pending_count": pending,
            "message": None
            if available
            else "Missed-run review requires the Phase 5A database migrations.",
        }
    )


@router.post("/attention/{attention_id}/acknowledge")
def acknowledge_attention(attention_id: int, body: Optional[AcknowledgeBody] = None):
    body = body or AcknowledgeBody()
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Acknowledging a missed run requires confirm=true.",
        )
    try:
        _load_pending(attention_id)
        result = acknowledge_partition_job_attention(attention_id, body.note)
    except HTTPException:
        raise
    except DatabaseError as exc:
        raise_for_domain(exc)
    return ok(
        {
            "result": result,
            "message": (
                "Missed run acknowledged. Nothing was executed; the normal "
                "schedule is unchanged."
            ),
        }
    )


@router.post("/attention/{attention_id}/recover")
def recover_attention(attention_id: int, body: Optional[RecoverBody] = None):
    body = body or RecoverBody()
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Running a missed occurrence requires confirm=true.",
        )
    try:
        item = _load_pending(attention_id)
        if requires_strong_confirmation(item):
            target = attention_target(item)
            if not body.confirm_drop:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This missed run is a DROP. Recovery requires "
                        "confirm_drop=true because it may permanently remove "
                        "partition data."
                    ),
                )
            if (body.confirm_target or "").strip() != target:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Type the exact target table '{target}' to confirm "
                        "the DROP recovery."
                    ),
                )
        if item.get("reason") == "REMOTE_OUTCOME_UNKNOWN":
            # Phase 5D: an unknown remote outcome is never re-run from here.
            raise DatabaseConflictError(
                "The target outcome of this run is unknown. Inspect the target and "
                "resolve the execution attempt (APPLIED / NOT_APPLIED) instead of re-running it."
            )
        # Pre-5D / control-database items carry no target: local SQL path.
        job = get_partition_job(item.get("job_id")) if item.get("database_target_id") is not None else None
        if job is not None and is_remote_job(job):
            # Phase 5D: managed target → scheduler engine; the attention item is
            # marked MANUALLY_RERUN only after a confirmed, finalized success.
            remote = run_remote_manual(int(item["job_id"]), confirm_drop=True,
                                       attention_id=attention_id)
            result = {"status": remote.get("status"), "message": remote.get("message"),
                      "execution_duration_ms": remote.get("execution_duration_ms"),
                      "attention_status": "MANUALLY_RERUN" if remote.get("status") == "MANUAL_SUCCESS"
                      else "PENDING_REVIEW",
                      "job_id": item.get("job_id"), "attempt_id": remote.get("attempt_id")}
        else:
            result = recover_partition_job_attention(attention_id)
    except HTTPException:
        raise
    except DatabaseError as exc:
        raise_for_domain(exc)
    return ok(
        {
            "result": result,
            "message": (
                f"Missed occurrence for job {result.get('job_id')} executed manually "
                "and committed. The normal scheduled next run is unchanged."
            ),
        }
    )
