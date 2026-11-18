from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query

from api.deps import ok, raise_for_domain
from database import (
    HISTORY_MAX_LIMIT,
    HISTORY_MAX_OFFSET,
    DatabaseError,
    list_database_targets,
    query_partition_job_logs,
)

router = APIRouter(tags=["logs"])


def _target_keys() -> tuple[dict[int, str], str | None]:
    """target_id -> target_key and the control key (no secrets). Best effort."""
    try:
        targets, _invalid, available = list_database_targets()
    except DatabaseError:
        return {}, None
    if not available:
        return {}, None
    keys = {t.target_id: t.target_key for t in targets}
    control = next((t.target_key for t in targets if t.is_control_database), None)
    return keys, control


@router.get("/logs")
def list_logs(
    limit: int = Query(default=100, ge=1, le=HISTORY_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=HISTORY_MAX_OFFSET),
    target_id: Optional[int] = Query(default=None, ge=1),
    status: Optional[str] = Query(default=None, pattern=r"^[A-Za-z_]{1,40}$"),
    job: Optional[str] = Query(default=None, max_length=100),
    operation: Optional[str] = Query(default=None, pattern=r"^(?i:create|drop)$"),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
):
    """Phase 5F.1: one page of history (server-side filters + pagination)."""
    try:
        page = query_partition_job_logs(
            limit=limit, offset=offset, target_id=target_id, status=status, job=job,
            operation=operation, since=since, until=until)
    except DatabaseError as exc:
        raise_for_domain(exc)
    keys, control = _target_keys()
    for row in page["logs"]:
        row.setdefault("database_target_id", None)
        row.setdefault("execution_attempt_id", None)
        target = row.get("database_target_id")
        # NULL = control database (local executors and every pre-5D row).
        row["target_key"] = keys.get(target) if target is not None else control
    return ok(page)
