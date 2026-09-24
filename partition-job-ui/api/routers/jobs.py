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
)
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


class ManualRunBody(BaseModel):
    confirm_drop: bool = False


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


@router.post("/jobs/{job_id}/run")
def run_job(job_id: int, body: ManualRunBody | None = None):
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
