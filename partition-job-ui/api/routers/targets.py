"""Phase 5C — hub-local database targets (read-only API).

GET /api/targets                 registry (control DB) — no secrets
GET /api/targets/{id}            one target
GET /api/targets/{id}/health     READ-ONLY health of that target only

Registry writes are DBA-only in Phase 5C (the API has no authentication, so
create/edit endpoints are deliberately not exposed). Responses never contain
passwords, secret values or secret identifiers (only ``secret_configured``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.deps import ok, raise_for_domain
from database import DatabaseError, get_database_target, list_database_targets
from database_targets import check_target_health
from observability import emit_target_health

router = APIRouter(tags=["targets"])


@router.get("/targets")
def list_targets():
    try:
        targets, invalid, available = list_database_targets()
    except DatabaseError as exc:
        raise_for_domain(exc)
    return ok(
        {
            "targets": [t.to_public_dict() for t in targets],
            "invalid_targets": invalid,
            "available": available,
            "message": None
            if available
            else "Database targets require migration 20261117_01 (single-database mode).",
        }
    )


def _load(target_id: int):
    try:
        target = get_database_target(target_id)
    except DatabaseError as exc:
        raise_for_domain(exc)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Database target {target_id} not found.")
    return target


@router.get("/targets/{target_id}")
def get_target(target_id: int):
    return ok({"target": _load(target_id).to_public_dict()})


@router.get("/targets/{target_id}/health")
def target_health(target_id: int):
    target = _load(target_id)
    health = check_target_health(target)
    emit_target_health(target=target.target_key, state=health.get("state"))
    return ok({"target": target.to_public_dict(), "health": health})
