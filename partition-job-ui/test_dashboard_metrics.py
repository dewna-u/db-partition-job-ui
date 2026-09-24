"""Unit tests for read-only dashboard metrics (no DB / no scheduler side effects)."""

from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from dashboard_metrics import (
    format_countdown,
    format_uptime,
    job_counts,
    next_execution,
    success_rate,
    scheduler_uptime,
    system_insights,
)


class DashboardMetricsTests(unittest.TestCase):
    def test_next_execution_earliest_enabled(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, 0)
        jobs = [
            {
                "job_id": 10,
                "job_name": "later",
                "is_enabled": True,
                "is_create": True,
                "next_run_time": now + timedelta(hours=2),
            },
            {
                "job_id": 31,
                "job_name": "soon",
                "is_enabled": True,
                "is_create": False,
                "next_run_time": now + timedelta(minutes=12),
            },
            {
                "job_id": 99,
                "job_name": "disabled",
                "is_enabled": False,
                "is_create": True,
                "next_run_time": now + timedelta(minutes=1),
            },
        ]
        result = next_execution(jobs, now=now)
        self.assertTrue(result["available"])
        self.assertEqual(result["job_id"], 31)
        self.assertEqual(result["countdown"], "12m")
        self.assertIn("DROP", result["detail"])

    def test_next_execution_empty(self) -> None:
        result = next_execution([], now=datetime(2026, 9, 24, 12, 0, 0))
        self.assertFalse(result["available"])
        self.assertEqual(result["detail"], "No upcoming jobs")

    def test_success_rate(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, 0)
        logs = [
            {"last_run_status": "SUCCESS", "job_runtime": now - timedelta(days=1)},
            {"last_run_status": "MANUAL_SUCCESS", "job_runtime": now - timedelta(days=2)},
            {"last_run_status": "FAIL", "job_runtime": now - timedelta(days=3)},
            {"last_run_status": "SUCCESS", "job_runtime": now - timedelta(days=40)},
        ]
        result = success_rate(logs, days=30, now=now)
        self.assertTrue(result["available"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["success"], 2)
        self.assertEqual(result["label"], "66.7%")

    def test_success_rate_empty(self) -> None:
        result = success_rate([], now=datetime(2026, 9, 24, 12, 0, 0))
        self.assertFalse(result["available"])
        self.assertEqual(result["detail"], "No executions yet")

    def test_scheduler_uptime(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, 0)
        started = now - timedelta(days=2, hours=14)
        status = {
            "scheduler_active": True,
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # format_uptime uses wall clock; compare via format_uptime directly
        label = format_uptime(started, now=now)
        self.assertEqual(label, "2d 14h")
        uptime = scheduler_uptime(status)
        self.assertTrue(uptime["available"])
        self.assertEqual(uptime["detail"], "Running continuously")

    def test_scheduler_uptime_unreachable(self) -> None:
        uptime = scheduler_uptime(None)
        self.assertFalse(uptime["available"])
        self.assertEqual(uptime["label"], "Unavailable")

    def test_job_counts(self) -> None:
        jobs = [
            {"is_enabled": True, "is_create": True},
            {"is_enabled": False, "is_create": False},
            {"is_enabled": True, "is_create": False},
        ]
        counts = job_counts(jobs)
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["enabled"], 2)
        self.assertEqual(counts["disabled"], 1)

    def test_format_countdown_overdue(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, 0)
        self.assertEqual(
            format_countdown(now - timedelta(minutes=8), now=now),
            "Overdue 8m",
        )

    def test_system_insights_scheduler_offline(self) -> None:
        insights = system_insights([], [], scheduler_ok=False)
        self.assertTrue(any("unavailable" in item.lower() for item in insights))


if __name__ == "__main__":
    unittest.main()
