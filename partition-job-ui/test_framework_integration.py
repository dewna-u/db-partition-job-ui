"""Full framework integration tests (sql/framework/INTEGRATION_TEST_PLAN.md).

These run the disposable-database harness end to end, including
sql/tests/disposable/40_integration_full.sql, and check one 'ITEST <id> OK'
marker per planned case.

They SKIP — never pass — until:
  1. every required authoritative definition is captured into
     sql/framework/captured/  → otherwise: "FRAMEWORK DEFINITION NOT AVAILABLE"
  2. PARTOPS_DISPOSABLE_PG_BIN points at existing PostgreSQL/EDB binaries
     (opt-in; nothing is installed) → otherwise: "DISPOSABLE POSTGRES NOT CONFIGURED"
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
CAPTURED = os.path.join(ROOT, "sql", "framework", "captured")
PLAN = os.path.join(ROOT, "sql", "framework", "INTEGRATION_TEST_PLAN.md")
HARNESS = os.path.join(ROOT, "scripts", "disposable_db", "run_disposable_db_test.sh")
REQUIRED_CAPTURES = (
    "cron_to_interval_or_next_run",
    "create_any_table_partition",
    "drop_any_table_partition",
    "insert_data_to_partition_job_table",
)


def missing_captures():
    return [n for n in REQUIRED_CAPTURES if not os.path.isfile(os.path.join(CAPTURED, n + ".sql"))]


def skip_reason():
    missing = missing_captures()
    if missing:
        return "FRAMEWORK DEFINITION NOT AVAILABLE: " + ", ".join(missing)
    if not os.environ.get("PARTOPS_DISPOSABLE_PG_BIN"):
        return "DISPOSABLE POSTGRES NOT CONFIGURED (set PARTOPS_DISPOSABLE_PG_BIN)"
    if not shutil.which("bash"):
        return "bash not available"
    return None


def _plan_ids():
    with open(PLAN, encoding="utf-8") as handle:
        return re.findall(r"^\| ((?:C|D|S|SEC|T)\d) \|", handle.read(), flags=re.M)


class FullFrameworkIntegrationTests(unittest.TestCase):
    output = ""

    @classmethod
    def setUpClass(cls):
        reason = skip_reason()
        if reason:
            raise unittest.SkipTest(reason)
        env = dict(os.environ, PG_BIN=os.environ["PARTOPS_DISPOSABLE_PG_BIN"])
        proc = subprocess.run(
            ["bash", HARNESS, "--i-understand-this-is-disposable"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=900,
        )
        cls.returncode = proc.returncode
        cls.output = proc.stdout + proc.stderr

    def test_harness_succeeded(self):
        self.assertEqual(self.returncode, 0, self.output[-4000:])
        self.assertIn("full integration: RUN", self.output)

    def test_every_planned_case_reported_ok(self):
        for case in _plan_ids():
            with self.subTest(case=case):
                # S4 (target lock) needs a second session: 30_target_lock.sql (T8).
                marker = "T8 OK" if case == "S4" else "ITEST {} OK".format(case)
                self.assertIn(marker, self.output)


class IntegrationGatingTests(unittest.TestCase):
    """Always runs: proves the gating reports honestly instead of passing."""

    def test_skip_reason_names_missing_definitions(self):
        missing = missing_captures()
        if missing:
            self.assertTrue(skip_reason().startswith("FRAMEWORK DEFINITION NOT AVAILABLE"))
            for name in missing:
                self.assertIn(name, skip_reason())

    def test_plan_has_all_required_areas(self):
        ids = set(_plan_ids())
        for expected in ("C1", "C2", "C3", "C4", "D1", "D2", "D3", "D4", "S1", "S2", "S3",
                         "S4", "S5", "SEC1", "SEC2", "SEC3", "T1", "T2", "T3", "T4"):
            self.assertIn(expected, ids)


if __name__ == "__main__":
    unittest.main()
