from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import ok, raise_for_domain
from database import DatabaseError, get_partition_job_logs, list_database_targets

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
def list_logs(limit: int = Query(default=100, ge=1, le=1000)):
    try:
        logs = get_partition_job_logs(limit)
    except DatabaseError as exc:
        raise_for_domain(exc)
    keys, control = _target_keys()
    for row in logs:
        row.setdefault("database_target_id", None)
        row.setdefault("execution_attempt_id", None)
        target_id = row.get("database_target_id")
        # NULL = control database (local executors and every pre-5D row).
        row["target_key"] = keys.get(target_id) if target_id is not None else control
    return ok({"logs": logs})
