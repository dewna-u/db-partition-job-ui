from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter

from api.deps import ok
from job_autofill import calculate_next_run, describe_schedule, validate_six_field_cron

router = APIRouter(tags=["cron"])


class CronPreviewBody(BaseModel):
    job_schedule: str


@router.post("/cron/preview")
def cron_preview(body: CronPreviewBody):
    schedule = (body.job_schedule or "").strip()
    error = validate_six_field_cron(schedule)
    if error:
        return ok(
            {
                "valid": False,
                "error": error,
                "next_run": None,
                "description": None,
            }
        )
    next_run = calculate_next_run(schedule)
    return ok(
        {
            "valid": True,
            "error": None,
            "next_run": next_run.isoformat(sep=" ") if next_run else None,
            "description": describe_schedule(schedule),
        }
    )
