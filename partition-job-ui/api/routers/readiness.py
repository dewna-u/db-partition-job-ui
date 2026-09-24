from __future__ import annotations

from fastapi import APIRouter

from api.deps import ok, raise_for_domain
from database import DatabaseError, get_database_readiness

router = APIRouter(tags=["readiness"])


@router.get("/readiness")
def readiness():
    try:
        return ok({"readiness": get_database_readiness()})
    except DatabaseError as exc:
        raise_for_domain(exc)
