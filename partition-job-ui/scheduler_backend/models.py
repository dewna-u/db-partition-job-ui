"""Typed models for the realtime scheduler backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class ScheduledJob:
    """Minimum identity held in memory for one upcoming occurrence."""

    job_id: int
    expected_run_time: datetime
    delay_seconds: float
    job_name: Optional[str] = None
    is_create: Optional[bool] = None

    @property
    def key(self) -> tuple[int, datetime]:
        return (self.job_id, self.expected_run_time)


@dataclass
class SchedulerConfig:
    """Runtime configuration loaded from environment (no secrets logged)."""

    lookahead_seconds: float = 120.0
    reconcile_seconds: float = 30.0
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    lock_file: str = "/tmp/partition-job-scheduler.lock"
    connect_retry_seconds: float = 5.0
    max_connect_retry_seconds: float = 60.0
    schema: str = "mubasher_oms"
    upcoming_function: str = "get_upcoming_partition_jobs"
    scheduled_run_function: str = "run_partition_job_scheduled"


@dataclass
class SchedulerStatus:
    """In-memory operational status (no DB connection required to read)."""

    started_at: Optional[datetime] = None
    last_refresh_at: Optional[datetime] = None
    last_refresh_result: str = "never"
    upcoming_job_count: int = 0
    next_job_id: Optional[int] = None
    next_expected_run_time: Optional[datetime] = None
    last_execution_job_id: Optional[int] = None
    last_execution_result: Optional[str] = None
    scheduler_active: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(sep=" ") if self.started_at else None,
            "last_refresh_at": (
                self.last_refresh_at.isoformat(sep=" ") if self.last_refresh_at else None
            ),
            "last_refresh_result": self.last_refresh_result,
            "upcoming_job_count": self.upcoming_job_count,
            "next_job_id": self.next_job_id,
            "next_expected_run_time": (
                self.next_expected_run_time.isoformat(sep=" ")
                if self.next_expected_run_time
                else None
            ),
            "last_execution_job_id": self.last_execution_job_id,
            "last_execution_result": self.last_execution_result,
            "scheduler_active": self.scheduler_active,
            **self.extra,
        }
