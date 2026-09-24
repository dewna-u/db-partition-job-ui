from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from api.deps import ok, raise_for_domain
from database import DatabaseError, PgAgentNotInstalledError, get_pgagent_job_details, get_pgagent_jobs

router = APIRouter(tags=["pgagent"])


@router.get("/pgagent/jobs")
def list_pgagent_jobs():
    try:
        return ok({"jobs": get_pgagent_jobs()})
    except PgAgentNotInstalledError as exc:
        raise_for_domain(exc)
    except DatabaseError as exc:
        raise_for_domain(exc)


@router.get("/pgagent/jobs/{job_id}")
def pgagent_job_details(job_id: int, step_id: Optional[int] = Query(default=None)):
    try:
        return ok({"details": get_pgagent_job_details(job_id, step_id=step_id)})
    except PgAgentNotInstalledError as exc:
        raise_for_domain(exc)
    except DatabaseError as exc:
        raise_for_domain(exc)
