"""Read-only dashboard metrics derived from jobs, logs, and scheduler status.

No writes. No scheduler/job execution side effects.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


SUCCESS_STATUSES = frozenset({"SUCCESS", "MANUAL_SUCCESS"})
FAIL_STATUSES = frozenset({"FAIL", "MANUAL_FAIL", "ERROR", "FAILED", "FAILED_CONNECTION"})


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", ""))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_countdown(target: Any, *, now: Optional[datetime] = None) -> str:
    """Human-friendly relative time (e.g. 12m, 01h 08m, Overdue)."""
    dt = _as_datetime(target)
    if dt is None:
        return "—"
    current = now or _now()
    secs = int((dt - current).total_seconds())
    if secs < 0:
        overdue = abs(secs)
        if overdue < 60:
            return f"Overdue {overdue}s"
        if overdue < 3600:
            return f"Overdue {overdue // 60}m"
        return f"Overdue {overdue // 3600}h"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        hours = secs // 3600
        mins = (secs % 3600) // 60
        return f"{hours:02d}h {mins:02d}m"
    days = secs // 86400
    hours = (secs % 86400) // 3600
    return f"{days}d {hours:02d}h"


def format_uptime(started_at: Any, *, now: Optional[datetime] = None) -> str:
    """Human-friendly uptime from backend started_at."""
    started = _as_datetime(started_at)
    if started is None:
        return "—"
    current = now or _now()
    secs = max(0, int((current - started).total_seconds()))
    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins:02d}m"
    if mins > 0:
        return f"{mins}m"
    return f"{secs}s"


def format_age(moment: Any, *, now: Optional[datetime] = None) -> str:
    """Seconds/minutes ago from a timestamp."""
    dt = _as_datetime(moment)
    if dt is None:
        return "—"
    current = now or _now()
    secs = max(0, int((current - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def target_table(job: dict[str, Any]) -> str:
    schema = job.get("table_schema") or ""
    table = job.get("table_name") or ""
    if schema and table:
        return f"{schema}.{table}"
    return table or schema or "—"


def operation_label(job: dict[str, Any]) -> str:
    return "CREATE" if bool(job.get("is_create")) else "DROP"


def display_status(job: dict[str, Any], *, now: Optional[datetime] = None) -> tuple[str, str]:
    """
    Return (label, tone) for queue/status badges.

    tone: green | blue | amber | mute | red
    """
    if not job.get("is_enabled"):
        return "Disabled", "mute"
    status = str(job.get("last_run_status") or "").upper()
    next_run = _as_datetime(job.get("next_run_time"))
    current = now or _now()
    if status in FAIL_STATUSES:
        return "Failed", "red"
    if next_run is not None and next_run < current - timedelta(minutes=5):
        return "Needs review", "amber"
    if next_run is not None and next_run >= current:
        return "Scheduled", "blue"
    if status in SUCCESS_STATUSES:
        return "Completed", "green"
    if next_run is None:
        return "Paused", "amber"
    return status or "Unknown", "mute"


def next_run_label(job: dict[str, Any], *, now: Optional[datetime] = None) -> str:
    if not job.get("is_enabled"):
        return "Paused"
    next_run = job.get("next_run_time")
    if next_run is None:
        return "—"
    return format_countdown(next_run, now=now)


def job_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    enabled = sum(1 for j in jobs if j.get("is_enabled"))
    create = sum(1 for j in jobs if j.get("is_create"))
    return {
        "total": len(jobs),
        "enabled": enabled,
        "disabled": len(jobs) - enabled,
        "create": create,
        "drop": len(jobs) - create,
    }


def next_execution(jobs: list[dict[str, Any]], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Earliest enabled next_run_time among configured jobs."""
    current = now or _now()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for job in jobs:
        if not job.get("is_enabled"):
            continue
        dt = _as_datetime(job.get("next_run_time"))
        if dt is None:
            continue
        candidates.append((dt, job))
    if not candidates:
        return {
            "available": False,
            "countdown": "—",
            "detail": "No upcoming jobs",
            "job_id": None,
            "job_name": None,
            "operation": None,
            "next_run_time": None,
        }
    candidates.sort(key=lambda item: (item[0], item[1].get("job_id") or 0))
    when, job = candidates[0]
    return {
        "available": True,
        "countdown": format_countdown(when, now=current),
        "detail": f"Job #{job.get('job_id')} · {operation_label(job)}",
        "job_id": job.get("job_id"),
        "job_name": job.get("job_name"),
        "operation": operation_label(job),
        "next_run_time": when,
        "absolute": when.strftime("%Y-%m-%d %H:%M:%S"),
    }


def success_rate(
    logs: list[dict[str, Any]],
    *,
    days: int = 30,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Success rate over the last N days from partitioning_job_table_log.

    Uses job_runtime as the event timestamp (existing schema).
    Completed = success + fail statuses known to the app.
    """
    current = now or _now()
    cutoff = current - timedelta(days=max(1, days))
    success = 0
    failed = 0
    for row in logs:
        when = _as_datetime(row.get("job_runtime"))
        if when is not None and when < cutoff:
            continue
        status = str(row.get("last_run_status") or "").upper()
        if status in SUCCESS_STATUSES:
            success += 1
        elif status in FAIL_STATUSES:
            failed += 1
    total = success + failed
    if total == 0:
        return {
            "available": False,
            "rate": None,
            "label": "—",
            "detail": "No executions yet",
            "success": 0,
            "failed": 0,
            "total": 0,
        }
    rate = round(100.0 * success / total, 1)
    return {
        "available": True,
        "rate": rate,
        "label": f"{rate}%",
        "detail": f"Last {days} days",
        "success": success,
        "failed": failed,
        "total": total,
    }


def failed_count_recent(
    logs: list[dict[str, Any]],
    *,
    hours: int = 24,
    now: Optional[datetime] = None,
) -> int:
    current = now or _now()
    cutoff = current - timedelta(hours=max(1, hours))
    count = 0
    for row in logs:
        when = _as_datetime(row.get("job_runtime"))
        if when is not None and when < cutoff:
            continue
        if str(row.get("last_run_status") or "").upper() in FAIL_STATUSES:
            count += 1
    return count


def overdue_jobs(jobs: list[dict[str, Any]], *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    current = now or _now()
    overdue: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("is_enabled"):
            continue
        dt = _as_datetime(job.get("next_run_time"))
        if dt is not None and dt < current:
            overdue.append(job)
    return overdue


def scheduler_uptime(status: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not status:
        return {"available": False, "label": "Unavailable", "detail": "Backend unreachable"}
    if not status.get("scheduler_active"):
        started = status.get("started_at")
        if started:
            return {
                "available": True,
                "label": format_uptime(started),
                "detail": "Backend idle",
                "started_at": started,
            }
        return {"available": False, "label": "Unavailable", "detail": "Scheduler idle"}
    started = status.get("started_at")
    if not started:
        return {"available": False, "label": "—", "detail": "Start time not reported"}
    return {
        "available": True,
        "label": format_uptime(started),
        "detail": "Running continuously",
        "started_at": started,
    }


def system_insights(
    jobs: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    *,
    scheduler_ok: bool,
    status: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Deterministic operational insights from real data only."""
    insights: list[str] = []
    if not scheduler_ok:
        insights.append(
            "The realtime scheduler backend is currently unavailable."
        )
    fails = failed_count_recent(logs, hours=24)
    if fails:
        insights.append(
            f"{fails} partition job{'s' if fails != 1 else ''} failed during the last 24 hours. "
            "Review execution history before the next scheduled run."
        )
    overdue = overdue_jobs(jobs)
    if overdue:
        insights.append(
            f"{len(overdue)} enabled job{'s' if len(overdue) != 1 else ''} "
            "have passed their expected next execution time."
        )
    disabled = sum(1 for j in jobs if not j.get("is_enabled"))
    if disabled:
        insights.append(
            f"{disabled} configured job{'s' if disabled != 1 else ''} "
            "are currently disabled."
        )
    if not insights:
        if status and status.get("scheduler_active"):
            insights.append("All enabled partition jobs are currently scheduled normally.")
        elif jobs:
            insights.append("Configured jobs are loaded. Monitor the scheduler heartbeat for live timing.")
        else:
            insights.append("No parameterized partition jobs are configured yet.")
    return insights[:3]


def jobs_to_csv(jobs: list[dict[str, Any]]) -> str:
    """CSV export of configured jobs (read-only)."""
    fields = [
        "job_id",
        "job_name",
        "is_enabled",
        "operation",
        "table_schema",
        "table_name",
        "job_schedule",
        "frequency",
        "next_run_time",
        "last_run_time",
        "last_run_status",
        "partition_unit",
        "partition_period",
        "create_drop_interval",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for job in jobs:
        row = {key: job.get(key) for key in fields}
        row["operation"] = operation_label(job)
        for key in ("next_run_time", "last_run_time"):
            value = row.get(key)
            if isinstance(value, datetime):
                row[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow(row)
    return buffer.getvalue()
