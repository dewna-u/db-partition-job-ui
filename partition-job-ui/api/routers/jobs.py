from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api.deps import ok, raise_for_domain
from dashboard_metrics import jobs_to_csv
from database import (
    DatabaseError,
    create_partition_job,
    get_database_readiness,
    get_partition_job,
    get_partition_jobs,
    run_partition_job_manual,
    update_partition_job,
)
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
        result = update_partition_job(job_id, validated)
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
    return ok(
        {
            "result": result,
            "job_id": job_id,
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
