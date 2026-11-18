"""Phase 5F.1 — performance / resource optimisation tests.

No real database: SQL is captured on a fake connection. Covers server-side
history and attempt pagination, filters and validation, the per-job latest
manual result (no client-side 200-row scan), per-job attention, the one-
connection dashboard snapshot and the frontend polling/data-source changes.
"""

from __future__ import annotations

import os
import re
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

import database
from api.main import app
from database import DatabaseError

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(HERE, "frontend", "src")


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        handler = self.conn.on_execute
        if handler is not None:
            handler(sql, params)

    def fetchall(self):
        return list(self.conn.rows.pop(0)) if self.conn.rows else []

    def fetchone(self):
        return self.conn.one.pop(0) if self.conn.one else None


class _Conn:
    def __init__(self, rows=None, one=None, on_execute=None):
        self.executed = []
        self.rows = list(rows or [])
        self.one = list(one or [])
        self.on_execute = on_execute
        self.rollbacks = 0

    def cursor(self, row_factory=None):
        return _Cursor(self)

    def rollback(self):
        self.rollbacks += 1

    def commit(self):  # pragma: no cover — reads never commit
        raise AssertionError("read path must not commit")


class _Counter:
    """Counts how many connections a call opens."""

    def __init__(self, conn):
        self.conn = conn
        self.opened = 0

    def __call__(self, _kwargs):
        @contextmanager
        def _cm():
            self.opened += 1
            yield self.conn
        return _cm()


def _run(fn, conn, **kwargs):
    counter = _Counter(conn)
    with patch.object(database, "_connection", counter), \
            patch.object(database, "_main_db_kwargs", return_value={}):
        result = fn(**kwargs)
    return result, counter.opened


def _rows(n, start=1):
    return [{"job_log_id": i, "job_id": 5, "job_name": "J", "last_run_status": "SUCCESS",
             "job_runtime": datetime(2026, 11, 1), "job_error": None, "execution_duration_ms": 1,
             "database_target_id": None, "execution_attempt_id": None}
            for i in range(start, start + n)]


# ---------------------------------------------------------------------------
# History pagination
# ---------------------------------------------------------------------------
class HistoryPaginationTests(unittest.TestCase):
    def test_first_page_has_more_and_next_offset(self):
        conn = _Conn(rows=[_rows(26)])
        page, opened = _run(database.query_partition_job_logs, conn, limit=25, offset=0)
        self.assertEqual(opened, 1)
        self.assertEqual(len(page["logs"]), 25)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 25)
        self.assertIsNone(page["previous_offset"])
        sql, params = conn.executed[0]
        self.assertEqual(params["lim"], 26)  # LIMIT n+1, no COUNT(*)
        self.assertEqual(params["off"], 0)
        self.assertNotIn("count(", sql.lower())

    def test_last_page(self):
        conn = _Conn(rows=[_rows(3)])
        page, _ = _run(database.query_partition_job_logs, conn, limit=25, offset=50)
        self.assertFalse(page["has_more"])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(page["previous_offset"], 25)

    def test_ordering_is_deterministic_and_bounded(self):
        conn = _Conn(rows=[[]])
        _run(database.query_partition_job_logs, conn)
        sql = conn.executed[0][0]
        self.assertIn("ORDER BY l.job_runtime DESC, l.job_log_id DESC", sql)
        self.assertIn("LIMIT %(lim)s OFFSET %(off)s", sql)
        for forbidden in ("insert", "update ", "delete", "truncate"):
            self.assertNotIn(forbidden, sql.lower())

    def test_target_filter_resolves_control_target_once(self):
        # managed target: plain equality (no per-row function call)
        conn = _Conn(rows=[[]], one=[{"id": 1}])
        _, opened = _run(database.query_partition_job_logs, conn, target_id=2)
        self.assertEqual(opened, 1)
        self.assertIn("partops_control_target_id()", conn.executed[0][0])
        sql = conn.executed[1][0]
        self.assertIn("l.database_target_id = %(target_id)s", sql)
        self.assertNotIn("IS NULL", sql)
        self.assertNotIn("partops_control_target_id", sql)
        # control target: NULL history rows (local executors, pre-5D) included
        conn = _Conn(rows=[[]], one=[{"id": 1}])
        _run(database.query_partition_job_logs, conn, target_id=1)
        self.assertIn("l.database_target_id IS NULL", conn.executed[1][0])

    def test_target_filter_requires_5d_function(self):
        from psycopg.errors import UndefinedFunction

        def handler(sql, _p):
            if "partops_control_target_id" in sql:
                raise UndefinedFunction("missing")
        with self.assertRaises(DatabaseError):
            _run(database.query_partition_job_logs, _Conn(on_execute=handler), target_id=2)

    def test_filters_are_bound_parameters(self):
        conn = _Conn(rows=[[]], one=[{"id": 1}])
        _run(database.query_partition_job_logs, conn, status="manual_fail", job="ORD_%x",
             operation="drop", target_id=1, since=datetime(2026, 1, 1), until=datetime(2026, 2, 1))
        sql, params = conn.executed[1]
        self.assertEqual(params["target_id"], 1)
        self.assertEqual(params["status"], "MANUAL_FAIL")
        self.assertEqual(params["job_name"], "%ORD\\_\\%x%")  # LIKE wildcards escaped
        self.assertIs(params["is_create"], False)
        self.assertIn("l.last_run_status = %(status)s", sql)
        self.assertIn("l.job_name ILIKE %(job_name)s", sql)
        self.assertIn("jj.is_create = %(is_create)s", sql)
        self.assertIn("l.job_runtime >= %(since)s", sql)
        self.assertIn("l.job_runtime < %(until)s", sql)
        # NULL target rows belong to the control database
        self.assertIn("l.database_target_id IS NULL", sql)
        self.assertNotIn("ORD_", sql)  # value never interpolated

    def test_numeric_job_filter_is_job_id(self):
        conn = _Conn(rows=[[]])
        _run(database.query_partition_job_logs, conn, job="42")
        sql, params = conn.executed[0]
        self.assertEqual(params["job_id"], 42)
        self.assertIn("l.job_id = %(job_id)s", sql)

    def test_invalid_inputs_rejected(self):
        for kwargs in ({"limit": 0}, {"limit": database.HISTORY_MAX_LIMIT + 1}, {"offset": -1},
                       {"offset": database.HISTORY_MAX_OFFSET + 1}, {"status": "x; drop"},
                       {"operation": "truncate"}, {"limit": "abc"}):
            with self.assertRaises(DatabaseError, msg=kwargs):
                _run(database.query_partition_job_logs, _Conn(), **kwargs)


class HistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _page(self, **extra):
        page = {"logs": [], "limit": 100, "offset": 0, "has_more": False,
                "next_offset": None, "previous_offset": None}
        page.update(extra)
        return page

    def test_max_limit_validation(self):
        with patch("api.routers.logs.query_partition_job_logs") as q:
            for url in (f"/api/logs?limit={database.HISTORY_MAX_LIMIT + 1}", "/api/logs?limit=0",
                        "/api/logs?offset=-1", "/api/logs?status=bad%20status",
                        "/api/logs?operation=truncate", "/api/logs?target_id=0",
                        "/api/logs?since=not-a-date"):
                self.assertEqual(self.client.get(url).status_code, 422, url)
            q.assert_not_called()

    def test_filters_forwarded_and_page_returned(self):
        with patch("api.routers.logs.query_partition_job_logs",
                   return_value=self._page(has_more=True, next_offset=50, limit=50)) as q, \
                patch("api.routers.logs.list_database_targets", return_value=([], [], False)):
            r = self.client.get("/api/logs?limit=50&offset=0&status=FAIL&job=ORD&operation=create"
                                "&target_id=2&since=2026-11-01T00:00:00")
        self.assertEqual(r.status_code, 200)
        kw = q.call_args.kwargs
        self.assertEqual((kw["limit"], kw["offset"], kw["status"], kw["job"], kw["operation"],
                          kw["target_id"]), (50, 0, "FAIL", "ORD", "create", 2))
        self.assertEqual(kw["since"], datetime(2026, 11, 1))
        body = r.json()["data"] if "data" in r.json() else r.json()
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], 50)


# ---------------------------------------------------------------------------
# Execution attempts pagination / filtering
# ---------------------------------------------------------------------------
class AttemptsPaginationTests(unittest.TestCase):
    def test_filters_and_unknown_count(self):
        attempts = [{"attempt_id": i} for i in range(11, 0, -1)]
        conn = _Conn(rows=[attempts], one=[{"n": 3}])
        page, opened = _run(database.page_execution_attempts, conn, state="OUTCOME_UNKNOWN",
                            open_only=True, job_id=5, target_id=2, limit=10, offset=0)
        self.assertEqual(opened, 1)
        sql, params = conn.executed[0]
        self.assertIn("state = %(state)s", sql)
        self.assertIn("state <> 'FINALIZED'", sql)
        self.assertIn("job_id = %(job_id)s", sql)
        self.assertIn("database_target_id = %(target_id)s", sql)
        self.assertIn("ORDER BY attempt_id DESC LIMIT %(lim)s OFFSET %(off)s", sql)
        self.assertEqual(params["lim"], 11)
        self.assertEqual(len(page["attempts"]), 10)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["next_offset"], 10)
        # unknown outcomes stay visible across ALL pages/filters
        self.assertEqual(page["unknown_count"], 3)
        self.assertIn("state = 'OUTCOME_UNKNOWN'", conn.executed[1][0])
        self.assertNotIn("%(", conn.executed[1][0])

    def test_include_finalized_has_no_state_filter(self):
        conn = _Conn(rows=[[]], one=[{"n": 0}])
        _run(database.page_execution_attempts, conn)
        self.assertNotIn("WHERE", conn.executed[0][0])

    def test_invalid_state_rejected(self):
        with self.assertRaises(DatabaseError):
            _run(database.page_execution_attempts, _Conn(), state="BOGUS")

    def test_missing_ledger_is_unavailable_not_error(self):
        from psycopg.errors import UndefinedTable

        def raise_undefined(sql, _p):
            raise UndefinedTable("missing")
        page, _ = _run(database.page_execution_attempts, _Conn(on_execute=raise_undefined))
        self.assertFalse(page["available"])
        self.assertEqual(page["attempts"], [])

    def test_api_validation_and_forwarding(self):
        client = TestClient(app)
        with patch("api.routers.attempts.page_execution_attempts") as q:
            self.assertEqual(client.get("/api/execution-attempts?limit=501").status_code, 422)
            self.assertEqual(client.get("/api/execution-attempts?offset=-1").status_code, 422)
            q.assert_not_called()
            q.return_value = {"attempts": [], "available": True, "has_more": False, "limit": 20,
                              "offset": 20, "next_offset": None, "previous_offset": 0,
                              "unknown_count": 1}
            r = client.get("/api/execution-attempts?open=true&target_id=2&state=outcome_unknown"
                           "&limit=20&offset=20")
        self.assertEqual(r.status_code, 200)
        kw = q.call_args.kwargs
        self.assertEqual((kw["open_only"], kw["target_id"], kw["state"], kw["limit"], kw["offset"]),
                         (True, 2, "OUTCOME_UNKNOWN", 20, 20))


# ---------------------------------------------------------------------------
# Latest manual result / runtime context / per-job attention
# ---------------------------------------------------------------------------
class RuntimeContextTests(unittest.TestCase):
    def test_latest_manual_run_is_scoped_to_job_and_single_row(self):
        manual = {"job_log_id": 9, "job_id": 7, "last_run_status": "MANUAL_FAIL"}
        conn = _Conn(one=[manual, ("RUNNING",)], rows=[[{"attention_id": 1, "job_id": 7}]])
        ctx, opened = _run(database.get_job_runtime_context, conn, job_id=7)
        self.assertEqual(opened, 1)  # everything on ONE connection
        sql, params = conn.executed[0]
        self.assertEqual(params, {"job_id": 7})
        self.assertIn("l.job_id = %(job_id)s", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertIn("'MANUAL_SUCCESS', 'MANUAL_FAIL', 'MANUAL_OUTCOME_UNKNOWN'", sql)
        self.assertIn("ORDER BY l.job_runtime DESC, l.job_log_id DESC", sql)
        self.assertEqual(ctx["latest_manual_run"], manual)
        self.assertEqual(ctx["open_attempt_state"], "RUNNING")
        self.assertEqual(len(ctx["pending_attention"]), 1)
        # attention query filtered by this job
        att_sql, att_params = conn.executed[2]
        self.assertEqual(att_params["job_id"], 7)
        self.assertEqual(att_params["status"], database.ATTENTION_STATUS_PENDING)
        # last_run_status semantics untouched: no write, no UPDATE
        for s, _p in conn.executed:
            self.assertNotIn("update ", s.lower())

    def test_no_manual_run(self):
        conn = _Conn(one=[None, (None,)], rows=[[]])
        ctx, _ = _run(database.get_job_runtime_context, conn, job_id=7)
        self.assertIsNone(ctx["latest_manual_run"])
        self.assertIsNone(ctx["open_attempt_state"])

    def test_pre_5d_database_degrades(self):
        from psycopg.errors import UndefinedFunction, UndefinedTable

        def handler(sql, _p):
            if "partops_job_open_attempt_state" in sql:
                raise UndefinedFunction("missing")
            if "attention" in sql:
                raise UndefinedTable("missing")
        conn = _Conn(one=[None], on_execute=handler)
        ctx, _ = _run(database.get_job_runtime_context, conn, job_id=7)
        self.assertIsNone(ctx["open_attempt_state"])
        self.assertFalse(ctx["attention_available"])

    def test_runtime_context_api(self):
        client = TestClient(app)
        payload = {"job_id": 7, "latest_manual_run": None, "open_attempt_state": None,
                   "pending_attention": [], "attention_available": True}
        with patch("api.routers.jobs.get_job_runtime_context", return_value=payload) as g:
            r = client.get("/api/jobs/7/runtime-context")
        self.assertEqual(r.status_code, 200)
        g.assert_called_once_with(7)


class PerJobAttentionTests(unittest.TestCase):
    def test_query_filters_by_job(self):
        conn = _Conn(rows=[[{"attention_id": 4, "job_id": 7}]])
        (items, available), _ = _run(database.get_job_pending_attention, conn, job_id=7)
        self.assertTrue(available)
        self.assertEqual(len(items), 1)
        sql, params = conn.executed[0]
        self.assertIn("a.job_id = %(job_id)s::numeric", sql)
        self.assertEqual(params["job_id"], 7)

    def test_global_list_unchanged(self):
        conn = _Conn(rows=[[]])
        _run(database.get_partition_job_attention, conn)
        _sql, params = conn.executed[0]
        self.assertIsNone(params["job_id"])  # global endpoint: no job filter

    def test_api(self):
        client = TestClient(app)
        with patch("api.routers.jobs.get_job_pending_attention",
                   return_value=([{"attention_id": 4}], True)):
            r = client.get("/api/jobs/7/attention")
        self.assertEqual(r.status_code, 200)
        body = r.json()["data"] if "data" in r.json() else r.json()
        self.assertEqual(body["pending_count"], 1)
        self.assertEqual(client.get("/api/jobs/abc/attention").status_code, 422)


# ---------------------------------------------------------------------------
# Dashboard: one connection instead of three
# ---------------------------------------------------------------------------
class DashboardSnapshotTests(unittest.TestCase):
    def test_single_connection_and_section_isolation(self):
        conn = _Conn()
        with patch.object(database, "_query_jobs", return_value=[{"job_id": 1}]), \
                patch.object(database, "_query_recent_logs", side_effect=DatabaseError("logs down")), \
                patch.object(database, "get_database_readiness", return_value={"ok": True}):
            snap, opened = _run(database.get_dashboard_snapshot, conn)
        self.assertEqual(opened, 1)
        self.assertEqual(snap["jobs"], [{"job_id": 1}])
        self.assertEqual(snap["logs_error"], "logs down")
        self.assertEqual(snap["readiness"], {"ok": True})

    def test_connection_failure_reported_for_all_sections(self):
        @contextmanager
        def down(_k):
            raise DatabaseError("Unable to connect")
            yield  # pragma: no cover
        with patch.object(database, "_connection", down), \
                patch.object(database, "_main_db_kwargs", return_value={}):
            snap = database.get_dashboard_snapshot()
        for key in ("jobs", "logs", "readiness"):
            self.assertEqual(snap[f"{key}_error"], "Unable to connect")


# ---------------------------------------------------------------------------
# Frontend: data sources and polling (static checks; no browser)
# ---------------------------------------------------------------------------
def _read(*parts):
    with open(os.path.join(FRONTEND, *parts), encoding="utf-8") as fh:
        return fh.read()


class FrontendSourceTests(unittest.TestCase):
    def test_job_drawer_no_longer_scans_200_history_rows(self):
        src = _read("components", "jobs", "job-detail-panel.tsx")
        self.assertNotRegex(src, r"api\.logs\(\s*200")
        self.assertIn("jobRuntimeContext", src)

    def test_api_client_exposes_paginated_endpoints(self):
        src = _read("lib", "api.ts")
        self.assertIn("/runtime-context", src)
        self.assertIn("/attention", src)
        self.assertRegex(src, r"offset")

    def test_hub_polling_pauses_when_hidden(self):
        src = _read("components", "hub", "hub-context.tsx")
        self.assertIn("visibilitychange", src)
        self.assertIn("document.hidden", src)
        def ms(name):
            m = re.search(name + r"\s*=\s*([\d\s\*]+);", src)
            self.assertIsNotNone(m, name)
            value = 1
            for part in m.group(1).split("*"):
                value *= int(part.strip())
            return value
        self.assertTrue(30_000 <= ms("SCHEDULER_REFRESH_MS") <= 60_000)
        self.assertGreaterEqual(ms("DB_READINESS_REFRESH_MS"), 60_000)
        self.assertGreaterEqual(ms("TARGETS_REFRESH_MS"), 5 * 60_000)
        # health stays on demand (no interval for it)
        self.assertNotIn("setInterval(() => void loadHealth", src)

    def test_history_page_uses_server_pagination(self):
        src = _read("app", "history", "page.tsx")
        self.assertIn("api.logsPage", src)
        self.assertNotRegex(src, r"api\.logs\(\s*200")
        self.assertIn("unknown_count", src)


if __name__ == "__main__":
    unittest.main()
