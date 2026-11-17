"""Phase 5D — per-target scheduler runtime: slots, availability, preflight cache.

Concurrency (global + per target)
---------------------------------
Every execution holds exactly one TARGET slot and one GLOBAL slot.
Acquisition order is always target → global, release global → target:

* a task waiting for its (saturated) target holds NO global slot, so a busy
  or slow target never consumes global capacity another target could use;
* a task holds at most one target slot and waits for the global slot last,
  so there is no lock-order cycle (no deadlock);
* all bookkeeping is under one asyncio.Condition (single event loop).

Invariants: running_global <= global_max and, per target,
holding_target <= target_max (running <= holding).

Availability
------------
A target whose connection / identity / preflight fails enters a capped
exponential cooldown (15 s → 300 s). Occurrences of that target are not
dispatched during the cooldown (no hammering, no per-job backoff loops); the
first dispatch after it is the probe. Other targets are unaffected.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

TargetId = Optional[int]


class SlotTimeout(Exception):
    """A slot could not be acquired within the requested time."""


class SlotManager:
    def __init__(self, global_max: int) -> None:
        self._global_max = max(1, int(global_max))
        self._limits: Dict[TargetId, int] = {}
        self._holding: Dict[TargetId, int] = defaultdict(int)   # target slot held
        self._running: Dict[TargetId, int] = defaultdict(int)   # target + global held
        self._waiting: Dict[TargetId, int] = defaultdict(int)   # waiting for target slot
        self._global_running = 0
        self._cond: Optional[asyncio.Condition] = None

    # -- configuration -----------------------------------------------------
    @property
    def global_max(self) -> int:
        return self._global_max

    def set_limit(self, target_id: TargetId, limit: Optional[int]) -> None:
        if target_id is None:
            return
        if limit is None:
            self._limits.pop(target_id, None)
        else:
            self._limits[target_id] = max(1, int(limit))

    def limit(self, target_id: TargetId) -> Optional[int]:
        return self._limits.get(target_id) if target_id is not None else None

    def _target_free(self, target_id: TargetId) -> bool:
        limit = self.limit(target_id)
        return limit is None or self._holding[target_id] < limit

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    # -- acquire / release -------------------------------------------------
    async def acquire(self, target_id: TargetId, timeout: Optional[float] = None) -> None:
        cond = self._condition()
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

        def remaining() -> Optional[float]:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        async with cond:
            self._waiting[target_id] += 1
            try:
                await asyncio.wait_for(cond.wait_for(lambda: self._target_free(target_id)), remaining())
            except asyncio.TimeoutError:
                raise SlotTimeout("target slot") from None
            finally:
                self._waiting[target_id] -= 1
            self._holding[target_id] += 1
            try:
                await asyncio.wait_for(
                    cond.wait_for(lambda: self._global_running < self._global_max), remaining())
            except asyncio.TimeoutError:
                self._holding[target_id] -= 1
                cond.notify_all()
                raise SlotTimeout("global slot") from None
            except BaseException:
                self._holding[target_id] -= 1
                cond.notify_all()
                raise
            self._global_running += 1
            self._running[target_id] += 1

    async def release(self, target_id: TargetId) -> None:
        cond = self._condition()
        async with cond:
            self._running[target_id] = max(0, self._running[target_id] - 1)
            self._global_running = max(0, self._global_running - 1)
            self._holding[target_id] = max(0, self._holding[target_id] - 1)
            cond.notify_all()

    # -- introspection -----------------------------------------------------
    @property
    def global_running(self) -> int:
        return self._global_running

    def running(self, target_id: TargetId) -> int:
        return self._running.get(target_id, 0)

    def waiting(self, target_id: TargetId) -> int:
        """Waiting for the target slot, or holding it while waiting for a global slot."""
        return self._waiting.get(target_id, 0) + max(
            0, self._holding.get(target_id, 0) - self._running.get(target_id, 0))


@dataclass
class _Avail:
    failures: int = 0
    until_mono: float = 0.0
    last_error_class: Optional[str] = None


class TargetAvailability:
    """Capped exponential cooldown per target after connection-level failures."""

    def __init__(self, base_seconds: float = 15.0, cap_seconds: float = 300.0) -> None:
        self._base = max(1.0, float(base_seconds))
        self._cap = max(self._base, float(cap_seconds))
        self._state: Dict[int, _Avail] = {}

    def available(self, target_id: TargetId, now: Optional[float] = None) -> bool:
        if target_id is None:
            return True
        st = self._state.get(target_id)
        now = time.monotonic() if now is None else now
        return st is None or now >= st.until_mono

    def until(self, target_id: TargetId) -> float:
        st = self._state.get(target_id) if target_id is not None else None
        return st.until_mono if st else 0.0

    def record_failure(self, target_id: int, error_class: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        st = self._state.setdefault(target_id, _Avail())
        st.failures += 1
        st.last_error_class = error_class
        delay = min(self._cap, self._base * (2 ** (st.failures - 1)))
        st.until_mono = now + delay
        return delay

    def record_success(self, target_id: int) -> None:
        self._state.pop(target_id, None)

    def snapshot(self, target_id: int, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.monotonic() if now is None else now
        st = self._state.get(target_id)
        if st is None:
            return {"available": True, "consecutive_failures": 0,
                    "cooldown_seconds_remaining": 0, "last_error_class": None}
        return {
            "available": now >= st.until_mono,
            "consecutive_failures": st.failures,
            "cooldown_seconds_remaining": max(0, int(st.until_mono - now)),
            "last_error_class": st.last_error_class,
        }


class FrameworkCheckCache:
    """Bounded, invalidatable cache of per-target framework preflight results."""

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 32) -> None:
        self._ttl = max(1.0, float(ttl_seconds))
        self._max = max(1, int(max_entries))
        self._items: Dict[int, Tuple[float, bool, str]] = {}

    def get(self, target_id: int, now: Optional[float] = None) -> Optional[Tuple[bool, str]]:
        now = time.monotonic() if now is None else now
        item = self._items.get(target_id)
        if item is None or now - item[0] > self._ttl:
            self._items.pop(target_id, None)
            return None
        return item[1], item[2]

    def put(self, target_id: int, ok: bool, state: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if len(self._items) >= self._max and target_id not in self._items:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            self._items.pop(oldest, None)
        self._items[target_id] = (now, bool(ok), str(state))

    def invalidate(self, target_id: int) -> None:
        self._items.pop(target_id, None)
