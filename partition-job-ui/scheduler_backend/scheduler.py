"""Realtime scheduler loop: timers, refresh signal, reconciliation, execution."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from database_targets import (
    HEALTHY,
    HEALTHY_WITH_WARNINGS,
    check_target_health,
    metric_target_tag,
)
from observability import (
    emit_framework_ready,
    emit_remote_attempt,
    emit_remote_finalization_retry,
    emit_remote_unknown_outcomes,
    emit_target_health,
    emit_target_slots,
    emit_job_execution,
    emit_jobs_failed_count,
    emit_missed_occurrence,
    emit_pending_review_count,
    emit_retry_exhausted,
    emit_retry_scheduled,
    emit_scheduler_concurrency,
    emit_scheduler_execution_exception,
    emit_scheduler_heartbeat,
    emit_scheduler_refresh_failure,
)
from scheduler_backend.models import (
    ScheduledJob,
    SchedulerConfig,
    SchedulerStatus,
    target_table_resource_key,
)
from scheduler_backend.queue_manager import ScheduleQueue
from scheduler_backend.remote_execution import (
    FAILED,
    SUCCEEDED,
    UNKNOWN,
    ControlLedger,
    RemoteClaim,
    TargetOutcome,
    run_target_transaction,
)
from scheduler_backend.retry_policy import (
    ACTION_COOLDOWN,
    ACTION_EXHAUSTED,
    ACTION_RETRY,
    HANDLED_STATUSES,
    HISTORICAL,
    OccurrenceOwnership,
    OccurrenceRetryTracker,
    RetryDecision,
)
from scheduler_backend.scheduler_database import (
    check_framework_compatibility,
    fetch_control_target,
    count_failed_jobs,
    count_pending_attention,
    count_unknown_outcomes,
    execute_scheduled_job,
    fetch_targets,
    fetch_upcoming_jobs,
    reconcile_occurrence,
    remote_claim,
    remote_finalize,
    remote_mark_dispatching,
    remote_mark_unknown,
    remote_record_failure,
    remote_record_success,
    remote_recover_attempts,
)
from scheduler_backend.target_runtime import (
    FrameworkCheckCache,
    SlotManager,
    SlotTimeout,
    TargetAvailability,
)

logger = logging.getLogger(__name__)

# Rapid-loop breaker: cache only when the DB committed a transition of this
# occurrence (SQL returned status AND surrounding conn.transaction() committed).
#   EXECUTED                 worker ran; next_run_time advanced; txn committed
#   FAILED                   SQL-handled worker failure; occurrence advanced
#   FAILED_INVALID_SCHEDULE  fail-closed next_run_time=NULL; txn committed
#   MISSED_RESCHEDULED       Phase 5A: not executed; attention row; next advanced
#   MISSED_INVALID_SCHEDULE  Phase 5A: not executed; attention row; next NULL
# NOT cached: FAILED_DATABASE, FAILED_CONNECTION, SKIPPED_*, NOT_FOUND,
# SKIPPED_TARGET_LOCKED, connection/transaction exceptions. Those are instead
# governed by the bounded per-occurrence retry tracker (never immediate).
_HANDLED_OCCURRENCE_STATUSES = HANDLED_STATUSES
_MISSED_STATUSES = frozenset({"MISSED_RESCHEDULED", "MISSED_INVALID_SCHEDULE"})

# Phase 5D: claim decisions that end the occurrence without target work.
_CLAIM_STATUS = {
    "NEEDS_REVIEW": "REMOTE_NEEDS_REVIEW",
    "ALREADY_FINALIZED": "REMOTE_ALREADY_FINALIZED",
    "ALREADY_DISPATCHING": "REMOTE_ALREADY_DISPATCHING",
    "BUSY": "SKIPPED_TARGET_LOCKED",
    "CONFIG_REJECTED": "FAILED_TARGET_CONFIG",
    "SKIPPED_TARGET_DISABLED": "SKIPPED_DISABLED",
    "NOT_REMOTE": "SKIPPED_RESCHEDULED",
}
# Not dispatched (proven): error class -> retry-policy status.
_NOT_DISPATCHED_STATUS = {
    "TARGET_LOCKED": "SKIPPED_TARGET_LOCKED",
    "STATE_CONFLICT": "SKIPPED_LOCKED",
    "CONTROL_UNAVAILABLE": "FAILED_CONNECTION",
    "CONFIG_REJECTED": "FAILED_TARGET_CONFIG",
    "TARGET_MISMATCH": "FAILED_TARGET_CONFIG",
    "INVALID_REQUEST": "FAILED_TARGET_CONFIG",
    "UNREACHABLE": "FAILED_TARGET_UNAVAILABLE",
    "AUTH_FAILED": "FAILED_TARGET_UNAVAILABLE",
    "WRONG_DATABASE": "FAILED_TARGET_UNAVAILABLE",
    "CONFIG_ERROR": "FAILED_TARGET_UNAVAILABLE",
    "DISABLED": "FAILED_TARGET_UNAVAILABLE",
    "CONNECT_ERROR": "FAILED_TARGET_UNAVAILABLE",
}
# Target-level problems that put the whole target into an availability cooldown.
_CONNECTION_STATES = frozenset(
    {"UNREACHABLE", "AUTH_FAILED", "WRONG_DATABASE", "CONFIG_ERROR", "DISABLED", "CONNECT_ERROR"}
)

# Secondary rapid-loop breaker bounds (process-local only; DB is source of truth).
_CIRCUIT_BREAKER_MAX_ENTRIES = 512
_CIRCUIT_BREAKER_TTL_SECONDS = 300.0

# Bounded wait for in-flight workers during shutdown (seconds).
_SHUTDOWN_DRAIN_SECONDS = 120.0

# Coalesce refresh signals: at most one queue rebuild per interval. Prevents a
# stream of completions / HTTP refresh calls from becoming a DB-polling spin.
_MIN_REFRESH_INTERVAL_SECONDS = 1.0

# Pending-review gauge: at most one COUNT per interval unless a miss occurred.
_PENDING_COUNT_INTERVAL_SECONDS = 60.0


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
        self._retry = OccurrenceRetryTracker(
            failure_delays=config.retry_delays,
            contention_delays=config.target_lock_cooldowns,
            exhausted_retry_seconds=config.exhausted_retry_seconds,
            max_attempts=config.retry_max_attempts,
            target_lock_max_wait_seconds=config.target_lock_max_wait_seconds,
        )
        # Missed-run classification: OWNED (seen on time) vs HISTORICAL.
        self._ownership = OccurrenceOwnership(config.overdue_tolerance_seconds)
        # Phase 5D: global + per-target slots (target first, then global).
        self._slots = SlotManager(int(config.max_concurrent_jobs))
        self._targets: Dict[int, Any] = {}
        self._occurrences: Dict[Tuple[int, datetime], ScheduledJob] = {}
        self._availability = TargetAvailability(
            config.target_cooldown_base_seconds, config.target_cooldown_max_seconds)
        self._fw_cache = FrameworkCheckCache(config.target_preflight_ttl_seconds)
        self._busy_tables: Set[Tuple[Optional[int], str, str]] = set()
        # attempt_id -> next try (monotonic): TARGET_SUCCEEDED awaiting CONTROL
        # finalization. Never re-executes target DDL.
        self._finalize_pending: Dict[int, float] = {}
        self._finalize_failures: Dict[int, int] = {}
        # attempt_id -> (action, args, next_try, failures): ledger writes that
        # failed after the target outcome was known.
        self._ledger_pending: Dict[int, Tuple[str, Any, float, int]] = {}
        self._pending_task: Optional[asyncio.Task] = None
        self._executor_id = "scheduler-{}".format(uuid.uuid4().hex[:12])
        self._active_tasks: Set[asyncio.Task] = set()
        self._active_keys: Set[Tuple[int, datetime]] = set()
        self._active_count = 0
        self._last_refresh_mono = float("-inf")
        self._last_signal_refresh_mono = float("-inf")
        self._last_pending_count_mono = float("-inf")
        self._pending_count_dirty = True
        # Phase 5C: the control database's own target (set once the gate passes).
        self._control_target: Any = None
        # Phase 5B framework gate: nothing is dispatched until it passes.
        self._framework_ready = not bool(config.framework_check_enabled)
        self._framework_next_check = 0.0
        self.status.extra["framework_status"] = (
            "not_checked" if config.framework_check_enabled else "disabled"
        )
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

    def _update_retry_status(self) -> None:
        self.status.extra["retry_tracked"] = len(self._retry)
        self.status.extra["retry_exhausted"] = self._retry.exhausted_count()
        counts = self._ownership.counts()
        self.status.extra["owned_occurrences"] = counts["owned"]
        self.status.extra["historical_occurrences"] = counts["historical"]

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
        self.status.extra["global_active"] = int(self._active_count)
        self.status.extra["global_max"] = int(self.config.max_concurrent_jobs)
        self.status.extra["targets"] = self._targets_status()
        overdue = self.status.extra.get("overdue_jobs")
        emit_scheduler_concurrency(
            active_jobs=self.status.active_job_count,
            max_concurrent_jobs=self.status.max_concurrent_jobs,
            queue_depth=self.status.queued_job_count,
            available_slots=available,
            overdue_jobs=int(overdue) if overdue is not None else None,
        )

    def _targets_status(self) -> Dict[str, Any]:
        """Per-target operational view (no host, credentials or secret ids)."""
        out: Dict[str, Any] = {}
        now = time.monotonic()
        targets = dict(getattr(self, "_targets", {}) or {})
        control = self._control_target
        if control is not None and getattr(control, "target_id", None) not in targets:
            targets[control.target_id] = control
        for target_id, target in sorted(targets.items()):
            key = metric_target_tag(target) or str(target_id)
            avail = self._availability.snapshot(target_id, now)
            item = {
                "target_id": target_id,
                "is_control_database": bool(getattr(target, "is_control_database", False)),
                "is_enabled": bool(getattr(target, "is_enabled", True)),
                "active_jobs": self._slots.running(target_id),
                "waiting_jobs": self._slots.waiting(target_id),
                "max_concurrent_jobs": self._slots.limit(target_id),
                **avail,
            }
            out[key] = item
            emit_target_slots(target=key, active=item["active_jobs"], waiting=item["waiting_jobs"],
                              limit=item["max_concurrent_jobs"], available=bool(avail["available"]))
        return out

    async def refresh_queue(self, reason: str) -> bool:
        """
        Open DB → fetch upcoming jobs → close DB → rebuild memory queue.

        Occurrences under retry/cooldown keep their not-before time, so a
        refresh can never turn a failed occurrence into an immediate re-run.

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

        # Phase 5D: discovery returns jobs of every enabled target of the hub
        # (database_target_id / target_key). The pre-5D fallback returns only
        # control-database jobs: annotate them with the control target id.
        await self._refresh_targets()
        control_id = getattr(self._control_target, "target_id", None)
        if control_id is not None:
            jobs = [
                j if j.database_target_id is not None
                else dataclasses.replace(j, database_target_id=control_id, is_control_target=True)
                for j in jobs
            ]
        self._occurrences = {job.key: job for job in jobs}
        # Forget retry/ownership state for occurrences the DB no longer reports.
        present = [job.key for job in jobs]
        self._retry.retain(present, protected_keys=set(self._active_keys))
        self._ownership.retain(present, protected_keys=set(self._active_keys))
        # Classify NEW occurrences at first sighting (sticky afterwards):
        # seen before due / within tolerance → OWNED; already overdue → HISTORICAL.
        for job in jobs:
            cls = self._ownership.observe(job.key, float(job.delay_seconds))
            if cls == HISTORICAL and job.key not in self._active_keys:
                logger.info(
                    "event=historical_occurrence_discovered job_id=%s "
                    "expected_run_time=%s overdue_seconds=%.0f tolerance_seconds=%.0f "
                    "(will be reconciled as missed; NOT executed)",
                    job.job_id,
                    job.expected_run_time,
                    -float(job.delay_seconds),
                    self._ownership.tolerance_seconds,
                )

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

        self.queue.replace_from_jobs(
            filtered, not_before=self._retry.not_before_map()
        )
        self.status.extra["overdue_jobs"] = overdue_count
        self._update_status_from_queue()
        self._update_retry_status()
        self.status.last_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.last_refresh_result = f"ok:{reason}:{len(filtered)}"
        self.status.extra["circuit_breaker_entries"] = len(self._handled_occurrences)
        self._backoff = float(self.config.connect_retry_seconds)
        # Heartbeat on successful reconcile/refresh (existing cycle; no extra loop).
        emit_scheduler_heartbeat()
        # Current enabled FAIL job rows (gauge). Never invent 0 on COUNT failure.
        try:
            failed_count = await asyncio.to_thread(count_failed_jobs, self.config)
        except Exception:  # noqa: BLE001 — do not break refresh/scheduling
            logger.exception(
                "event=failed_jobs_count_failed reason=%s",
                reason,
            )
        else:
            self.status.extra["failed_jobs_count"] = int(failed_count)
            emit_jobs_failed_count(int(failed_count))
        await self._maybe_refresh_pending_count(reason)
        logger.info(
            "event=scheduler_refresh_ok reason=%s upcoming=%s overdue=%s "
            "failed_jobs=%s pending_review=%s retry_tracked=%s next_job_id=%s "
            "next_expected_run_time=%s active=%s max=%s",
            reason,
            len(filtered),
            overdue_count,
            self.status.extra.get("failed_jobs_count"),
            self.status.extra.get("pending_review_count"),
            len(self._retry),
            self.status.next_job_id,
            self.status.next_expected_run_time,
            self._active_count,
            self.config.max_concurrent_jobs,
        )
        return True

    async def _maybe_refresh_pending_count(self, reason: str) -> None:
        """Pending-review gauge, throttled (one cheap COUNT per interval)."""
        now = time.monotonic()
        if (
            not self._pending_count_dirty
            and (now - self._last_pending_count_mono) < _PENDING_COUNT_INTERVAL_SECONDS
        ):
            return
        self._last_pending_count_mono = now
        self._pending_count_dirty = False
        try:
            pending = await asyncio.to_thread(count_pending_attention, self.config)
        except Exception:  # noqa: BLE001 — never break refresh/scheduling
            logger.exception("event=pending_review_count_failed reason=%s", reason)
            return
        if pending is None:
            return
        self.status.extra["pending_review_count"] = int(pending)
        emit_pending_review_count(int(pending))
        if self._control_target is None:
            return
        try:
            unknown = await asyncio.to_thread(count_unknown_outcomes, self.config)
        except Exception:  # noqa: BLE001
            logger.exception("event=unknown_outcome_count_failed reason=%s", reason)
            return
        if unknown is not None:
            self.status.extra["unknown_outcomes"] = int(unknown)
            emit_remote_unknown_outcomes(int(unknown))

    async def _ensure_framework(self) -> bool:
        """Run the read-only framework gate (rate-limited) until it passes.

        While it fails or cannot be evaluated, no occurrence is dispatched (no
        execution, no reconcile). Once it passes it is not re-run for the life
        of the process; later object removal is caught by bounded retries.
        """
        if self._framework_ready:
            return True
        now = time.monotonic()
        if now < self._framework_next_check:
            return False
        self._framework_next_check = now + float(self.config.framework_recheck_seconds)
        try:
            report = await asyncio.to_thread(check_framework_compatibility, self.config)
        except Exception:  # noqa: BLE001 — unknown ⇒ stay blocked
            logger.exception("event=framework_check_error (scheduler stays blocked)")
            self.status.extra["framework_status"] = "unknown"
            emit_framework_ready(False)
            return False
        self.status.extra["framework_expected_version"] = report.expected_version
        self.status.extra["framework_database_version"] = report.database_version
        if report.ok:
            try:
                target = await asyncio.to_thread(fetch_control_target, self.config)
            except Exception:  # noqa: BLE001 — registry problem ⇒ stay blocked
                logger.exception("event=control_target_lookup_failed (scheduler stays blocked)")
                self.status.extra["framework_status"] = "blocked"
                self.status.extra["framework_failures"] = ["control target lookup failed"]
                emit_framework_ready(False)
                return False
            self._set_control_target(target)
            try:
                await self._recover_remote_attempts()
            except Exception:  # noqa: BLE001 — never dispatch with an unclassified ledger
                logger.exception("event=remote_attempt_recovery_failed (scheduler stays blocked)")
                self.status.extra["framework_status"] = "blocked"
                self.status.extra["framework_failures"] = ["remote attempt recovery failed"]
                emit_framework_ready(False)
                return False
            self._framework_ready = True
            await self._refresh_targets()
            self.status.extra["framework_status"] = "ready"
            self.status.extra.pop("framework_failures", None)
            logger.info(
                "event=framework_check_passed expected=%s database=%s warnings=%s",
                report.expected_version,
                report.database_version,
                len(report.warnings),
            )
            emit_framework_ready(True)
            return True
        failures = [
            "{} [{}] {}".format(r.object, r.check, r.detail) for r in report.failures
        ]
        self.status.extra["framework_status"] = "blocked"
        self.status.extra["framework_failures"] = failures[:20]
        logger.error(
            "event=framework_check_failed failures=%s expected=%s database=%s "
            "(NO job will be dispatched; next check in %.0f s): %s",
            len(failures),
            report.expected_version,
            report.database_version,
            float(self.config.framework_recheck_seconds),
            " | ".join(failures[:10]),
        )
        emit_framework_ready(False)
        return False

    def _set_control_target(self, target: Any) -> None:
        self._control_target = target
        self.status.extra["control_target_id"] = getattr(target, "target_id", None)
        self.status.extra["control_target_key"] = getattr(target, "target_key", None)
        self.status.extra["control_target_max_concurrent_jobs"] = getattr(
            target, "max_concurrent_jobs", None)
        target_id = getattr(target, "target_id", None)
        limit = getattr(target, "max_concurrent_jobs", None)
        if isinstance(target_id, int) and isinstance(limit, int):
            self._slots.set_limit(target_id, limit)
            if limit < int(self.config.max_concurrent_jobs):
                logger.warning(
                    "event=control_target_limit_below_global target=%s max_concurrent_jobs=%s "
                    "global=%s (control-database jobs run at most %s at a time; raise "
                    "partops_database_target.max_concurrent_jobs to keep Phase 2 parallelism)",
                    getattr(target, "target_key", None), limit,
                    self.config.max_concurrent_jobs, limit)

    def _target_tag(self) -> Optional[str]:
        """Low-cardinality metric tag (registry key) of the control target."""
        key = getattr(self._control_target, "target_key", None)
        return key if isinstance(key, str) else None

    def _record_retry(
        self, job_id: int, expected: datetime, status: str,
        target_tag: Optional[str] = None,
    ) -> RetryDecision:
        """Apply the bounded retry policy to a non-handled outcome."""
        tag = target_tag if target_tag is not None else self._target_tag()
        decision = self._retry.record_outcome((job_id, expected), status)
        if decision.action == ACTION_RETRY:
            logger.warning(
                "event=scheduled_occurrence_retry_scheduled job_id=%s "
                "expected_run_time=%s status=%s attempt=%s delay_seconds=%.1f",
                job_id,
                expected,
                status,
                decision.attempt,
                decision.delay_seconds,
            )
            emit_retry_scheduled(status=status, target=tag)
        elif decision.action == ACTION_EXHAUSTED:
            logger.error(
                "event=scheduled_occurrence_retry_exhausted job_id=%s "
                "expected_run_time=%s last_status=%s attempts=%s "
                "(no further execution; handing to DB reconciliation)",
                job_id,
                expected,
                status,
                decision.attempt,
            )
            emit_retry_exhausted(status=status, target=tag)
        elif decision.action == ACTION_COOLDOWN:
            logger.info(
                "event=scheduled_occurrence_cooldown job_id=%s expected_run_time=%s "
                "status=%s delay_seconds=%.1f",
                job_id,
                expected,
                status,
                decision.delay_seconds,
            )
        self._update_retry_status()
        return decision

    def _note_missed(
        self, job_id: int, expected: datetime, status: str, reason: str, result: Dict[str, Any]
    ) -> None:
        self._pending_count_dirty = True
        logger.warning(
            "event=missed_occurrence_reconciled job_id=%s missed_run_time=%s "
            "reason=%s status=%s rescheduled_to=%s (NOT executed; needs DBA review)",
            job_id,
            expected,
            reason,
            status,
            result.get("next_run_time"),
        )
        emit_missed_occurrence(reason=reason, status=status, target=self._target_tag())

    async def _reconcile_occurrence(
        self, job_id: int, expected: datetime, reason: str
    ) -> None:
        """Ask the DB to record the occurrence for review and advance it.

        Never executes CREATE/DROP. ``reason``:
          MISSED_SCHEDULED_RUN — historical occurrence (first seen overdue)
          RETRY_EXHAUSTED      — owned occurrence whose retry budget is used up
        """
        key = (job_id, expected)
        state = self._retry.get(key)
        if reason == "RETRY_EXHAUSTED":
            attempts = state.attempts if state else 0
            last_status = (
                (state.exhausted_status or state.last_status) if state else "UNKNOWN"
            )
            detail = (
                "Scheduled execution did not complete after {} attempt(s); last "
                "result {}. Not executed automatically.".format(attempts, last_status)
            )
        else:
            detail = (
                "Occurrence was already overdue when the scheduler first "
                "discovered it (e.g. scheduler downtime or database outage). "
                "Not executed automatically."
            )
        logger.warning(
            "event=occurrence_reconcile job_id=%s expected_run_time=%s reason=%s",
            job_id,
            expected,
            reason,
        )
        try:
            result = await asyncio.to_thread(
                reconcile_occurrence, self.config, job_id, expected, reason, detail
            )
            status = str(result.get("status") or "FAILED_DATABASE")
        except Exception:  # noqa: BLE001 — never kill the scheduler process
            logger.exception(
                "event=occurrence_reconcile_exception job_id=%s expected_run_time=%s",
                job_id,
                expected,
            )
            result = {}
            status = "FAILED_CONNECTION"

        self.status.last_execution_job_id = job_id
        self.status.last_execution_result = status
        decision = self._retry.record_reconcile_outcome(key, status)
        if status in _MISSED_STATUSES:
            self._handled_occurrences.add(key, status)
            self._ownership.forget(key)
            self._note_missed(job_id, expected, status, reason, result)
        elif decision.action == ACTION_EXHAUSTED:
            logger.error(
                "event=occurrence_reconcile_failed job_id=%s expected_run_time=%s "
                "reason=%s status=%s next_attempt_in_seconds=%.0f",
                job_id,
                expected,
                reason,
                status,
                decision.delay_seconds,
            )
        self._update_retry_status()

    async def _reconcile_exhausted(self, job_id: int, expected: datetime) -> None:
        """Backwards-compatible alias: RETRY_EXHAUSTED reconciliation."""
        await self._reconcile_occurrence(job_id, expected, "RETRY_EXHAUSTED")

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

        if self._ownership.get(key) == HISTORICAL:
            # First seen already overdue: never executed, only reconciled.
            await self._reconcile_occurrence(job_id, expected, "MISSED_SCHEDULED_RUN")
            return

        if self._retry.is_exhausted(key):
            # Bounded retries are used up: never execute this occurrence again.
            await self._reconcile_occurrence(job_id, expected, "RETRY_EXHAUSTED")
            return

        job = self._occurrences.get(key)
        if self._is_remote(job):
            # Phase 5D: managed target → remote orchestrator. Control-database
            # jobs keep the proven run_partition_job_scheduled path below.
            await self._execute_remote(job)
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
            emit_job_execution(
                status="FAILED_CONNECTION", is_create=None, target=self._target_tag()
            )
            # Do not cache: transaction likely rolled back; occurrence still open.
            # Bounded, delayed retry instead of immediate rediscovery.
            self._record_retry(job_id, expected, "FAILED_CONNECTION")
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
            status=status, is_create=is_create, duration_ms=duration_ms,
            target=self._target_tag(),
        )
        if status == "SKIPPED_TARGET_LOCKED":
            logger.info(
                "event=scheduled_job_target_locked job_id=%s expected_run_time=%s "
                "(owned occurrence; bounded cooldown before re-attempt)",
                job_id,
                expected,
            )

        if status in _HANDLED_OCCURRENCE_STATUSES:
            self._handled_occurrences.add(key, status)
            self._retry.clear(key)
            self._ownership.forget(key)
            self._update_retry_status()
            if status in _MISSED_STATUSES:
                self._note_missed(
                    job_id, expected, status, "MISSED_SCHEDULED_RUN", result
                )
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
            return

        # Not advanced by the DB: never re-dispatch immediately.
        self._record_retry(job_id, expected, status)

    # ------------------------------------------------------------------
    # Phase 5D — targets, slots, remote orchestration
    # ------------------------------------------------------------------
    def _control_id(self) -> Optional[int]:
        return getattr(self._control_target, "target_id", None)

    def _is_remote(self, job: Optional[ScheduledJob]) -> bool:
        if job is None or job.database_target_id is None:
            return False
        if job.is_control_target is not None:
            return not bool(job.is_control_target)
        control_id = self._control_id()
        return control_id is not None and job.database_target_id != control_id

    def _slot_target(self, key: Tuple[int, datetime]) -> Optional[int]:
        job = self._occurrences.get(key)
        if job is not None and job.database_target_id is not None:
            return job.database_target_id
        return self._control_id()

    def _target_key_of(self, target_id: Optional[int]) -> Optional[str]:
        target = self._targets.get(target_id) if target_id is not None else None
        if target is not None:
            return metric_target_tag(target)
        if target_id is not None and target_id == self._control_id():
            return self._target_tag()
        return None

    async def _refresh_targets(self) -> None:
        """Reload the hub's target registry (CONTROL DB). Failure keeps the last copy."""
        if not self._framework_ready or self._control_target is None:
            return
        try:
            targets = await asyncio.to_thread(fetch_targets, self.config)
        except Exception:  # noqa: BLE001 — never break scheduling
            logger.exception("event=target_registry_refresh_failed (keeping previous registry)")
            return
        self._targets = dict(targets)
        for target in self._targets.values():
            self._slots.set_limit(target.target_id, target.max_concurrent_jobs)

    async def _recover_remote_attempts(self) -> None:
        """Scheduler start: classify attempts left by a previous process.

        DISPATCHING → OUTCOME_UNKNOWN (never re-dispatched); TARGET_SUCCEEDED →
        control finalization only; stale PREPARED/TARGET_FAILED → closed.
        """
        rows = await asyncio.to_thread(remote_recover_attempts, self.config, self._executor_id)
        unknown = 0
        for row in rows:
            action = str(row.get("action") or "")
            attempt_id = row.get("attempt_id")
            if action == "FINALIZE" and attempt_id is not None:
                self._finalize_pending[int(attempt_id)] = 0.0
            elif action == "MARKED_UNKNOWN":
                unknown += 1
                logger.critical(
                    "event=remote_attempt_outcome_unknown_after_restart attempt_id=%s "
                    "(was DISPATCHING; NOT re-dispatched; needs review)", attempt_id)
                emit_remote_attempt(outcome="unknown", target=None)
        self.status.extra["recovered_attempts"] = len(rows)
        if unknown:
            self._pending_count_dirty = True
        logger.info("event=remote_attempt_recovery rows=%s finalize=%s marked_unknown=%s",
                    len(rows), len(self._finalize_pending), unknown)

    async def _target_ready(self, target: Any) -> Tuple[bool, str]:
        """Cached read-only target_execution preflight (bounded TTL)."""
        cached = self._fw_cache.get(target.target_id)
        if cached is not None:
            return cached
        health = await asyncio.to_thread(check_target_health, target)
        state = str(health.get("state") or "CONFIG_ERROR")
        ok = state in (HEALTHY, HEALTHY_WITH_WARNINGS)
        emit_target_health(target=metric_target_tag(target), state=state)
        if ok:
            self._fw_cache.put(target.target_id, True, state)
        return ok, state

    def _target_failure(self, target_id: int, error_class: str) -> None:
        delay = self._availability.record_failure(target_id, error_class)
        self._fw_cache.invalidate(target_id)
        logger.warning("event=target_unavailable target=%s error_class=%s cooldown_seconds=%.0f",
                       self._target_key_of(target_id), error_class, delay)

    def _ledger(self) -> ControlLedger:
        cfg = self.config
        return ControlLedger(
            mark_dispatching=lambda a: remote_mark_dispatching(cfg, a, self._executor_id),
            record_success=lambda a, d: remote_record_success(cfg, a, d),
            record_failure=lambda a, c, s, d: remote_record_failure(cfg, a, c, s, d),
            mark_unknown=lambda a, s: remote_mark_unknown(cfg, a, s),
        )

    async def _record_pre_dispatch_failure(self, attempt_id: int, error_class: str, summary: str) -> None:
        try:
            await asyncio.to_thread(remote_record_failure, self.config, attempt_id, error_class, summary, None)
        except Exception:  # noqa: BLE001 — attempt stays PREPARED; re-claim is safe
            logger.exception("event=remote_record_failure_failed attempt_id=%s", attempt_id)

    def _remember_ledger_write(self, attempt_id: int, outcome: TargetOutcome) -> None:
        if outcome.ledger_recorded or not outcome.ledger_action:
            return
        args: Any
        if outcome.ledger_action == "success":
            args = outcome.duration_ms
        elif outcome.ledger_action == "unknown":
            args = outcome.summary
        else:
            args = (outcome.error_class or "WORKER_ERROR", outcome.summary, outcome.duration_ms)
        self._ledger_pending[attempt_id] = (outcome.ledger_action, args, 0.0, 0)

    async def _finalize_attempt(self, attempt_id: int, target_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """CONTROL finalization of a confirmed target commit. Never re-runs DDL."""
        try:
            result = await asyncio.to_thread(remote_finalize, self.config, attempt_id)
        except Exception:  # noqa: BLE001 — retried later (persisted TARGET_SUCCEEDED)
            fails = self._finalize_failures.get(attempt_id, 0) + 1
            self._finalize_failures[attempt_id] = fails
            delay = min(300.0, 15.0 * (2 ** (fails - 1)))
            self._finalize_pending[attempt_id] = time.monotonic() + delay
            logger.exception("event=remote_finalize_failed attempt_id=%s retry_in_seconds=%.0f "
                             "(target DDL is NOT re-executed)", attempt_id, delay)
            emit_remote_finalization_retry(target=target_key)
            return None
        decision = str(result.get("decision") or "")
        if decision == "RETRY_LATER":
            self._finalize_pending[attempt_id] = time.monotonic() + 5.0
            emit_remote_finalization_retry(target=target_key)
            return None
        self._finalize_pending.pop(attempt_id, None)
        self._finalize_failures.pop(attempt_id, None)
        if decision not in ("FINALIZED", "ALREADY_FINALIZED"):
            logger.critical("event=remote_finalize_unexpected attempt_id=%s decision=%s", attempt_id, decision)
        else:
            logger.info("event=remote_attempt_finalized attempt_id=%s result=%s next_run_time=%s",
                        attempt_id, result.get("result"), result.get("next_run_time"))
        return result

    async def _drain_pending(self) -> None:
        """Retry ONLY control-database writes (ledger / finalization)."""
        now = time.monotonic()
        for attempt_id, (action, args, next_try, fails) in list(self._ledger_pending.items()):
            if next_try > now:
                continue
            try:
                if action == "success":
                    await asyncio.to_thread(remote_record_success, self.config, attempt_id, args)
                elif action == "unknown":
                    await asyncio.to_thread(remote_mark_unknown, self.config, attempt_id, args)
                else:
                    await asyncio.to_thread(remote_record_failure, self.config, attempt_id, *args)
            except Exception:  # noqa: BLE001
                delay = min(300.0, 15.0 * (2 ** fails))
                self._ledger_pending[attempt_id] = (action, args, time.monotonic() + delay, fails + 1)
                logger.exception("event=remote_ledger_retry_failed attempt_id=%s action=%s", attempt_id, action)
                emit_remote_finalization_retry(target=None)
                continue
            del self._ledger_pending[attempt_id]
            if action == "success":
                self._finalize_pending[attempt_id] = 0.0
        for attempt_id, next_try in list(self._finalize_pending.items()):
            if next_try <= time.monotonic():
                await self._finalize_attempt(attempt_id)
        self._update_remote_status()

    def _service_pending(self, now: float) -> None:
        if self._pending_task is not None and not self._pending_task.done():
            return
        due = any(t <= now for t in self._finalize_pending.values()) or any(
            v[2] <= now for v in self._ledger_pending.values())
        if due:
            self._pending_task = asyncio.create_task(self._drain_pending(), name="partops-remote-pending")

    def _next_pending_due(self) -> Optional[float]:
        times = list(self._finalize_pending.values()) + [v[2] for v in self._ledger_pending.values()]
        return min(times) if times else None

    def _complete_remote(
        self, key: Tuple[int, datetime], status: str, target_key: Optional[str],
        is_create: Any = None, duration_ms: Any = None, result: Optional[Dict[str, Any]] = None,
    ) -> None:
        job_id, expected = key
        self.status.last_execution_job_id = job_id
        self.status.last_execution_result = status
        emit_job_execution(status=status, is_create=is_create, duration_ms=duration_ms, target=target_key)
        if status in _HANDLED_OCCURRENCE_STATUSES:
            self._handled_occurrences.add(key, status)
            self._retry.clear(key)
            self._ownership.forget(key)
            self._update_retry_status()
            if status in _MISSED_STATUSES:
                self._note_missed(job_id, expected, status, "MISSED_SCHEDULED_RUN", result or {})
            return
        self._record_retry(job_id, expected, status, target_tag=target_key)

    async def _execute_remote(self, job: ScheduledJob) -> None:
        """Remote scheduled occurrence: claim → target txn → finalize (Phase 5D)."""
        key = job.key
        target = self._targets.get(job.database_target_id)
        target_key = metric_target_tag(target) if target is not None else job.target_key
        try:
            claim_row = await asyncio.to_thread(
                remote_claim, self.config, job.job_id, job.expected_run_time,
                "SCHEDULED", self._executor_id)
        except Exception:  # noqa: BLE001 — control DB problem; nothing dispatched
            logger.exception("event=remote_claim_failed job_id=%s expected_run_time=%s",
                             job.job_id, job.expected_run_time)
            self._complete_remote(key, "FAILED_CONNECTION", target_key)
            return
        decision = str(claim_row.get("decision") or "")
        attempt_id = claim_row.get("attempt_id")
        logger.info("event=remote_claim job_id=%s expected_run_time=%s target=%s decision=%s attempt_id=%s",
                    job.job_id, job.expected_run_time, target_key, decision, attempt_id)
        if decision == "FINALIZE_PENDING" and attempt_id is not None:
            result = await self._finalize_attempt(int(attempt_id), target_key)
            status = "EXECUTED" if result else "REMOTE_FINALIZE_PENDING"
            if result is None:
                self._finalize_pending.setdefault(int(attempt_id), time.monotonic() + 15.0)
            self._complete_remote(key, status, target_key, job.is_create)
            return
        if decision != "CLAIMED":
            status = _CLAIM_STATUS.get(decision, decision or "FAILED_DATABASE")
            if decision == "ALREADY_DISPATCHING":
                logger.critical("event=remote_attempt_already_dispatching attempt_id=%s "
                                "(never dispatched twice; restart recovery will classify it)", attempt_id)
            self._complete_remote(key, status, target_key, job.is_create)
            return

        claim = RemoteClaim.from_row(job.job_id, claim_row)
        is_create = claim.operation == "CREATE"
        if target is None or not target.is_enabled or target.target_id != claim.database_target_id:
            await self._record_pre_dispatch_failure(claim.attempt_id, "TARGET_NOT_LOADED",
                                                    "target registry not loaded or disabled")
            self._complete_remote(key, "FAILED_TARGET_UNAVAILABLE", target_key, is_create)
            return
        resource = target_table_resource_key(target.target_id, claim.table_schema, claim.table_name)
        if resource in self._busy_tables:
            await self._record_pre_dispatch_failure(claim.attempt_id, "TARGET_LOCKED",
                                                    "table busy in this scheduler")
            self._complete_remote(key, "SKIPPED_TARGET_LOCKED", target_key, is_create)
            return
        try:
            ok, state = await self._target_ready(target)
        except Exception:  # noqa: BLE001
            logger.exception("event=target_preflight_error target=%s", target_key)
            ok, state = False, "CONFIG_ERROR"
        if not ok:
            await self._record_pre_dispatch_failure(claim.attempt_id, state, "target preflight: " + state)
            if state in _CONNECTION_STATES:
                self._target_failure(target.target_id, state)
                status = "FAILED_TARGET_UNAVAILABLE"
            else:
                status = "FAILED_TARGET_PREFLIGHT"
            emit_remote_attempt(outcome="not_dispatched", target=target_key)
            self._complete_remote(key, status, target_key, is_create)
            return

        self._busy_tables.add(resource)
        try:
            outcome = await asyncio.to_thread(
                run_target_transaction, target, claim, self._ledger(), schema=self.config.schema)
        finally:
            self._busy_tables.discard(resource)
        self._remember_ledger_write(claim.attempt_id, outcome)
        emit_remote_attempt(outcome=outcome.kind.lower(), target=target_key)

        if outcome.kind == SUCCEEDED:
            self._availability.record_success(target.target_id)
            if outcome.ledger_recorded:
                result = await self._finalize_attempt(claim.attempt_id, target_key)
                status = "EXECUTED" if result else "REMOTE_FINALIZE_PENDING"
            else:
                status = "REMOTE_FINALIZE_PENDING"
            self._complete_remote(key, status, target_key, is_create, outcome.duration_ms,
                                  result=None)
        elif outcome.kind == UNKNOWN:
            self._pending_count_dirty = True
            self._complete_remote(key, "REMOTE_OUTCOME_UNKNOWN", target_key, is_create, outcome.duration_ms)
        elif outcome.kind == FAILED:
            self._availability.record_success(target.target_id)
            self._complete_remote(key, "FAILED_TARGET_WORKER", target_key, is_create, outcome.duration_ms)
        else:
            error_class = outcome.error_class or ""
            if error_class in _CONNECTION_STATES:
                self._target_failure(target.target_id, error_class)
            self._complete_remote(key, _NOT_DISPATCHED_STATUS.get(error_class, "FAILED_TARGET_PREFLIGHT"),
                                  target_key, is_create)
        self._update_remote_status()

    async def run_manual_remote(
        self, job_id: int, confirm_drop: bool = False, attention_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Manual run of a job on a MANAGED target through the same engine.

        Same claim / target lock / identity + framework check / ledger as
        scheduled execution; never retried automatically; schedule unchanged.
        """
        if not self._framework_ready:
            return {"status": "SCHEDULER_NOT_READY",
                    "message": "Scheduler framework gate has not passed; nothing was executed."}
        try:
            claim_row = await asyncio.to_thread(
                remote_claim, self.config, int(job_id), None, "MANUAL", self._executor_id,
                attention_id, bool(confirm_drop))
        except Exception:  # noqa: BLE001
            logger.exception("event=remote_manual_claim_failed job_id=%s", job_id)
            return {"status": "MANUAL_NOT_RUN", "message": "Control database unavailable; nothing was executed."}
        decision = str(claim_row.get("decision") or "")
        if decision != "CLAIMED":
            return {"status": decision or "MANUAL_NOT_RUN", "message": claim_row.get("message"),
                    "attempt_id": claim_row.get("attempt_id")}
        claim = RemoteClaim.from_row(int(job_id), claim_row)
        target = self._targets.get(claim.database_target_id)
        target_key = metric_target_tag(target) if target is not None else claim.target_key
        base = {"attempt_id": claim.attempt_id, "target_key": claim.target_key,
                "operation": claim.operation}
        if target is None or not target.is_enabled:
            await self._record_pre_dispatch_failure(claim.attempt_id, "TARGET_NOT_LOADED", "target not loaded")
            return dict(base, status="MANUAL_NOT_RUN", message="Target registry not loaded; nothing was executed.")
        try:
            await self._slots.acquire(target.target_id, timeout=self.config.manual_slot_timeout_seconds)
        except SlotTimeout:
            await self._record_pre_dispatch_failure(claim.attempt_id, "SLOT_TIMEOUT", "no execution slot")
            return dict(base, status="MANUAL_TARGET_BUSY",
                        message="No execution slot for this target became free; nothing was executed.")
        self._active_count += 1
        self._update_concurrency_status()
        try:
            resource = target_table_resource_key(target.target_id, claim.table_schema, claim.table_name)
            if resource in self._busy_tables:
                await self._record_pre_dispatch_failure(claim.attempt_id, "TARGET_LOCKED", "table busy")
                return dict(base, status="MANUAL_TARGET_LOCKED",
                            message="The target table is busy; nothing was executed. Try again later.")
            ok, state = await self._target_ready(target)
            if not ok:
                await self._record_pre_dispatch_failure(claim.attempt_id, state, "target preflight: " + state)
                if state in _CONNECTION_STATES:
                    self._target_failure(target.target_id, state)
                return dict(base, status="MANUAL_TARGET_UNAVAILABLE",
                            message="Target {} is not ready ({}); nothing was executed.".format(claim.target_key, state))
            self._busy_tables.add(resource)
            try:
                outcome = await asyncio.to_thread(
                    run_target_transaction, target, claim, self._ledger(), schema=self.config.schema)
            finally:
                self._busy_tables.discard(resource)
        finally:
            self._active_count = max(0, self._active_count - 1)
            await self._slots.release(target.target_id)
            self._update_concurrency_status()

        self._remember_ledger_write(claim.attempt_id, outcome)
        emit_remote_attempt(outcome=outcome.kind.lower(), target=target_key)
        if outcome.kind == SUCCEEDED:
            self._availability.record_success(target.target_id)
            result = await self._finalize_attempt(claim.attempt_id, target_key) if outcome.ledger_recorded else None
            if result is None:
                self._finalize_pending.setdefault(claim.attempt_id, time.monotonic() + 15.0)
                return dict(base, status="MANUAL_FINALIZE_PENDING", execution_duration_ms=outcome.duration_ms,
                            message="Target operation committed; history is being recorded (no re-run).")
            return dict(base, status="MANUAL_SUCCESS", execution_duration_ms=outcome.duration_ms,
                        message=result.get("message"))
        if outcome.kind == UNKNOWN:
            self._pending_count_dirty = True
            return dict(base, status="MANUAL_OUTCOME_UNKNOWN", execution_duration_ms=outcome.duration_ms,
                        message="The target outcome could not be confirmed. DO NOT re-run until the "
                                "target state has been inspected; resolve it under Needs review.")
        if outcome.kind == FAILED:
            return dict(base, status="MANUAL_FAIL", execution_duration_ms=outcome.duration_ms,
                        message=outcome.summary)
        if (outcome.error_class or "") in _CONNECTION_STATES:
            self._target_failure(target.target_id, outcome.error_class or "")
        status = "MANUAL_TARGET_LOCKED" if outcome.error_class == "TARGET_LOCKED" else "MANUAL_NOT_RUN"
        return dict(base, status=status, message="Nothing was executed ({}).".format(outcome.error_class))

    def _update_remote_status(self) -> None:
        self.status.extra["finalization_pending"] = len(self._finalize_pending)
        self.status.extra["ledger_writes_pending"] = len(self._ledger_pending)

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
        slot_target = self._slot_target(key)
        try:
            await self._slots.acquire(slot_target)
            try:
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
                await self._slots.release(slot_target)
        finally:
            self._active_keys.discard(key)
            # Coalesce refreshes: many completions → one queue rebuild.
            self.request_refresh()

    def _spawn_due_jobs(self, now: float) -> None:
        """Pop due queue items and schedule worker tasks (non-blocking)."""
        if self.shutdown_event.is_set():
            return
        if not self._framework_ready:
            # Framework gate not passed: never execute or reconcile anything.
            return
        while True:
            due = self.queue.pop_due(now)
            if due is None:
                break
            job_id, expected = due
            key = (job_id, expected)
            if key in self._active_keys:
                continue
            not_before = self._retry.not_before(key)
            if not_before is not None and not_before > now:
                # Defensive: never dispatch an occurrence inside its backoff
                # (e.g. a queue rebuilt by code that bypassed not_before).
                self.queue.push(job_id, expected, not_before)
                continue
            job = self._occurrences.get(key)
            if (self._is_remote(job)
                    and self._ownership.get(key) != HISTORICAL
                    and not self._retry.is_exhausted(key)):
                target_id = job.database_target_id
                if not self._availability.available(target_id, now):
                    # Target cooling down: re-queue at the end of the cooldown
                    # (no slot used, no connection attempt, no retry consumed).
                    self.queue.push(job_id, expected, self._availability.until(target_id))
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

    async def _wait_for_shutdown(self, timeout: float) -> None:
        """Sleep without blocking the loop; returns early on shutdown."""
        try:
            await asyncio.wait_for(
                self.shutdown_event.wait(), timeout=max(0.01, float(timeout))
            )
        except asyncio.TimeoutError:
            pass

    async def _refresh(self, reason: str) -> None:
        await self.refresh_queue(reason)
        self._last_refresh_mono = time.monotonic()

    async def run(self) -> None:
        self.status.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.status.scheduler_active = True
        self.status.extra["lookahead_seconds"] = float(self.config.lookahead_seconds)
        self.status.extra["reconcile_seconds"] = float(self.config.reconcile_seconds)
        self.status.extra["bind"] = f"{self.config.bind_host}:{self.config.bind_port}"
        self.status.extra["max_concurrent_jobs"] = int(self.config.max_concurrent_jobs)
        self.status.extra["retry_delays_seconds"] = list(self.config.retry_delays)
        self.status.extra["retry_max_attempts"] = int(self.config.retry_max_attempts)
        self.status.extra["overdue_tolerance_seconds"] = float(
            self.config.overdue_tolerance_seconds
        )
        self._update_concurrency_status()
        self._update_retry_status()
        await self._refresh("startup")
        await self._ensure_framework()

        next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)

        while not self.shutdown_event.is_set():
            if not self._framework_ready:
                await self._ensure_framework()
            now = time.monotonic()
            self._spawn_due_jobs(now)
            if self._framework_ready:
                self._service_pending(now)

            if self.refresh_event.is_set():
                if self.shutdown_event.is_set():
                    break
                since = now - self._last_signal_refresh_mono
                if since < _MIN_REFRESH_INTERVAL_SECONDS:
                    # Coalesce back-to-back signal refreshes (completions / HTTP):
                    # bounded refresh rate, still shutdown-responsive. The first
                    # signal after a quiet period is handled immediately.
                    await self._wait_for_shutdown(
                        _MIN_REFRESH_INTERVAL_SECONDS - since
                    )
                    continue
                self.refresh_event.clear()
                self._last_signal_refresh_mono = time.monotonic()
                await self._refresh("http-refresh")
                next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)
                self._spawn_due_jobs(time.monotonic())
                continue

            if now >= next_reconcile:
                await self._refresh("periodic-reconcile")
                next_reconcile = time.monotonic() + float(self.config.reconcile_seconds)
                self._spawn_due_jobs(time.monotonic())
                continue

            peeked = self.queue.peek()
            wait_candidates = [next_reconcile - now, float(self.config.reconcile_seconds)]
            if not self._framework_ready:
                # Due items cannot be dispatched while blocked: wait for the
                # next gate check instead of the (already due) queue head.
                wait_candidates.append(max(0.0, self._framework_next_check - now))
            elif peeked is not None:
                wait_candidates.append(max(0.0, peeked[0] - now))
            if self.status.last_refresh_result.startswith("error:"):
                wait_candidates.append(self._backoff)
            pending_due = self._next_pending_due()
            if self._framework_ready and pending_due is not None:
                wait_candidates.append(max(0.0, pending_due - now))

            timeout = max(0.05, min(wait_candidates))
            try:
                await asyncio.wait_for(self.refresh_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        await self._drain_active_tasks()
        if self._pending_task is not None and not self._pending_task.done():
            try:
                await asyncio.wait_for(self._pending_task, timeout=30)
            except Exception:  # noqa: BLE001
                logger.exception("event=remote_pending_drain_incomplete (recovered at next start)")
        self.status.scheduler_active = False
        self._update_concurrency_status()
        logger.info("Scheduler loop stopped")
