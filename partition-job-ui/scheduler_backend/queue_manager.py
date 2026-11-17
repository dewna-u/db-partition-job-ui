"""In-memory priority queue for upcoming scheduled job occurrences."""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Optional, Tuple

from scheduler_backend.models import ScheduledJob


@dataclass(order=True)
class _HeapItem:
    due_monotonic: float
    expected_run_time: datetime
    job_id: int


class ScheduleQueue:
    """
    Priority queue keyed by (job_id, expected_run_time).

    Stale heap entries are ignored lazily after authoritative rebuild/reconcile.
    """

    def __init__(self) -> None:
        self._heap: list[_HeapItem] = []
        self._active: dict[tuple[int, datetime], float] = {}

    def __len__(self) -> int:
        return len(self._active)

    def clear(self) -> None:
        self._heap.clear()
        self._active.clear()

    def replace_from_jobs(
        self,
        jobs: Iterable[ScheduledJob],
        *,
        now_mono: Optional[float] = None,
        not_before: Optional[Mapping[Tuple[int, datetime], float]] = None,
    ) -> None:
        """Rebuild the authoritative set from a fresh DB snapshot.

        ``not_before`` maps occurrence keys to the earliest monotonic time they
        may be dispatched (retry backoff / cooldown). The due time is the later
        of the DB-derived time and that bound — a refresh can never make an
        occurrence under backoff due immediately.
        """
        now = time.monotonic() if now_mono is None else now_mono
        bounds = not_before or {}
        self._heap.clear()
        self._active.clear()
        for job in jobs:
            key = job.key
            due_mono = now + float(job.delay_seconds)
            bound = bounds.get(key)
            if bound is not None and bound > due_mono:
                due_mono = bound
            self.push(job.job_id, job.expected_run_time, due_mono)

    def push(self, job_id: int, expected_run_time: datetime, due_mono: float) -> None:
        """Insert or move one occurrence to ``due_mono`` (lazy heap update)."""
        key = (job_id, expected_run_time)
        self._active[key] = due_mono
        heapq.heappush(
            self._heap,
            _HeapItem(
                due_monotonic=due_mono,
                expected_run_time=expected_run_time,
                job_id=job_id,
            ),
        )

    def peek(self) -> Optional[tuple[float, int, datetime]]:
        """Return (due_monotonic, job_id, expected_run_time) for the next active item."""
        while self._heap:
            item = self._heap[0]
            key = (item.job_id, item.expected_run_time)
            active_due = self._active.get(key)
            if active_due is None or active_due != item.due_monotonic:
                heapq.heappop(self._heap)
                continue
            return (item.due_monotonic, item.job_id, item.expected_run_time)
        return None

    def pop_due(
        self, now_mono: Optional[float] = None
    ) -> Optional[tuple[int, datetime]]:
        """Pop the next due active item, or None if the head is still in the future."""
        now = time.monotonic() if now_mono is None else now_mono
        peeked = self.peek()
        if peeked is None:
            return None
        due_mono, job_id, expected = peeked
        if due_mono > now:
            return None
        self._active.pop((job_id, expected), None)
        heapq.heappop(self._heap)
        return (job_id, expected)

    def active_jobs_sorted(self) -> list[tuple[int, datetime, float]]:
        """Return active entries ordered by due time then job_id (for status)."""
        items = [
            (due, job_id, expected)
            for (job_id, expected), due in self._active.items()
        ]
        items.sort(key=lambda row: (row[0], row[1], row[2]))
        return [(job_id, expected, due) for due, job_id, expected in items]
