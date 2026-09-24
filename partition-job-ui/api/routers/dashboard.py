from __future__ import annotations

from fastapi import APIRouter

from api.deps import ok, raise_for_domain
from dashboard_metrics import (
    FAIL_STATUSES,
    failed_count_recent,
    job_counts,
    next_execution,
    scheduler_uptime,
    success_rate,
    system_insights,
)
from database import DatabaseError, get_database_readiness, get_partition_job_logs, get_partition_jobs
from scheduler_client import fetch_scheduler_status

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def dashboard_summary():
    jobs: list = []
    logs: list = []
    readiness = None
    readiness_error = None
    jobs_error = None
    logs_error = None

    try:
        jobs = get_partition_jobs()
    except DatabaseError as exc:
        jobs_error = exc.message
    try:
        logs = get_partition_job_logs(200)
    except DatabaseError as exc:
        logs_error = exc.message
    try:
        readiness = get_database_readiness()
    except DatabaseError as exc:
        readiness_error = exc.message

    scheduler_ok, status, scheduler_message = fetch_scheduler_status()
    counts = job_counts(jobs)
    nxt = next_execution(jobs)
    rate = success_rate(logs)
    uptime = scheduler_uptime(status if scheduler_ok else None)
    fails = sum(
        1
        for row in logs
        if str(row.get("last_run_status") or "").upper() in FAIL_STATUSES
    )
    fails_24h = failed_count_recent(logs, hours=24)
    insights = system_insights(jobs, logs, scheduler_ok=scheduler_ok, status=status)

    return ok(
        {
            "jobs": jobs,
            "logs": logs[:25],
            "counts": counts,
            "next_execution": nxt,
            "success_rate": rate,
            "scheduler_uptime": uptime,
            "failed_executions": fails,
            "failed_last_24h": fails_24h,
            "insights": insights,
            "scheduler": {
                "ok": scheduler_ok,
                "status": status,
                "message": scheduler_message,
            },
            "readiness": readiness,
            "readiness_error": readiness_error,
            "jobs_error": jobs_error,
            "logs_error": logs_error,
        }
    )
