from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import ok
from system_readiness import build_system_readiness

router = APIRouter(tags=["system"])


@router.get("/system/readiness")
def system_readiness(refresh: bool = Query(False)):
    return ok(build_system_readiness(force_refresh=refresh))
