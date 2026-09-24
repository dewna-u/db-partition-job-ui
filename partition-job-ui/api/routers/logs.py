from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import ok, raise_for_domain
from database import DatabaseError, get_partition_job_logs

router = APIRouter(tags=["logs"])


@router.get("/logs")
def list_logs(limit: int = Query(default=100, ge=1, le=1000)):
    try:
        return ok({"logs": get_partition_job_logs(limit)})
    except DatabaseError as exc:
        raise_for_domain(exc)
