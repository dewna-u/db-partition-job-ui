from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api.deps import ok, raise_for_domain
from dashboard_metrics import jobs_to_csv
from api.remote_manual import is_remote_job, run_remote_manual
from database import (
    DatabaseConflictError,
    DatabaseError,
    list_database_targets,
    create_partition_job,
    get_database_readiness,
    get_job_open_attempt_state,
    get_partition_job,
    get_partition_jobs,
    run_partition_job_manual,
    update_partition_job,
)
from database_targets import HEALTHY, HEALTHY_WITH_WARNINGS, check_target_health
from job_autofill import calculate_next_run
from scheduler_client import notify_scheduler_refresh
from validators import ValidationError, validate_form_data

router = APIRouter(tags=["jobs"])


class JobCreateBody(BaseModel):
    job_name: str
    is_enabled: bool = True
    table_schema: str
    table_name: str
    db_config: str = "{}"
    job_schedule: str
    frequency_amount: int = Field(ge=1)
    frequency_unit: str
    next_run_time: datetime
    partition_unit: str
    partition_period: int = Field(ge=1)
    is_create: bool = True
    create_drop_amount: int = Field(ge=1)
    create_drop_unit: str
    # Phase 5C: managed database target. None = control database (default).
    database_target_id: Optional[int] = Field(default=None, ge=1)


class JobUpdateBody(BaseModel):
    """Explicit editable configuration fields — no arbitrary column updates."""

    job_name: str
    is_enabled: bool = True
    table_schema: str
    table_name: str
    db_config: str = "{}"
    job_schedule: str
    frequency_amount: int = Field(ge=1)
    frequency_unit: str
    next_run_time: Optional[datetime] = None
    partition_unit: str
    partition_period: int = Field(ge=1)
    is_create: bool = True
    create_drop_amount: int = Field(ge=1)
    create_drop_unit: str
    # Phase 5C: None = keep the job's current target.
    database_target_id: Optional[int] = Field(default=None, ge=1)
    confirm_dangerous: bool = False


class ManualRunBody(BaseModel):
    confirm_drop: bool = False


def _as_naive(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    text = str(dt).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", ""))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def resolve_job_target(
    requested: Optional[int],
    current: Optional[int],
    confirm_dangerous: bool,
    *,
    job_id: Optional[int] = None,
) -> Optional[int]:
    """Validate the job's database target (Phase 5C/5D).

    * unknown / misconfigured / disabled target -> 400
    * changing a job's target -> dangerous (confirm_dangerous=true required)
      and refused (409) while the job has an open / unresolved remote
      execution attempt (also enforced atomically in SQL)
    * a managed (non-control) target must pass its read-only health /
      target_execution framework check before jobs are assigned to it (409)

    No target requested => no registry lookup: an edit keeps the job's current
    target and a create gets the control target from the SQL column DEFAULT.
    """
    if requested is None:
        return current
    targets, invalid, available = list_database_targets()
    if not available:
        raise ValidationError(
            "Database targets are not configured on this installation "
            "(migration 20261117_01 not applied)."
        )
    by_id = {t.target_id: t for t in targets}
    if requested not in by_id:
        if any(bad.get("target_id") == requested for bad in invalid):
            raise ValidationError(f"Database target {requested} is misconfigured.")
        raise ValidationError(f"Database target {requested} does not exist.")
    target = by_id[requested]
    if not target.is_enabled:
        raise ValidationError(f"Database target {target.target_key} is disabled.")
    if current is not None and requested != current:
        old_key = by_id[current].target_key if current in by_id else str(current)
        if not confirm_dangerous:
            raise ValidationError(
                "Dangerous configuration change requires confirm_dangerous=true: "
                f"database target changes from {old_key} to {target.target_key}"
            )
        if job_id is not None:
            state = get_job_open_attempt_state(job_id)
            if state:
                raise DatabaseConflictError(
                    f"Job {job_id} has a remote execution attempt in state {state}; "
                    "its database target cannot change until it is finalized or resolved."
                )
    if not target.is_control_database:
        health = check_target_health(target)
        if health.get("state") not in (HEALTHY, HEALTHY_WITH_WARNINGS):
            raise DatabaseConflictError(
                f"Database target {target.target_key} is not ready for execution "
                f"({health.get('state')}): {health.get('detail') or ''}".strip()
            )
    return requested


def _is_control_target(target_id: Optional[int]) -> bool:
    if target_id is None:
        return True
    targets, _invalid, available = list_database_targets()
    if not available:
        return True
    return any(t.target_id == target_id and t.is_control_database for t in targets)


def _prepare_job_update(
    existing: dict[str, Any], body: JobUpdateBody
) -> dict[str, Any]:
    """Validate, enforce dangerous-change confirmation, recalculate next_run_time."""
    raw: dict[str, Any] = body.model_dump()
    # next_run_time may be omitted; seed from existing then recalculate as needed.
    if raw.get("next_run_time") is None:
        raw["next_run_time"] = existing.get("next_run_time") or datetime.now()

    validated = validate_form_data(raw)

    old_is_create = bool(existing.get("is_create"))
    new_is_create = bool(validated["is_create"])
    old_target = (
        f"{existing.get('table_schema')}.{existing.get('table_name')}"
    )
    new_target = f"{validated['table_schema']}.{validated['table_name']}"
    dangerous = (old_is_create != new_is_create) or (old_target != new_target)
    if dangerous and not body.confirm_dangerous:
        reasons = []
        if old_is_create != new_is_create:
            reasons.append(
                f"operation changes from {'CREATE' if old_is_create else 'DROP'} "
                f"to {'CREATE' if new_is_create else 'DROP'}"
            )
        if old_target != new_target:
            reasons.append(f"target changes from {old_target} to {new_target}")
        raise ValidationError(
            "Dangerous configuration change requires confirm_dangerous=true: "
            + "; ".join(reasons)
        )

    # Phase 5C/5D: target change = dangerous, refused while an attempt is open.
    current_target = existing.get("database_target_id")
    new_target = resolve_job_target(
        body.database_target_id,
        current_target,
        body.confirm_dangerous,
        job_id=existing.get("job_id"),
    )

    schedule_changed = str(existing.get("job_schedule") or "") != validated[
        "job_schedule"
    ]
    enabling = (not bool(existing.get("is_enabled"))) and bool(
        validated["is_enabled"]
    )
    next_run = _as_naive(validated["next_run_time"])
    now = datetime.now().replace(microsecond=0)

    if schedule_changed:
        calculated = calculate_next_run(validated["job_schedule"], now=now)
        if calculated is None:
            raise ValidationError(
                "Could not calculate next_run_time for the new schedule."
            )
        validated["next_run_time"] = calculated
    elif enabling and (next_run is None or next_run <= now):
        calculated = calculate_next_run(validated["job_schedule"], now=now)
        if calculated is None:
            raise ValidationError(
                "Could not calculate next_run_time when re-enabling the job."
            )
        validated["next_run_time"] = calculated
    elif next_run is not None and next_run <= now and validated["is_enabled"]:
        # Keep schedules honest when enabling stays true but next is already past.
        calculated = calculate_next_run(validated["job_schedule"], now=now)
        if calculated is not None:
            validated["next_run_time"] = calculated

    if new_target is not None and new_target != current_target:
        validated["database_target_change"] = new_target
    return validated


@router.get("/jobs")
def list_jobs():
    try:
        return ok({"jobs": get_partition_jobs()})
    except DatabaseError as exc:
        raise_for_domain(exc)


@router.get("/jobs/export.csv")
def export_jobs_csv():
    try:
        jobs = get_partition_jobs()
    except DatabaseError as exc:
        raise_for_domain(exc)
    return PlainTextResponse(
        jobs_to_csv(jobs),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=partition_jobs.csv"},
    )


@router.get("/jobs/{job_id}")
def get_job(job_id: int):
    try:
        job = get_partition_job(job_id)
    except DatabaseError as exc:
        raise_for_domain(exc)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
    return ok({"job": job})


@router.post("/jobs")
def create_job(body: JobCreateBody):
    raw: dict[str, Any] = body.model_dump()
    try:
        validated = validate_form_data(raw)
        # Phase 5C/5D: new rows default to the control target in SQL; a
        # managed target is validated (enabled + healthy) before any write.
        target = resolve_job_target(body.database_target_id, None, False)
        validated["database_target_id"] = None if _is_control_target(target) else target
        result = create_partition_job(validated)
    except (ValidationError, DatabaseError) as exc:
        raise_for_domain(exc)

    refresh_ok, refresh_message = notify_scheduler_refresh()
    return ok(
        {
            "result": result,
            "refresh_ok": refresh_ok,
            "refresh_message": refresh_message,
            "message": "Partition configuration created successfully.",
        },
        status_code=201,
    )


@router.patch("/jobs/{job_id}")
def update_job(job_id: int, body: JobUpdateBody):
    try:
        existing = get_partition_job(job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
        validated = _prepare_job_update(existing, body)
        result = update_partition_job(
            job_id, validated, new_target_id=validated.pop("database_target_change", None)
        )
    except HTTPException:
        raise
    except ValidationError as exc:
        raise_for_domain(exc)
    except DatabaseError as exc:
        if "not found" in (exc.message or "").lower():
            raise HTTPException(
                status_code=404, detail=f"Job {job_id} not found."
            ) from exc
        raise_for_domain(exc)

    # Refresh after successful commit/close inside update_partition_job.
    # A refresh failure must not undo the DB update.
    refresh_ok, refresh_message = notify_scheduler_refresh()
    # Re-read so UI shows DB-authoritative next_run_time / is_enabled.
    try:
        updated_job = get_partition_job(job_id)
    except DatabaseError:
        updated_job = None
    return ok(
        {
            "result": result,
            "job_id": job_id,
            "job": updated_job,
            "refresh_ok": refresh_ok,
            "refresh_message": refresh_message,
            "message": "Job updated successfully.",
        }
    )


@router.post("/jobs/{job_id}/run")
def run_job(job_id: int, body: Optional[ManualRunBody] = None):
    body = body or ManualRunBody()
    try:
        readiness = get_database_readiness()
        if not readiness.get("config_table_select"):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Manual run is blocked: configuration table SELECT is missing. "
                    "Ask a DBA to grant only what is needed."
                ),
            )
        job = get_partition_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
        if not bool(job.get("is_create")) and not body.confirm_drop:
            raise HTTPException(
                status_code=400,
                detail="DROP jobs require confirm_drop=true before manual execution.",
            )
        if is_remote_job(job):
            # Phase 5D: managed target → the scheduler's remote engine.
            result = run_remote_manual(job_id, confirm_drop=body.confirm_drop)
            return ok({"result": result, "message": result.get("message") or (
                f"Manual run of job {job_id} completed on target {result.get('target_key')}.")})
        result = run_partition_job_manual(job_id)
    except HTTPException:
        raise
    except DatabaseError as exc:
        raise_for_domain(exc)
    return ok(
        {
            "result": result,
            "message": f"Manual run of job {job_id} completed and committed.",
        }
    )
