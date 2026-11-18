from __future__ import annotations

from fastapi import APIRouter

from api.deps import ok, raise_for_domain
from dashboard_metrics import (
    FAIL_STATUSES,
    average_execution_duration_ms,
    failed_count_recent,
    job_counts,
    next_execution,
    scheduler_uptime,
    success_rate,
    system_insights,
)
from database import get_dashboard_snapshot
from scheduler_client import fetch_scheduler_status

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def dashboard_summary():
    # Phase 5F.1: jobs + recent history + privilege readiness on ONE
    # short-lived connection (was three); each section fails independently.
    snapshot = get_dashboard_snapshot(200)
    jobs = snapshot["jobs"] or []
    logs = snapshot["logs"] or []
    readiness = snapshot["readiness"]
    jobs_error = snapshot["jobs_error"]
    logs_error = snapshot["logs_error"]
    readiness_error = snapshot["readiness_error"]

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
    avg_duration = average_execution_duration_ms(logs)

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
            "average_duration": avg_duration,
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
