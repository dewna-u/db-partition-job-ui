"""Realtime scheduler loop: timers, refresh signal, reconciliation, execution."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional, Set, Tuple

from observability import (
    emit_job_execution,
    emit_scheduler_concurrency,
    emit_scheduler_execution_exception,
    emit_scheduler_heartbeat,
    emit_scheduler_refresh_failure,
)
from scheduler_backend.models import SchedulerConfig, SchedulerStatus
from scheduler_backend.queue_manager import ScheduleQueue
from scheduler_backend.scheduler_database import (
    execute_scheduled_job,
    fetch_upcoming_jobs,
)

logger = logging.getLogger(__name__)

# Rapid-loop breaker: cache only when the DB committed a transition of this
# occurrence (SQL returned status AND surrounding conn.transaction() committed).
# NOT cached: FAILED_DATABASE, FAILED_CONNECTION, SKIPPED_*, NOT_FOUND,
# SKIPPED_TARGET_LOCKED, connection/transaction exceptions.
_HANDLED_OCCURRENCE_STATUSES = frozenset(
    {
        "EXECUTED",  # worker ran; next_run_time advanced; txn committed
        "FAILED",  # SQL-handled worker failure; occurrence advanced; txn committed
        "FAILED_INVALID_SCHEDULE",  # fail-closed next_run_time=NULL; txn committed
    }
)

# Secondary rapid-loop breaker bounds (process-local only; DB is source of truth).
_CIRCUIT_BREAKER_MAX_ENTRIES = 512
_CIRCUIT_BREAKER_TTL_SECONDS = 300.0

# Bounded wait for in-flight workers during shutdown (seconds).
_SHUTDOWN_DRAIN_SECONDS = 120.0


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
        self._exec_semaphore = asyncio.Semaphore(int(config.max_concurrent_jobs))
        self._active_tasks: Set[asyncio.Task] = set()
        self._active_keys: Set[Tuple[int, datetime]] = set()
        self._active_count = 0
        self.status.max_concurrent_jobs = int(config.max_concurrent_jobs)
        self._update_concurrency_status()

    def request_refresh(self) -> None:
        """Coalesced refresh signal (multiple completions → one refresh)."""
        self.refresh_event.set()

    def request_shutdown(self) -> None:
        self.shutdown_event.set()
        self.refresh_event.set()

    def _update_status_from_queue(self) -> None:
        self.status.upcoming_job_count = len(self.queue)
        self.status.queued_job_count = len(self.queue)
        peeked = self.queue.peek()
        if peeked is None:
            self.status.next_job_id = None
            self.status.next_expected_run_time = None
        else:
            _due, job_id, expected = peeked
            self.status.next_job_id = job_id
            self.status.next_expected_run_time = expected
        self._update_concurrency_status()

    def _update_concurrency_status(self) -> None:
        self.status.max_concurrent_jobs = int(self.config.max_concurrent_jobs)
        self.status.active_job_count = int(self._active_count)
        self.status.queued_job_count = len(self.queue)
        available = max(
            0, int(self.config.max_concurrent_jobs) - int(self._active_count)
        )
        self.status.extra["available_slots"] = available
        if self._active_count > self.config.max_concurrent_jobs:
            logger.critical(
                "Invariant violated: active_job_count=%s > max_concurrent_jobs=%s",
                self._active_count,
                self.config.max_concurrent_jobs,
            )
        overdue = self.status.extra.get("overdue_jobs")
        emit_scheduler_concurrency(
            active_jobs=self.status.active_job_count,
            max_concurrent_jobs=self.status.max_concurrent_jobs,
            queue_depth=self.status.queued_job_count,
            available_slots=available,
            overdue_jobs=int(overdue) if overdue is not None else None,
        )

    async def refresh_queue(self, reason: str) -> bool:
        """
        Open DB → fetch upcoming jobs → close DB → rebuild memory queue.

        Returns True on success, False on connection/query failure.
        """
        try:
            jobs = await asyncio.to_thread(fetch_upcoming_jobs, self.config)
        except Exception:  # noqa: BLE001
            logger.exception(
                "event=scheduler_refresh_failed reason=%s",
                reason,
            )
            self.status.last_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.status.last_refresh_result = f"error:{reason}"
            self._backoff = min(
                self._backoff * 2.0,
                float(self.config.max_connect_retry_seconds),
            )
            emit_scheduler_refresh_failure()
            return False

        filtered = []
        overdue_count = 0
        for job in jobs:
            if float(job.delay_seconds) <= 0.0:
                overdue_count += 1
            key = (job.job_id, job.expected_run_time)
            if key in self._active_keys:
                # Already in-flight (queued on semaphore or executing).
                continue
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
        self.status.extra["overdue_jobs"] = overdue_count
        self._update_status_from_queue()
        self.status.last_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.last_refresh_result = f"ok:{reason}:{len(filtered)}"
        self.status.extra["circuit_breaker_entries"] = len(self._handled_occurrences)
        self._backoff = float(self.config.connect_retry_seconds)
        # Heartbeat on successful reconcile/refresh (existing cycle; no extra loop).
        emit_scheduler_heartbeat()
        logger.info(
            "event=scheduler_refresh_ok reason=%s upcoming=%s overdue=%s "
            "next_job_id=%s next_expected_run_time=%s active=%s max=%s",
            reason,
            len(filtered),
            overdue_count,
            self.status.next_job_id,
            self.status.next_expected_run_time,
            self._active_count,
            self.config.max_concurrent_jobs,
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
            "event=scheduled_job_timer job_id=%s expected_run_time=%s",
            job_id,
            expected,
        )
        try:
            # Blocking psycopg work must not run on the event loop.
            result = await asyncio.to_thread(
                execute_scheduled_job, self.config, job_id, expected
            )
        except Exception:  # noqa: BLE001 — never kill the scheduler process
            logger.exception(
                "event=scheduled_job_exception job_id=%s expected_run_time=%s",
                job_id,
                expected,
            )
            self.status.last_execution_job_id = job_id
            self.status.last_execution_result = "FAILED_CONNECTION"
            self._backoff = min(
                self._backoff * 2.0,
                float(self.config.max_connect_retry_seconds),
            )
            emit_scheduler_execution_exception()
            emit_job_execution(status="FAILED_CONNECTION", is_create=None)
            # Do not cache: transaction likely rolled back; occurrence still open.
            return

        status = str(result.get("status") or "FAILED")
        new_next = result.get("next_run_time")
        is_create = result.get("is_create")
        # Only DB-provided execution_duration_ms (never semaphore wait).
        duration_ms = result.get("execution_duration_ms")
        self.status.last_execution_job_id = job_id
        self.status.last_execution_result = status
        logger.info(
            "event=scheduled_job_complete job_id=%s status=%s "
            "expected_run_time=%s returned_next_run_time=%s duration_ms=%s",
            job_id,
            status,
            expected,
            new_next,
            duration_ms,
        )
        emit_job_execution(
            status=status, is_create=is_create, duration_ms=duration_ms
        )
        if status == "SKIPPED_TARGET_LOCKED":
            logger.info(
                "event=scheduled_job_target_locked job_id=%s expected_run_time=%s "
                "(will remain eligible for reconciliation)",
                job_id,
                expected,
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

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._active_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error(
                "Scheduled job task ended with exception: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _run_job_with_slot(self, job_id: int, expected: datetime) -> None:
        key = (job_id, expected)
        logger.info(
            "Scheduled job waiting for execution slot job_id=%s "
            "expected_run_time=%s active=%s max=%s",
            job_id,
            expected,
            self._active_count,
            self.config.max_concurrent_jobs,
        )
        try:
            async with self._exec_semaphore:
                if self.shutdown_event.is_set():
                    logger.info(
                        "Scheduled job skipped after shutdown signal "
                        "job_id=%s expected_run_time=%s",
                        job_id,
                        expected,
                    )
                    return
                self._active_count += 1
                self._update_concurrency_status()
                logger.info(
                    "event=scheduled_job_start job_id=%s expected_run_time=%s "
                    "active=%s max=%s",
                    job_id,
                    expected,
                    self._active_count,
                    self.config.max_concurrent_jobs,
                )
                try:
                    await self._execute_one(job_id, expected)
                finally:
                    self._active_count = max(0, self._active_count - 1)
                    self._update_concurrency_status()
                    logger.info(
                        "Scheduled job finished job_id=%s expected_run_time=%s "
                        "result=%s active=%s max=%s",
                        job_id,
                        expected,
                        self.status.last_execution_result,
                        self._active_count,
                        self.config.max_concurrent_jobs,
                    )
        finally:
            self._active_keys.discard(key)
            # Coalesce refreshes: many completions → one queue rebuild.
            self.request_refresh()

    def _spawn_due_jobs(self, now: float) -> None:
        """Pop due queue items and schedule worker tasks (non-blocking)."""
        if self.shutdown_event.is_set():
            return
        while True:
            due = self.queue.pop_due(now)
            if due is None:
                break
            job_id, expected = due
            key = (job_id, expected)
            if key in self._active_keys:
                continue
            self._active_keys.add(key)
            task = asyncio.create_task(
                self._run_job_with_slot(job_id, expected),
                name="partops-job-{}-{}".format(job_id, expected.isoformat()),
            )
            self._active_tasks.add(task)
            task.add_done_callback(self._on_task_done)
        self._update_status_from_queue()

    async def _drain_active_tasks(self) -> None:
        if not self._active_tasks:
            return
        logger.info(
            "Scheduler shutting down; draining %s active task(s)",
            len(self._active_tasks),
        )
        pending = list(self._active_tasks)
        done, still = await asyncio.wait(pending, timeout=_SHUTDOWN_DRAIN_SECONDS)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)
        logger.info(
            "Scheduler drain complete (finished=%s cancelled=%s)",
            len(done),
            len(still),
        )

    async def run(self) -> None:
        self.status.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.scheduler_active = True
        self.status.extra["lookahead_seconds"] = float(self.config.lookahead_seconds)
        self.status.extra["reconcile_seconds"] = float(self.config.reconcile_seconds)
        self.status.extra["bind"] = f"{self.config.bind_host}:{self.config.bind_port}"
        self.status.extra["max_concurrent_jobs"] = int(self.config.max_concurrent_jobs)
        self._update_concurrency_status()
        await self.refresh_queue("startup")

        next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)

        while not self.shutdown_event.is_set():
            now = time.monotonic()
            self._spawn_due_jobs(now)

            if self.refresh_event.is_set():
                self.refresh_event.clear()
                if self.shutdown_event.is_set():
                    break
                await self.refresh_queue("http-refresh")
                next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)
                self._spawn_due_jobs(time.monotonic())
                continue

            if now >= next_reconcile:
                await self.refresh_queue("periodic-reconcile")
                next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)
                self._spawn_due_jobs(time.monotonic())
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

        await self._drain_active_tasks()
        self.status.scheduler_active = False
        self._update_concurrency_status()
        logger.info("Scheduler loop stopped")
