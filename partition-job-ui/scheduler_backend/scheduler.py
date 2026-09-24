"""Realtime scheduler loop: timers, refresh signal, reconciliation, execution."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from scheduler_backend.models import SchedulerConfig, SchedulerStatus
from scheduler_backend.queue_manager import ScheduleQueue
from scheduler_backend.scheduler_database import (
    execute_scheduled_job,
    fetch_upcoming_jobs,
)

logger = logging.getLogger(__name__)


class PartitionScheduler:
    """Persistent process logic with short-lived DB connections only."""

    def __init__(self, config: SchedulerConfig) -> None:
        self.config = config
        self.queue = ScheduleQueue()
        self.status = SchedulerStatus()
        self.refresh_event = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self._backoff = float(config.connect_retry_seconds)

    def request_refresh(self) -> None:
        self.refresh_event.set()

    def request_shutdown(self) -> None:
        self.shutdown_event.set()
        self.refresh_event.set()

    def _update_status_from_queue(self) -> None:
        self.status.upcoming_job_count = len(self.queue)
        peeked = self.queue.peek()
        if peeked is None:
            self.status.next_job_id = None
            self.status.next_expected_run_time = None
        else:
            _due, job_id, expected = peeked
            self.status.next_job_id = job_id
            self.status.next_expected_run_time = expected

    async def refresh_queue(self, reason: str) -> bool:
        """
        Open DB → fetch upcoming jobs → close DB → rebuild memory queue.

        Returns True on success, False on connection/query failure.
        """
        try:
            jobs = await asyncio.to_thread(fetch_upcoming_jobs, self.config)
        except Exception:  # noqa: BLE001
            logger.exception("Upcoming-job refresh failed (%s)", reason)
            self.status.last_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.status.last_refresh_result = f"error:{reason}"
            self._backoff = min(
                self._backoff * 2.0,
                float(self.config.max_connect_retry_seconds),
            )
            return False

        self.queue.replace_from_jobs(jobs)
        self._update_status_from_queue()
        self.status.last_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.last_refresh_result = f"ok:{reason}:{len(jobs)}"
        self._backoff = float(self.config.connect_retry_seconds)
        logger.info(
            "Queue refreshed (%s): %s upcoming job(s); next=%s @ %s",
            reason,
            len(jobs),
            self.status.next_job_id,
            self.status.next_expected_run_time,
        )
        return True

    async def _execute_one(self, job_id: int, expected: datetime) -> None:
        logger.info(
            "Timer fired for job_id=%s expected_run_time=%s",
            job_id,
            expected,
        )
        try:
            result = await asyncio.to_thread(
                execute_scheduled_job, self.config, job_id, expected
            )
        except Exception:  # noqa: BLE001 — never kill the scheduler process
            logger.exception(
                "Scheduled execution connection/runtime failure for job_id=%s",
                job_id,
            )
            self.status.last_execution_job_id = job_id
            self.status.last_execution_result = "FAILED_CONNECTION"
            self._backoff = min(
                self._backoff * 2.0,
                float(self.config.max_connect_retry_seconds),
            )
            return

        status = str(result.get("status") or "FAILED")
        self.status.last_execution_job_id = job_id
        self.status.last_execution_result = status
        logger.info(
            "Scheduled job %s result %s (%s)",
            job_id,
            status,
            result.get("message"),
        )

    async def run(self) -> None:
        self.status.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.scheduler_active = True
        # Read-only status metadata for the UI (does not affect scheduling).
        self.status.extra["lookahead_seconds"] = float(self.config.lookahead_seconds)
        self.status.extra["reconcile_seconds"] = float(self.config.reconcile_seconds)
        self.status.extra["bind"] = f"{self.config.bind_host}:{self.config.bind_port}"
        await self.refresh_queue("startup")

        next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)

        while not self.shutdown_event.is_set():
            now = time.monotonic()

            # Execute all currently due jobs sequentially (deterministic order).
            while True:
                due = self.queue.pop_due(now)
                if due is None:
                    break
                job_id, expected = due
                await self._execute_one(job_id, expected)
                # After each execution, refresh so the new next_run_time is loaded.
                await self.refresh_queue("post-execution")
                now = time.monotonic()
                next_reconcile = now + float(self.config.reconcile_seconds)

            if self.refresh_event.is_set():
                self.refresh_event.clear()
                if self.shutdown_event.is_set():
                    break
                await self.refresh_queue("http-refresh")
                next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)
                continue

            if now >= next_reconcile:
                await self.refresh_queue("periodic-reconcile")
                next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)
                continue

            peeked = self.queue.peek()
            wait_candidates = [next_reconcile - now, float(self.config.reconcile_seconds)]
            if peeked is not None:
                wait_candidates.append(max(0.0, peeked[0] - now))
            # If DB was unavailable, wait at least the backoff interval.
            if self.status.last_refresh_result.startswith("error:"):
                wait_candidates.append(self._backoff)

            timeout = max(0.05, min(wait_candidates))
            try:
                await asyncio.wait_for(self.refresh_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        self.status.scheduler_active = False
        logger.info("Scheduler loop stopped")
