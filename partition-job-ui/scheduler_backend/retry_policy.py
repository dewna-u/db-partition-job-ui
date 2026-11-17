"""Per-occurrence ownership and bounded retry / cooldown policy (Phase 5A).

Keyed by ``(job_id, expected_run_time)``. Process-local and ephemeral; the
database stays authoritative. Nothing here sleeps or does I/O: the scheduler
turns ``not_before`` values into queue due-times and waits on its event loop.

Ownership (missed-run classification)
-------------------------------------
* OWNED — the scheduler first saw the occurrence before it was due, or no more
  than ``overdue_tolerance_seconds`` after it (timer / refresh jitter). The
  scheduler is responsible for it: it may execute late after waiting for a
  concurrency slot, a busy target table or a retry backoff. The database still
  revalidates everything and applies its own safety ceiling.
* HISTORICAL — the first sighting was already later than the tolerance (e.g.
  scheduler start after downtime, refreshes failing during a DB outage). It is
  never executed: the scheduler asks the database to reconcile it as missed.

The classification is made once, at first sighting, and never changes for the
life of the process. A scheduler restart forgets ownership, so occurrences that
are overdue at startup are HISTORICAL by design.

Retry lifecycle of one occurrence
---------------------------------
    committed status (EXECUTED / FAILED / FAILED_INVALID_SCHEDULE / MISSED_*)
        → state cleared
    FAILED_DATABASE / FAILED_CONNECTION
        → retry after retry_delays[n] (last value repeats) until max_attempts
          execution attempts were made → EXHAUSTED
    SKIPPED_TARGET_LOCKED / SKIPPED_LOCKED   (contention, not a failure)
        → cooldown target_lock_cooldowns[n] (last value repeats) until
          target_lock_max_wait_seconds elapsed since the first contention
          → EXHAUSTED
    any other non-committed status
        → cooldown >= min_redispatch_seconds, at most MAX_OTHER_OUTCOMES
    EXHAUSTED
        → the next dispatch reconciles in the DB instead of executing
          (RETRY_EXHAUSTED attention item; next_run_time advanced).
          Reconcile failures back off exponentially up to a cap.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Optional, Sequence, Tuple

OccurrenceKey = Tuple[int, datetime]

# Committed DB transitions: the occurrence is finished from the scheduler's view.
HANDLED_STATUSES = frozenset(
    {
        "EXECUTED",
        "FAILED",
        "FAILED_INVALID_SCHEDULE",
        "MISSED_RESCHEDULED",
        "MISSED_INVALID_SCHEDULE",
        # Phase 5D remote outcomes that must NEVER be re-dispatched by the
        # retry path: target commit confirmed (only control finalization may
        # be pending), outcome unknown (needs review), or already decided.
        "REMOTE_FINALIZE_PENDING",
        "REMOTE_OUTCOME_UNKNOWN",
        "REMOTE_NEEDS_REVIEW",
        "REMOTE_ALREADY_FINALIZED",
        "REMOTE_ALREADY_DISPATCHING",
    }
)
# Phase 5D: remote failures PROVEN not to have applied anything on the target
# (no connection / preflight failed / worker raised and the target transaction
# was rolled back) use the same bounded retry as control-DB failures.
REMOTE_FAILURE_STATUSES = frozenset(
    {
        "FAILED_TARGET_UNAVAILABLE",
        "FAILED_TARGET_PREFLIGHT",
        "FAILED_TARGET_WORKER",
        "FAILED_TARGET_CONFIG",
    }
)
FAILURE_STATUSES = frozenset({"FAILED_DATABASE", "FAILED_CONNECTION"}) | REMOTE_FAILURE_STATUSES
# Not executable by this scheduler (Phase 5C: job belongs to another managed
# target). Nothing to retry; get_upcoming no longer returns such jobs.
NOT_OURS_STATUSES = frozenset({"SKIPPED_REMOTE_TARGET"})
CONTENTION_STATUSES = frozenset({"SKIPPED_TARGET_LOCKED", "SKIPPED_LOCKED"})
# Reconcile outcomes after which nothing more is owed for this occurrence.
RECONCILE_CLEARING_STATUSES = HANDLED_STATUSES | NOT_OURS_STATUSES | frozenset(
    {"SKIPPED_RESCHEDULED", "SKIPPED_DISABLED", "NOT_FOUND", "SKIPPED_NOT_DUE"}
)

OWNED = "OWNED"
HISTORICAL = "HISTORICAL"

# Conservative defaults (documented in .env.realtime.example / README).
DEFAULT_OVERDUE_TOLERANCE_SECONDS = 60.0
DEFAULT_RETRY_MAX_ATTEMPTS = 4
# ~4 min of total retry window: rides out a typical DB restart / failover.
DEFAULT_FAILURE_DELAYS: Tuple[float, ...] = (15.0, 60.0, 180.0)
DEFAULT_TARGET_LOCK_COOLDOWNS: Tuple[float, ...] = (5.0, 10.0, 20.0, 30.0)
DEFAULT_TARGET_LOCK_MAX_WAIT_SECONDS = 3600.0
DEFAULT_EXHAUSTED_RETRY_SECONDS = 60.0
# Backwards-compatible alias (pre-hardening name).
DEFAULT_CONTENTION_DELAYS = DEFAULT_TARGET_LOCK_COOLDOWNS

MIN_REDISPATCH_SECONDS = 1.0
MAX_OTHER_OUTCOMES = 20
MAX_RECONCILE_BACKOFF_SECONDS = 600.0
DEFAULT_MAX_ENTRIES = 1024
DEFAULT_TTL_SECONDS = 24 * 3600.0

ACTION_CLEARED = "cleared"
ACTION_RETRY = "retry"
ACTION_COOLDOWN = "cooldown"
ACTION_EXHAUSTED = "exhausted"


@dataclass
class RetryState:
    failures: int = 0
    contentions: int = 0
    others: int = 0
    reconcile_failures: int = 0
    last_status: str = ""
    exhausted: bool = False
    exhausted_status: str = ""
    first_contention_mono: Optional[float] = None
    next_eligible_mono: float = 0.0
    updated_mono: float = 0.0

    @property
    def attempts(self) -> int:
        """Execution attempts made so far for this occurrence."""
        return self.failures + self.contentions + self.others


@dataclass(frozen=True)
class RetryDecision:
    action: str
    delay_seconds: float
    attempt: int
    status: str


def _delay_at(delays: Sequence[float], index: int) -> float:
    """``delays[index]``; the last value repeats beyond the list."""
    return float(delays[min(max(index, 0), len(delays) - 1)])


class OccurrenceRetryTracker:
    """Bounded per-occurrence retry state (no I/O, no sleeping)."""

    def __init__(
        self,
        failure_delays: Sequence[float] = DEFAULT_FAILURE_DELAYS,
        contention_delays: Sequence[float] = DEFAULT_TARGET_LOCK_COOLDOWNS,
        exhausted_retry_seconds: float = DEFAULT_EXHAUSTED_RETRY_SECONDS,
        min_redispatch_seconds: float = MIN_REDISPATCH_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_attempts: Optional[int] = None,
        target_lock_max_wait_seconds: float = DEFAULT_TARGET_LOCK_MAX_WAIT_SECONDS,
    ) -> None:
        self._min = max(0.1, float(min_redispatch_seconds))
        self._failure_delays = tuple(
            max(self._min, float(d)) for d in failure_delays
        ) or (self._min,)
        self._contention_delays = tuple(
            max(self._min, float(d)) for d in contention_delays
        ) or (self._min,)
        # Default keeps the historical meaning: 1 attempt + 1 retry per delay.
        attempts = (
            len(self._failure_delays) + 1 if max_attempts is None else int(max_attempts)
        )
        self._max_attempts = max(1, attempts)
        self._lock_max_wait = max(self._min, float(target_lock_max_wait_seconds))
        self._exhausted_retry = max(self._min, float(exhausted_retry_seconds))
        self._max_entries = max(1, int(max_entries))
        self._ttl = max(60.0, float(ttl_seconds))
        self._states: "OrderedDict[OccurrenceKey, RetryState]" = OrderedDict()

    # -- introspection -----------------------------------------------------
    @property
    def max_failure_attempts(self) -> int:
        """Total execution attempts allowed for DB/connection failures."""
        return self._max_attempts

    def __len__(self) -> int:
        return len(self._states)

    def __contains__(self, key: object) -> bool:
        return key in self._states

    def get(self, key: OccurrenceKey) -> Optional[RetryState]:
        return self._states.get(key)

    def is_exhausted(self, key: OccurrenceKey) -> bool:
        state = self._states.get(key)
        return bool(state and state.exhausted)

    def exhausted_count(self) -> int:
        return sum(1 for s in self._states.values() if s.exhausted)

    def not_before(self, key: OccurrenceKey) -> Optional[float]:
        state = self._states.get(key)
        return state.next_eligible_mono if state else None

    def not_before_map(self) -> Dict[OccurrenceKey, float]:
        return {k: s.next_eligible_mono for k, s in self._states.items()}

    # -- mutation ------------------------------------------------------------
    def clear(self, key: OccurrenceKey) -> None:
        self._states.pop(key, None)

    def _touch(self, key: OccurrenceKey, now: float) -> RetryState:
        state = self._states.pop(key, None) or RetryState()
        state.updated_mono = now
        self._states[key] = state
        while len(self._states) > self._max_entries:
            self._states.popitem(last=False)
        return state

    def record_outcome(
        self, key: OccurrenceKey, status: str, now_mono: Optional[float] = None
    ) -> RetryDecision:
        """Record one execution outcome and decide when it may be re-dispatched."""
        now = time.monotonic() if now_mono is None else float(now_mono)
        status = (status or "").strip().upper() or "UNKNOWN"
        if status in HANDLED_STATUSES or status in NOT_OURS_STATUSES:
            self.clear(key)
            return RetryDecision(ACTION_CLEARED, 0.0, 0, status)

        state = self._touch(key, now)
        state.last_status = status
        if status in FAILURE_STATUSES:
            state.failures += 1
            if state.failures >= self._max_attempts:
                return self._exhaust(state, status, now)
            delay = _delay_at(self._failure_delays, state.failures - 1)
            action = ACTION_RETRY
        elif status in CONTENTION_STATUSES:
            state.contentions += 1
            if state.first_contention_mono is None:
                state.first_contention_mono = now
            waited = now - state.first_contention_mono
            if waited >= self._lock_max_wait:
                return self._exhaust(state, status, now)
            delay = _delay_at(self._contention_delays, state.contentions - 1)
            # Never schedule past the wait budget by more than one cooldown.
            delay = min(delay, max(self._min, self._lock_max_wait - waited))
            action = ACTION_RETRY
        else:
            state.others += 1
            if state.others > MAX_OTHER_OUTCOMES:
                return self._exhaust(state, status, now)
            delay = self._min
            action = ACTION_COOLDOWN

        delay = max(self._min, float(delay))
        state.next_eligible_mono = now + delay
        return RetryDecision(action, delay, state.attempts, status)

    def _exhaust(self, state: RetryState, status: str, now: float) -> RetryDecision:
        state.exhausted = True
        state.exhausted_status = status
        state.next_eligible_mono = now + self._min
        return RetryDecision(ACTION_EXHAUSTED, self._min, state.attempts, status)

    def record_reconcile_outcome(
        self, key: OccurrenceKey, status: str, now_mono: Optional[float] = None
    ) -> RetryDecision:
        """Outcome of a DB reconcile call (exhausted or historical occurrence)."""
        now = time.monotonic() if now_mono is None else float(now_mono)
        status = (status or "").strip().upper() or "UNKNOWN"
        if status in RECONCILE_CLEARING_STATUSES:
            self.clear(key)
            return RetryDecision(ACTION_CLEARED, 0.0, 0, status)
        state = self._touch(key, now)
        state.reconcile_failures += 1
        delay = min(
            self._exhausted_retry * (2 ** (state.reconcile_failures - 1)),
            max(self._exhausted_retry, MAX_RECONCILE_BACKOFF_SECONDS),
        )
        state.next_eligible_mono = now + delay
        return RetryDecision(ACTION_EXHAUSTED, delay, state.attempts, status)

    def retain(
        self,
        present_keys: Iterable[OccurrenceKey],
        protected_keys: Iterable[OccurrenceKey] = (),
        now_mono: Optional[float] = None,
    ) -> int:
        """Drop state for occurrences no longer in the DB snapshot or stale.

        Call only after a SUCCESSFUL refresh. ``protected_keys`` (in-flight)
        are never dropped. Returns the number of entries removed.
        """
        now = time.monotonic() if now_mono is None else float(now_mono)
        protected = set(protected_keys)
        keep = set(present_keys) | protected
        removed = 0
        for key in list(self._states.keys()):
            state = self._states[key]
            stale = (now - state.updated_mono) > self._ttl
            if key not in keep or (stale and key not in protected):
                del self._states[key]
                removed += 1
        return removed


class OccurrenceOwnership:
    """First-sighting classification: OWNED vs HISTORICAL (process-local)."""

    def __init__(
        self,
        overdue_tolerance_seconds: float = DEFAULT_OVERDUE_TOLERANCE_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES * 4,
    ) -> None:
        self._tolerance = max(0.0, float(overdue_tolerance_seconds))
        self._max_entries = max(1, int(max_entries))
        self._classes: "OrderedDict[OccurrenceKey, str]" = OrderedDict()

    @property
    def tolerance_seconds(self) -> float:
        return self._tolerance

    def __len__(self) -> int:
        return len(self._classes)

    def get(self, key: OccurrenceKey) -> Optional[str]:
        return self._classes.get(key)

    def observe(self, key: OccurrenceKey, delay_seconds: float) -> str:
        """Classify at first sighting; later sightings never change it.

        ``delay_seconds`` comes from the database clock
        (next_run_time - now): negative means already overdue.
        """
        existing = self._classes.get(key)
        if existing is not None:
            return existing
        cls = OWNED if float(delay_seconds) >= -self._tolerance else HISTORICAL
        self._classes[key] = cls
        while len(self._classes) > self._max_entries:
            self._classes.popitem(last=False)
        return cls

    def forget(self, key: OccurrenceKey) -> None:
        self._classes.pop(key, None)

    def retain(
        self, present_keys: Iterable[OccurrenceKey], protected_keys: Iterable[OccurrenceKey] = ()
    ) -> int:
        keep = set(present_keys) | set(protected_keys)
        removed = 0
        for key in list(self._classes.keys()):
            if key not in keep:
                del self._classes[key]
                removed += 1
        return removed

    def counts(self) -> Dict[str, int]:
        owned = sum(1 for c in self._classes.values() if c == OWNED)
        return {"owned": owned, "historical": len(self._classes) - owned}


def parse_delay_list(raw: Optional[str], default: Sequence[float], name: str) -> Tuple[float, ...]:
    """Parse "5,15,30" into delays. Rejects empty/negative/huge/non-numeric."""
    if raw is None or str(raw).strip() == "":
        return tuple(float(d) for d in default)
    parts = [p.strip() for p in str(raw).split(",")]
    values = []
    for part in parts:
        try:
            value = float(part)
        except ValueError as exc:
            raise RuntimeError(
                "{} must be a comma-separated list of seconds (got {!r}).".format(name, raw)
            ) from exc
        if not (1.0 <= value <= 3600.0):
            raise RuntimeError(
                "{} values must be between 1 and 3600 seconds (got {!r}).".format(name, raw)
            )
        values.append(value)
    if not (1 <= len(values) <= 10):
        raise RuntimeError("{} must list between 1 and 10 delays.".format(name))
    return tuple(values)


def parse_bounded_int(raw: Optional[str], default: int, low: int, high: int, name: str) -> int:
    """Strict integer in [low, high]; unset → default; anything else → error."""
    if raw is None or str(raw).strip() == "":
        return int(default)
    text = str(raw).strip()
    if not text.isdigit():
        raise RuntimeError("{} must be a whole number (got {!r}).".format(name, raw))
    value = int(text)
    if not (low <= value <= high):
        raise RuntimeError(
            "{} must be between {} and {} (got {}).".format(name, low, high, value)
        )
    return value


def parse_bounded_seconds(
    raw: Optional[str], default: float, low: float, high: float, name: str
) -> float:
    """Strict number of seconds in [low, high]; unset → default."""
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise RuntimeError("{} must be a number of seconds (got {!r}).".format(name, raw)) from exc
    if not (low <= value <= high):
        raise RuntimeError(
            "{} must be between {} and {} seconds (got {!r}).".format(name, low, high, raw)
        )
    return value
