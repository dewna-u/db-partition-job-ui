"""Typed models for the realtime scheduler backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class ScheduledJob:
    """Minimum identity held in memory for one upcoming occurrence."""

    job_id: int
    expected_run_time: datetime
    delay_seconds: float
    job_name: Optional[str] = None
    is_create: Optional[bool] = None
    # Phase 5C/5D: managed database target of this occurrence. Discovery
    # (partops_get_upcoming_jobs) returns it for every job of the hub.
    database_target_id: Optional[int] = None
    target_key: Optional[str] = None
    is_control_target: Optional[bool] = None

    @property
    def key(self) -> tuple[int, datetime]:
        """In-process bookkeeping key. job_id is unique in the CONTROL database
        across all targets, so (job_id, expected) identifies an occurrence."""
        return (self.job_id, self.expected_run_time)

    @property
    def occurrence_id(self) -> Tuple[Optional[int], int, datetime]:
        """Conceptual occurrence identity (target_id, job_id, expected_run_time)."""
        return (self.database_target_id, self.job_id, self.expected_run_time)


def target_table_resource_key(
    target_id: Optional[int], table_schema: Optional[str], table_name: Optional[str]
) -> Tuple[Optional[int], str, str]:
    """Central identity of a partitioned table across managed targets.

    ``db01.public.orders`` and ``db02.public.orders`` are different resources.
    PostgreSQL advisory locks (namespace 87421032) are per database and stay
    unchanged; this key is for central (Python) bookkeeping in Phase 5D.
    """
    return (target_id, (table_schema or "").lower(), (table_name or "").lower())


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
    # Phase 2: max simultaneous scheduled DB executions (default serial = 1).
    max_concurrent_jobs: int = 1
    schema: str = "mubasher_oms"
    upcoming_function: str = "get_upcoming_partition_jobs"
    scheduled_run_function: str = "run_partition_job_scheduled"
    # Phase 5A: missed-run ownership + bounded per-occurrence retry (seconds).
    # First sighting later than this past its scheduled time → HISTORICAL
    # (reconciled as missed, never executed). Covers timer/refresh jitter only.
    overdue_tolerance_seconds: float = 60.0
    # DB/connection failures: total execution attempts; delays between them
    # (last value repeats).
    retry_max_attempts: int = 4
    retry_delays: Tuple[float, ...] = (15.0, 60.0, 180.0)
    # Target table busy: cooldowns (last repeats) and total wait budget.
    target_lock_cooldowns: Tuple[float, ...] = (5.0, 10.0, 20.0, 30.0)
    target_lock_max_wait_seconds: float = 3600.0
    exhausted_retry_seconds: float = 60.0
    # Phase 5B: refuse to dispatch until the DB framework passes the manifest
    # gate (required execution functions, grants, framework version).
    # False only for directly constructed test configs; load_scheduler_config()
    # always enables it.
    framework_check_enabled: bool = False
    framework_recheck_seconds: float = 300.0
    # Phase 5D: target-aware discovery across all enabled targets of the hub.
    upcoming_targets_function: str = "partops_get_upcoming_jobs"
    # Remote targets: capped exponential availability cooldown and the TTL of
    # the cached per-target framework preflight.
    target_cooldown_base_seconds: float = 15.0
    target_cooldown_max_seconds: float = 300.0
    target_preflight_ttl_seconds: float = 300.0
    # Manual remote runs wait at most this long for a target/global slot.
    manual_slot_timeout_seconds: float = 60.0
    reconcile_function: str = "reconcile_missed_partition_job"
    attention_table: str = "partition_job_attention"


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
    max_concurrent_jobs: int = 1
    active_job_count: int = 0
    queued_job_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        available = max(0, int(self.max_concurrent_jobs) - int(self.active_job_count))
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
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "active_job_count": self.active_job_count,
            "queued_job_count": self.queued_job_count,
            "available_slots": available,
            **self.extra,
        }
