"""Realtime scheduler loop: timers, refresh signal, reconciliation, execution."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional, Tuple

from scheduler_backend.models import SchedulerConfig, SchedulerStatus
from scheduler_backend.queue_manager import ScheduleQueue
from scheduler_backend.scheduler_database import (
    execute_scheduled_job,
    fetch_upcoming_jobs,
)

logger = logging.getLogger(__name__)

# Rapid-loop breaker: cache only when the DB committed a transition of this
# occurrence. Ordinary skips leave the DB authoritative (do not cache).
# NOT cached: FAILED_CONNECTION, SKIPPED_*, NOT_FOUND, connection exceptions.
_HANDLED_OCCURRENCE_STATUSES = frozenset(
    {
        "EXECUTED",  # worker ran; next_run_time advanced
        "FAILED",  # worker failed but occurrence still advanced/logged in DB
        "FAILED_INVALID_SCHEDULE",  # fail-closed: next_run_time cleared to NULL
    }
)

# Secondary rapid-loop breaker bounds (process-local only; DB is source of truth).
_CIRCUIT_BREAKER_MAX_ENTRIES = 512
_CIRCUIT_BREAKER_TTL_SECONDS = 300.0


class RecentOccurrenceCache:
    """Short-lived bounded cache of recently transitioned occurrences.

    Rapid-loop circuit breaker only. Does not persist. Not a scheduling authority.
    """

    def __init__(
        self,
        max_entries: int = _CIRCUIT_BREAKER_MAX_ENTRIES,
        ttl_seconds: float = _CIRCUIT_BREAKER_TTL_SECONDS,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._items: "OrderedDict[Tuple[int, datetime], Tuple[float, str]]" = (
            OrderedDict()
        )

    def __len__(self) -> int:
        return len(self._items)

    def prune(self, now_mono: Optional[float] = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        expired = [
            key
            for key, (ts, _status) in self._items.items()
            if (now - ts) > self._ttl_seconds
        ]
        for key in expired:
            del self._items[key]
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)

    def contains(
        self, key: Tuple[int, datetime], now_mono: Optional[float] = None
    ) -> bool:
        self.prune(now_mono)
        return key in self._items

    def get_status(
        self, key: Tuple[int, datetime], now_mono: Optional[float] = None
    ) -> Optional[str]:
        self.prune(now_mono)
        item = self._items.get(key)
        if item is None:
            return None
        return item[1]

    def add(
        self,
        key: Tuple[int, datetime],
        status: str,
        now_mono: Optional[float] = None,
    ) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        self.prune(now)
        if key in self._items:
            del self._items[key]
        self._items[key] = (now, status)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)


class PartitionScheduler:
    """Persistent process logic with short-lived DB connections only."""

    def __init__(self, config: SchedulerConfig) -> None:
        self.config = config
        self.queue = ScheduleQueue()
        self.status = SchedulerStatus()
        self.refresh_event = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self._backoff = float(config.connect_retry_seconds)
        self._handled_occurrences = RecentOccurrenceCache()

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

        filtered = []
        for job in jobs:
            key = (job.job_id, job.expected_run_time)
            prior = self._handled_occurrences.get_status(key)
            if prior is not None:
                logger.critical(
                    "Rapid-loop breaker: excluding rediscovered occurrence "
                    "(local secondary cache only; DB remains authoritative) "
                    "job_id=%s expected_run_time=%s prior_result=%s refresh=%s",
                    job.job_id,
                    job.expected_run_time,
                    prior,
                    reason,
                )
                continue
            filtered.append(job)

        self.queue.replace_from_jobs(filtered)
        self._update_status_from_queue()
        self.status.last_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.last_refresh_result = f"ok:{reason}:{len(filtered)}"
        self.status.extra["circuit_breaker_entries"] = len(self._handled_occurrences)
        self._backoff = float(self.config.connect_retry_seconds)
        logger.info(
            "Queue refreshed (%s): %s upcoming job(s); next=%s @ %s",
            reason,
            len(filtered),
            self.status.next_job_id,
            self.status.next_expected_run_time,
        )
        return True

    async def _execute_one(self, job_id: int, expected: datetime) -> None:
        key = (job_id, expected)
        prior = self._handled_occurrences.get_status(key)
        if prior is not None:
            logger.critical(
                "Rapid-loop breaker: refusing duplicate occurrence "
                "(no worker, no DB mutation; local secondary cache only) "
                "job_id=%s expected_run_time=%s prior_result=%s",
                job_id,
                expected,
                prior,
            )
            self.status.last_execution_job_id = job_id
            self.status.last_execution_result = "SKIPPED_CIRCUIT_BREAKER"
            return

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
                "Scheduled execution connection/runtime failure for job_id=%s "
                "expected_run_time=%s",
                job_id,
                expected,
            )
            self.status.last_execution_job_id = job_id
            self.status.last_execution_result = "FAILED_CONNECTION"
            self._backoff = min(
                self._backoff * 2.0,
                float(self.config.max_connect_retry_seconds),
            )
            # Do not cache: transaction likely rolled back; occurrence still open.
            return

        status = str(result.get("status") or "FAILED")
        new_next = result.get("next_run_time")
        self.status.last_execution_job_id = job_id
        self.status.last_execution_result = status
        logger.info(
            "Scheduled job %s result %s (%s) expected_run_time=%s "
            "returned_next_run_time=%s",
            job_id,
            status,
            result.get("message"),
            expected,
            new_next,
        )

        if status in _HANDLED_OCCURRENCE_STATUSES:
            self._handled_occurrences.add(key, status)
            if status in ("EXECUTED", "FAILED") and new_next == expected:
                logger.critical(
                    "Occurrence did not advance after %s: job_id=%s "
                    "expected_run_time=%s returned_next_run_time=%s "
                    "(DB function/migration may be stale)",
                    status,
                    job_id,
                    expected,
                    new_next,
                )

    async def run(self) -> None:
        self.status.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.scheduler_active = True
        self.status.extra["lookahead_seconds"] = float(self.config.lookahead_seconds)
        self.status.extra["reconcile_seconds"] = float(self.config.reconcile_seconds)
        self.status.extra["bind"] = f"{self.config.bind_host}:{self.config.bind_port}"
        await self.refresh_queue("startup")

        next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)

        while not self.shutdown_event.is_set():
            now = time.monotonic()

            while True:
                due = self.queue.pop_due(now)
                if due is None:
                    break
                job_id, expected = due
                await self._execute_one(job_id, expected)
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
            if self.status.last_refresh_result.startswith("error:"):
                wait_candidates.append(self._backoff)

            timeout = max(0.05, min(wait_candidates))
            try:
                await asyncio.wait_for(self.refresh_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        self.status.scheduler_active = False
        logger.info("Scheduler loop stopped")
