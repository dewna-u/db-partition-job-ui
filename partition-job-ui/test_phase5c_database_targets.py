"""Phase 5C — hub-local multi-database foundation (unit + SQL contract tests).

No database, network, AWS or Datadog access: connections, secrets and the
catalog snapshot are all faked. The SQL behaviour itself is exercised on a
throwaway cluster by scripts/disposable_db/run_disposable_db_test.sh
(sql/tests/disposable/15_pre5c_jobs.sql + 25_phase5c_targets.sql).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import db_credentials
import framework_manifest as fm
from api.main import app
from api.routers import jobs as jobs_router
from database import DatabaseConflictError
from database_targets import (
    AUTH_FAILED,
    CONFIG_ERROR,
    DISABLED,
    FRAMEWORK_INCOMPATIBLE,
    HEALTHY,
    HEALTHY_WITH_WARNINGS,
    TARGET_EXECUTION_SCOPE,
    UNREACHABLE,
    WRONG_DATABASE,
    DatabaseTarget,
    TargetConfigError,
    TargetConnectionError,
    check_target_health,
    control_target,
    load_targets_from_rows,
    metric_target_tag,
    open_target_connection,
    resolve_credentials_for_target,
)
from scheduler_backend.models import ScheduledJob, SchedulerConfig, target_table_resource_key
from scheduler_backend.retry_policy import ACTION_CLEARED, OccurrenceRetryTracker
from scheduler_backend.scheduler import PartitionScheduler
from test_framework_phase5b import complete_snapshot
from validators import ValidationError

ROOT = os.path.dirname(os.path.abspath(__file__))
MIG = os.path.join(ROOT, "sql", "migrations")
MIG01 = os.path.join(MIG, "20261117_01_partops_database_target.sql")
MIG02 = os.path.join(MIG, "20261117_02_target_aware_executors.sql")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def ctl(**kw) -> DatabaseTarget:
    base = dict(target_id=1, target_key="db01", display_name="DB-01",
                database_name="partops", is_control_database=True)
    base.update(kw)
    return DatabaseTarget(**base)


def remote(**kw) -> DatabaseTarget:
    base = dict(target_id=2, target_key="db02", display_name="DB-02",
                database_name="oms_db02", host="db02.internal", port=5432,
                credential_provider="aws", secret_id="partops/db02",
                aws_region="eu-west-1")
    base.update(kw)
    return DatabaseTarget(**base)


class FakeConn:
    def __init__(self, dbname: str):
        self.dbname = dbname
        self.closed = False
        self.rolled_back = False

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                self.sql = sql

            def fetchone(self):
                return (conn.dbname,)

        return Cur()

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class AuthError(Exception):
    sqlstate = "28P01"


# ---------------------------------------------------------------------------
# 1 / 2 Migration: default target, backfill, NOT NULL, FK, no orphans
# ---------------------------------------------------------------------------
class MigrationContractTests(unittest.TestCase):
    def setUp(self):
        self.sql = _read(MIG01)

    def test_registry_has_no_credential_columns(self):
        table = self.sql[self.sql.index("CREATE TABLE IF NOT EXISTS mubasher_oms.partops_database_target"):]
        table = table[: table.index(chr(10) + ");")]
        self.assertNotRegex(table, r"(?i)\b(password|passwd|access_key|secret_value|secret_string)\b")
        self.assertIn("secret_id", table)  # identifier only
        self.assertIn("partops_database_target_secret_ck", table)

    def test_default_control_target_inserted_from_current_database(self):
        self.assertRegex(self.sql, r"'db01'")
        self.assertIn("current_database()", self.sql)
        self.assertRegex(self.sql, r"(?s)UNIQUE INDEX IF NOT EXISTS partops_database_target_one_control_uq.*WHERE is_control_database")

    def test_backfill_order_then_not_null_then_fk(self):
        s = self.sql
        add = s.index("ADD COLUMN IF NOT EXISTS database_target_id")
        backfill = s.index("SET database_target_id = mubasher_oms.partops_control_target_id()")
        orphan = s.index("orphan", backfill)
        not_null = s.index("SET NOT NULL", backfill)
        fk = s.index("partitioning_job_table_target_fk", not_null)
        self.assertLess(add, backfill)
        self.assertLess(backfill, orphan)
        self.assertLess(orphan, not_null)
        self.assertLess(not_null, fk)
        self.assertIn("SET DEFAULT mubasher_oms.partops_control_target_id()", s)
        self.assertIn("ON DELETE RESTRICT", s)

    def test_db_config_para_untouched_and_registry_readonly_for_apps(self):
        self.assertNotRegex(self.sql, r"(?i)update\s+mubasher_oms\.partitioning_job_table\s+set\s+[^;]*db_config_para")
        grants = re.findall(r"GRANT ([A-Z, ]+) ON (?:TABLE )?mubasher_oms\.partops_database_target TO", self.sql)
        self.assertTrue(grants)
        self.assertEqual({g.strip() for g in grants}, {"SELECT"})

    def test_postgres_regex_repetition_limits(self):
        # PostgreSQL rejects bounds > 255 at evaluation time ({1,512} broke inserts).
        for sql in (self.sql, _read(MIG02)):
            for lo, hi in re.findall(r"\{(\d+),(\d+)\}", sql):
                self.assertLessEqual(int(hi), 255)


# ---------------------------------------------------------------------------
# SQL remote-target guards (20261117_02)
# ---------------------------------------------------------------------------
class TargetAwareExecutorContractTests(unittest.TestCase):
    def setUp(self):
        self.sql = _read(MIG02)

    def _body(self, name):
        start = self.sql.index("CREATE OR REPLACE FUNCTION mubasher_oms." + name)
        end = self.sql.index("$function$;", self.sql.index("AS $function$", start))
        return self.sql[start:end]

    def test_get_upcoming_filters_to_control_target(self):
        self.assertIn("j.database_target_id = mubasher_oms.partops_control_target_id()",
                      self._body("get_upcoming_partition_jobs"))

    def test_scheduled_and_reconcile_skip_remote_before_any_work(self):
        for fn in ("run_partition_job_scheduled", "reconcile_missed_partition_job"):
            body = self._body(fn)
            guard = body.index("SKIPPED_REMOTE_TARGET")
            self.assertIn("IS DISTINCT FROM mubasher_oms.partops_control_target_id()", body)
            # After the job-id lock (control-DB row lock only) but before the
            # target-table lock, any UPDATE, the worker call and attention rows.
            for later in ("87421032", "create_any_table_partition",
                          "INSERT INTO mubasher_oms.partition_job_attention",
                          "UPDATE mubasher_oms.partitioning_job_table"):
                if later in body:
                    self.assertLess(guard, body.index(later), (fn, later))

    def test_manual_returns_remote_target_without_execution(self):
        body = self._body("run_partition_job_manual")
        guard = body.index("MANUAL_REMOTE_TARGET")
        for later in ("create_any_table_partition", "drop_any_table_partition", "87421032",
                      "INSERT INTO mubasher_oms.partitioning_job_table_log"):
            if later in body:
                self.assertLess(guard, body.index(later))

    def test_legacy_worker_bodies_not_defined(self):
        for path in (MIG01, MIG02):
            text = _read(path)
            for legacy in ("create_any_table_partition", "drop_any_table_partition",
                           "cron_to_interval_or_next_run", "insert_data_to_partition_job_table"):
                self.assertNotRegex(text, r"(?i)CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\s+mubasher_oms\." + legacy)

    def test_no_test_stub_in_production_migrations_or_manifest(self):
        for name in sorted(os.listdir(MIG)):
            if name.endswith(".sql"):
                self.assertNotRegex(_read(os.path.join(MIG, name)), r"(?i)\bstub\b", name)
        manifest = _read(os.path.join(ROOT, "sql", "framework", "framework_manifest.json"))
        self.assertNotRegex(manifest, r"(?i)\bstub\b")

    def test_version_recorded(self):
        self.assertIn("'20261117_02'", self.sql)
        # Later phases (5D: 20261117_04) raise the expected version.
        self.assertGreaterEqual(fm.load_manifest()["framework_version"], "20261117_02")


# ---------------------------------------------------------------------------
# 3 / 4 Model, compatibility, two targets
# ---------------------------------------------------------------------------
class TargetModelTests(unittest.TestCase):
    def test_single_target_control_inherits_env(self):
        t = ctl()
        self.assertIsNone(t.host)
        self.assertTrue(t.to_public_dict()["executable"])

    def test_two_targets_load_and_control(self):
        rows = [
            {"target_id": 1, "target_key": "db01", "display_name": "DB-01", "database_name": "partops",
             "credential_provider": "inherit", "is_enabled": True, "is_control_database": True},
            {"target_id": 2, "target_key": "db02", "display_name": "DB-02", "database_name": "oms2",
             "host": "h2", "port": 5432, "credential_provider": "aws", "secret_id": "s/db02",
             "aws_region": "eu-west-1", "is_enabled": True, "is_control_database": False,
             "max_concurrent_jobs": None},
        ]
        targets = load_targets_from_rows(rows)
        self.assertEqual(set(targets), {1, 2})
        self.assertEqual(control_target(targets).target_key, "db01")
        self.assertEqual(targets[2].max_concurrent_jobs, 1)  # default 1
        self.assertTrue(targets[2].to_public_dict()["executable"])  # Phase 5D: remote execution

    def test_two_control_rows_is_no_control(self):
        targets = {1: ctl(), 2: ctl(target_id=2, target_key="db02")}
        self.assertIsNone(control_target(targets))

    def test_validation_fail_closed(self):
        bad = [
            dict(target_key="DB01"),
            dict(target_key="db02", is_control_database=False),            # remote w/o host
            dict(credential_provider="aws"),                                # aws w/o secret
            dict(credential_provider="aws", secret_id='{"password":"x"}'),  # secret value
            dict(max_concurrent_jobs=0),
            dict(port=70000),
            dict(ssl_mode="bogus"),
        ]
        for kw in bad:
            with self.assertRaises(TargetConfigError, msg=kw):
                ctl(**kw)

    def test_occurrence_identity_includes_target(self):
        t = datetime(2026, 11, 17, 2, 0)
        a = ScheduledJob(5, t, 0.0, "J", True, database_target_id=1)
        b = dataclasses.replace(a, database_target_id=2)
        self.assertEqual(a.key, (5, t))  # retry/queue key unchanged (job_id unique)
        self.assertEqual(a.occurrence_id, (1, 5, t))
        self.assertNotEqual(a.occurrence_id, b.occurrence_id)
        legacy = ScheduledJob(5, t, 0.0, "J", True)
        self.assertIsNone(legacy.database_target_id)

    def test_target_lock_identity_includes_target(self):
        k1 = target_table_resource_key(1, "mubasher_oms", "orders")
        k2 = target_table_resource_key(2, "mubasher_oms", "orders")
        self.assertNotEqual(k1, k2)
        self.assertEqual(k1, target_table_resource_key(1, "mubasher_oms", "orders"))
        self.assertIn("1", str(k1))

    def test_job_select_templates_tolerate_pre5c_schema(self):
        text = _read(os.path.join(ROOT, "database.py"))
        self.assertGreaterEqual(text.count("(to_jsonb(j) ->> 'database_target_id')::integer AS database_target_id"), 4)


# ---------------------------------------------------------------------------
# Secrets: never serialized; per-target credentials and cache isolation
# ---------------------------------------------------------------------------
class SecretsTests(unittest.TestCase):
    def setUp(self):
        db_credentials._reset_for_tests()
        self.addCleanup(db_credentials._reset_for_tests)

    def test_public_dict_and_repr_hide_secret_identifier(self):
        t = remote()
        dumped = json.dumps(t.to_public_dict())
        self.assertNotIn("partops/db02", dumped)
        self.assertTrue(t.to_public_dict()["secret_configured"])
        self.assertNotIn("partops/db02", repr(t))
        self.assertNotIn("secret_id", t.to_public_dict())

    def test_per_target_aws_secrets_and_cache_isolation(self):
        secrets = {"partops/db02": ("u2", "p2"), "partops/db03": ("u3", "p3")}
        calls = []

        def fake_get(secret_id=None, region=None):
            calls.append(secret_id)
            return secrets[secret_id]

        t2 = remote()
        t3 = remote(target_id=3, target_key="db03", secret_id="partops/db03")
        with patch.dict(os.environ, {"DB_PASSWORD": "must-not-be-used"}), \
                patch("db_credentials.get_database_secret", side_effect=fake_get):
            self.assertEqual(resolve_credentials_for_target(t2), {"user": "u2", "password": "p2"})
            self.assertEqual(resolve_credentials_for_target(t3), {"user": "u3", "password": "p3"})
            resolve_credentials_for_target(t2)  # cached
            self.assertEqual(calls, ["partops/db02", "partops/db03"])
            keys = db_credentials.cached_secret_keys()
            self.assertEqual(len(keys), 2)
            # Invalidate db02 only.
            from database_targets import invalidate_target_credentials
            invalidate_target_credentials(t2)
            self.assertEqual([k[2] for k in db_credentials.cached_secret_keys()], ["partops/db03"])

    def test_aws_failure_is_fail_closed(self):
        with patch("db_credentials.get_database_secret",
                   side_effect=db_credentials.DatabaseCredentialError("denied")), \
                patch.dict(os.environ, {"DB_PASSWORD": "fallback"}):
            with self.assertRaises(TargetConnectionError) as cm:
                with open_target_connection(remote(), "t", connect=MagicMock()):
                    pass
        self.assertEqual(cm.exception.kind, CONFIG_ERROR)
        self.assertNotIn("fallback", str(cm.exception))

    def test_env_provider_requires_target_specific_vars(self):
        t = remote(credential_provider="env", secret_id=None)
        with patch.dict(os.environ, {"DB_USER": "x", "DB_PASSWORD": "y"}, clear=False):
            os.environ.pop("PARTOPS_TARGET_DB02_DB_USER", None)
            os.environ.pop("PARTOPS_TARGET_DB02_DB_PASSWORD", None)
            with self.assertRaises(db_credentials.DatabaseCredentialError):
                resolve_credentials_for_target(t)
        with patch.dict(os.environ, {"PARTOPS_TARGET_DB02_DB_USER": "u",
                                     "PARTOPS_TARGET_DB02_DB_PASSWORD": "p"}):
            self.assertEqual(resolve_credentials_for_target(t), {"user": "u", "password": "p"})


# ---------------------------------------------------------------------------
# Connections: routing, identity, auth retry, disabled, unreachable
# ---------------------------------------------------------------------------
class TargetConnectionTests(unittest.TestCase):
    ENV = {"PARTOPS_TARGET_DB02_DB_USER": "u2", "PARTOPS_TARGET_DB02_DB_PASSWORD": "p2",
           "DB_HOST": "control.local", "DB_PORT": "5444", "DB_USER": "cu", "DB_PASSWORD": "cp",
           "PARTITION_DB_SECRET_PROVIDER": "env"}

    def _env_remote(self, **kw):
        return remote(credential_provider="env", secret_id=None, **kw)

    def test_routing_control_vs_target(self):
        seen = []

        def connect(**kw):
            seen.append(kw)
            return FakeConn(kw["dbname"])

        with patch.dict(os.environ, self.ENV):
            with open_target_connection(ctl(), "t", connect=connect) as c1:
                self.assertEqual(c1.dbname, "partops")
            with open_target_connection(self._env_remote(), "t", connect=connect) as c2:
                self.assertEqual(c2.dbname, "oms_db02")
        self.assertEqual((seen[0]["host"], seen[0]["port"], seen[0]["user"]), ("control.local", 5444, "cu"))
        self.assertEqual((seen[1]["host"], seen[1]["port"], seen[1]["user"]), ("db02.internal", 5432, "u2"))
        self.assertEqual(seen[1]["application_name"], "partops-target-db02")
        self.assertLessEqual(seen[1]["connect_timeout"], 30)

    def test_wrong_database_is_refused_and_closed(self):
        conn = FakeConn("some_other_db")
        with patch.dict(os.environ, self.ENV):
            with self.assertRaises(TargetConnectionError) as cm:
                with open_target_connection(self._env_remote(), "t", connect=lambda **k: conn):
                    self.fail("must not yield")
        self.assertEqual(cm.exception.kind, WRONG_DATABASE)
        self.assertTrue(conn.closed)

    def test_disabled_never_connects(self):
        connect = MagicMock()
        with self.assertRaises(TargetConnectionError) as cm:
            with open_target_connection(self._env_remote(is_enabled=False), "t", connect=connect):
                pass
        self.assertEqual(cm.exception.kind, DISABLED)
        connect.assert_not_called()

    def test_unreachable(self):
        with patch.dict(os.environ, self.ENV):
            with self.assertRaises(TargetConnectionError) as cm:
                with open_target_connection(self._env_remote(), "t",
                                            connect=MagicMock(side_effect=OSError("refused"))):
                    pass
        self.assertEqual(cm.exception.kind, UNREACHABLE)

    def test_auth_failure_invalidates_only_that_target_and_retries_once(self):
        connect = MagicMock(side_effect=[AuthError("bad password"), FakeConn("oms_db02")])
        t = remote()
        with patch("database_targets.resolve_target_credentials",
                   return_value={"user": "u", "password": "p"}), \
                patch("database_targets.invalidate_database_secret_cache") as inv:
            with open_target_connection(t, "t", connect=connect) as c:
                self.assertEqual(c.dbname, "oms_db02")
        self.assertEqual(connect.call_count, 2)
        inv.assert_called_once_with(db_credentials.secret_cache_key("partops/db02", "eu-west-1"))

    def test_auth_failure_twice_is_auth_failed(self):
        connect = MagicMock(side_effect=[AuthError("x"), AuthError("x")])
        with patch("database_targets.resolve_target_credentials",
                   return_value={"user": "u", "password": "SECRETPW"}), \
                patch("database_targets.invalidate_database_secret_cache"):
            with self.assertRaises(TargetConnectionError) as cm:
                with open_target_connection(remote(), "t", connect=connect):
                    pass
        self.assertEqual(cm.exception.kind, AUTH_FAILED)
        self.assertNotIn("SECRETPW", str(cm.exception))


# ---------------------------------------------------------------------------
# Health / per-target preflight / isolation
# ---------------------------------------------------------------------------
class TargetHealthTests(unittest.TestCase):
    ENV = TargetConnectionTests.ENV

    def setUp(self):
        self.manifest = fm.load_manifest()

    def test_target_execution_scope_lists_workers(self):
        scoped = [o for o in self.manifest["objects"]
                  if TARGET_EXECUTION_SCOPE in (o.get("required_by") or [])]
        names = {o.get("name") for o in scoped}
        self.assertIn("create_any_table_partition", names)
        self.assertIn("drop_any_table_partition", names)

    def test_remote_target_healthy_without_version_or_registry(self):
        snap = complete_snapshot(self.manifest)
        snap.pop("version", None)
        snap.pop("control_target", None)
        with patch.dict(os.environ, self.ENV):
            h = check_target_health(remote(credential_provider="env", secret_id=None),
                                    connect=lambda **k: FakeConn("oms_db02"),
                                    manifest=self.manifest, snapshot_fn=lambda c, m: snap)
        # Only WARNINGs: legacy workers are not captured in Git (Phase 5B.1
        # deferred) — never a FAIL for missing version/registry on a target.
        self.assertEqual(h["state"], HEALTHY_WITH_WARNINGS, h)
        self.assertEqual(h["framework"]["counts"]["FAIL"], 0)
        self.assertIsNone(h["framework"]["expected_version"])

    def test_remote_target_missing_worker_is_incompatible(self):
        snap = complete_snapshot(self.manifest)
        snap["functions"] = [f for f in snap["functions"] if f["name"] != "create_any_table_partition"]
        with patch.dict(os.environ, self.ENV):
            h = check_target_health(remote(credential_provider="env", secret_id=None),
                                    connect=lambda **k: FakeConn("oms_db02"),
                                    manifest=self.manifest, snapshot_fn=lambda c, m: snap)
        self.assertEqual(h["state"], FRAMEWORK_INCOMPATIBLE)

    def test_control_target_uses_full_scheduler_scope(self):
        snap = complete_snapshot(self.manifest)
        snap["control_target"] = {"control_rows": 0, "control_database": None,
                                  "control_enabled": None, "target_rows": 0}
        with patch.dict(os.environ, self.ENV):
            h = check_target_health(ctl(), connect=lambda **k: FakeConn("partops"),
                                    manifest=self.manifest, snapshot_fn=lambda c, m: snap)
        self.assertEqual(h["state"], FRAMEWORK_INCOMPATIBLE)

    def test_privilege_error_is_incompatible_not_crash(self):
        class InsufficientPrivilege(Exception):
            pass

        def snap_fn(c, m):
            raise InsufficientPrivilege("permission denied for schema mubasher_oms")

        with patch.dict(os.environ, self.ENV):
            h = check_target_health(remote(credential_provider="env", secret_id=None),
                                    connect=lambda **k: FakeConn("oms_db02"),
                                    manifest=self.manifest, snapshot_fn=snap_fn)
        self.assertEqual(h["state"], FRAMEWORK_INCOMPATIBLE)

    def test_readiness_isolates_targets(self):
        import system_readiness

        t1, t2 = ctl(), remote()
        t3 = remote(target_id=3, target_key="db03", is_enabled=False)

        def health(target, **kw):
            if target.target_key == "db02":
                raise RuntimeError("boom")
            if target.target_key == "db03":
                return {"state": DISABLED, "detail": "off"}
            return {"state": HEALTHY, "detail": "ok"}

        invalid = [{"target_id": 9, "target_key": "db09", "error": "bad"}]
        with patch("database.list_database_targets", return_value=([t1, t2, t3], invalid, True)), \
                patch("database_targets.check_target_health", side_effect=health), \
                patch("observability.emit_target_health"):
            checks = system_readiness.check_database_targets()
        by_id = {c["key"]: c["status"] for c in checks}
        self.assertEqual(by_id["target_db01"], system_readiness.STATUS_READY)
        self.assertEqual(by_id["target_db02"], system_readiness.STATUS_FAILED)
        self.assertEqual(by_id["target_db03"], system_readiness.STATUS_NOT_REQUIRED)
        self.assertEqual(by_id["target_invalid_9"], system_readiness.STATUS_FAILED)
        self.assertNotIn("partops/db02", json.dumps(checks, default=str))

    def test_readiness_single_database_mode(self):
        import system_readiness

        with patch("database.list_database_targets", return_value=([], [], False)):
            checks = system_readiness.check_database_targets()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], system_readiness.STATUS_WARNING)


# ---------------------------------------------------------------------------
# Metrics: low-cardinality target tag only
# ---------------------------------------------------------------------------
class TargetMetricsTests(unittest.TestCase):
    def _capture(self, fn):
        sent = []
        with patch.dict(os.environ, {"PARTOPS_DATADOG_ENABLED": "true",
                                     "PARTOPS_ENVIRONMENT": "development"}), \
                patch("observability._send", side_effect=lambda p: sent.append(p.decode())):
            fn()
        return "\n".join(sent)

    def test_target_tag_is_registry_key_only(self):
        import observability

        out = self._capture(lambda: (
            observability.emit_job_execution(status="SKIPPED_REMOTE_TARGET", is_create=True,
                                             duration_ms=5, target="db02"),
            observability.emit_target_health(target="db02", state="UNREACHABLE"),
            observability.emit_target_health(target="db01", state="HEALTHY_WITH_WARNINGS"),
        ))
        self.assertIn("target:db02", out)
        self.assertIn("partops.target.healthy:0|g", out)
        self.assertIn("partops.target.healthy:1|g", out)
        self.assertIn("status:skipped_remote_target", out)
        for forbidden in ("job_id", "table", "schema:", "host", "secret", "db02.internal"):
            self.assertNotIn(forbidden, out)

    def test_metric_target_tag(self):
        self.assertEqual(metric_target_tag(remote()), "db02")
        self.assertIsNone(metric_target_tag(None))


# ---------------------------------------------------------------------------
# API: target validation, dangerous change, Phase 5D refusal, read-only routes
# ---------------------------------------------------------------------------
class JobTargetValidationTests(unittest.TestCase):
    def _targets(self, invalid=None, available=True):
        return ([ctl(), remote(), remote(target_id=3, target_key="db03", is_enabled=False)],
                invalid or [], available)

    def _resolve(self, requested, current, confirm=False, health="HEALTHY_WITH_WARNINGS",
                 open_state=None, **kw):
        with patch("api.routers.jobs.list_database_targets", return_value=self._targets(**kw)) as m,                 patch("api.routers.jobs.check_target_health", return_value={"state": health, "detail": "x"}),                 patch("api.routers.jobs.get_job_open_attempt_state", return_value=open_state):
            result = jobs_router.resolve_job_target(requested, current, confirm, job_id=7)
        return result, m

    def test_no_target_requested_does_not_touch_db(self):
        with patch("api.routers.jobs.list_database_targets") as m:
            self.assertEqual(jobs_router.resolve_job_target(None, 1, False), 1)
            self.assertIsNone(jobs_router.resolve_job_target(None, None, False))
        m.assert_not_called()

    def test_control_target_accepted(self):
        self.assertEqual(self._resolve(1, None)[0], 1)
        self.assertEqual(self._resolve(1, 1)[0], 1)

    def test_unknown_disabled_misconfigured(self):
        with self.assertRaisesRegex(ValidationError, "does not exist"):
            self._resolve(42, None)
        with self.assertRaisesRegex(ValidationError, "disabled"):
            self._resolve(3, None)
        with self.assertRaisesRegex(ValidationError, "misconfigured"):
            self._resolve(9, None, invalid=[{"target_id": 9, "target_key": "db09", "error": "x"}])
        with self.assertRaisesRegex(ValidationError, "not configured"):
            self._resolve(1, None, available=False)

    def test_change_requires_confirmation_and_no_open_attempt(self):
        # Phase 5D lifted the Phase 5C 409: moving a job is allowed with
        # confirm_dangerous=true, a healthy target and no open remote attempt.
        with self.assertRaisesRegex(ValidationError, "confirm_dangerous=true: database target changes from db01 to db02"):
            self._resolve(2, 1, confirm=False)
        self.assertEqual(self._resolve(2, 1, confirm=True)[0], 2)
        with self.assertRaisesRegex(DatabaseConflictError, "DISPATCHING"):
            self._resolve(2, 1, confirm=True, open_state="DISPATCHING")
        with self.assertRaisesRegex(DatabaseConflictError, "OUTCOME_UNKNOWN"):
            self._resolve(1, 2, confirm=True, open_state="OUTCOME_UNKNOWN")

    def test_new_job_on_remote_target_requires_ready_target(self):
        self.assertEqual(self._resolve(2, None)[0], 2)
        with self.assertRaisesRegex(DatabaseConflictError, "not ready"):
            self._resolve(2, None, health="UNREACHABLE")
        with self.assertRaisesRegex(DatabaseConflictError, "not ready"):
            self._resolve(2, None, health="FRAMEWORK_INCOMPATIBLE")


class TargetsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_list_targets_never_exposes_secrets(self):
        with patch("api.routers.targets.list_database_targets",
                   return_value=([ctl(), remote()], [], True)):
            r = self.client.get("/api/targets")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("partops/db02", r.text)
        self.assertNotRegex(r.text, r"(?i)password")
        keys = [t["target_key"] for t in r.json()["data"]["targets"]] if "data" in r.json() \
            else [t["target_key"] for t in r.json()["targets"]]
        self.assertEqual(keys, ["db01", "db02"])

    def test_unknown_target_404_and_health_isolated(self):
        with patch("api.routers.targets.get_database_target", return_value=None):
            self.assertEqual(self.client.get("/api/targets/77").status_code, 404)
        with patch("api.routers.targets.get_database_target", return_value=remote()), \
                patch("api.routers.targets.check_target_health",
                      return_value={"state": UNREACHABLE, "detail": "x"}), \
                patch("api.routers.targets.emit_target_health") as emit:
            r = self.client.get("/api/targets/2/health")
        self.assertEqual(r.status_code, 200)
        emit.assert_called_once_with(target="db02", state=UNREACHABLE)

    def test_no_registry_write_routes(self):
        for method in ("post", "put", "patch", "delete"):
            r = getattr(self.client, method)("/api/targets/1")
            self.assertEqual(r.status_code, 405, method)


# ---------------------------------------------------------------------------
# Scheduler foundation
# ---------------------------------------------------------------------------
class SchedulerTargetTests(unittest.TestCase):
    def test_skipped_remote_target_clears_retry_state(self):
        tracker = OccurrenceRetryTracker()
        key = (5, datetime(2026, 11, 17, 2, 0))
        tracker.record_outcome(key, "FAILED_DATABASE", now_mono=0.0)
        self.assertIn(key, tracker)
        decision = tracker.record_outcome(key, "SKIPPED_REMOTE_TARGET", now_mono=1.0)
        self.assertEqual(decision.action, ACTION_CLEARED)
        self.assertNotIn(key, tracker)

    def _run_gate(self, control_side_effect):
        m = fm.load_manifest()
        report = fm.scheduler_gate(m, complete_snapshot(m))
        cfg = SchedulerConfig(reconcile_seconds=60, framework_check_enabled=True)
        scheduler = PartitionScheduler(cfg)
        executed = []

        async def scenario():
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(0.6)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        with patch("scheduler_backend.scheduler.check_framework_compatibility", return_value=report), \
                patch("scheduler_backend.scheduler.fetch_control_target", side_effect=control_side_effect), \
                patch("scheduler_backend.scheduler.remote_recover_attempts", return_value=[]), \
                patch("scheduler_backend.scheduler.fetch_targets", return_value={}), \
                patch("scheduler_backend.scheduler.count_unknown_outcomes", return_value=0), \
                patch("scheduler_backend.scheduler.fetch_upcoming_jobs",
                      return_value=[ScheduledJob(5, datetime(2026, 11, 17, 2, 0), -1.0, "J", True)]), \
                patch("scheduler_backend.scheduler.execute_scheduled_job",
                      side_effect=lambda *a: executed.append(a) or {
                          "status": "EXECUTED", "job_id": 5, "message": "", "is_create": True,
                          "next_run_time": datetime(2026, 11, 18, 2, 0)}), \
                patch("scheduler_backend.scheduler.reconcile_occurrence"), \
                patch("scheduler_backend.scheduler.count_failed_jobs", return_value=0), \
                patch("scheduler_backend.scheduler.count_pending_attention", return_value=0):
            asyncio.run(scenario())
        return scheduler, executed

    def test_control_target_lookup_failure_blocks_dispatch(self):
        scheduler, executed = self._run_gate(TargetConfigError("no control"))
        self.assertEqual(executed, [])
        self.assertEqual(scheduler.status.extra["framework_status"], "blocked")
        self.assertIn("control target lookup failed", scheduler.status.extra["framework_failures"])

    def test_control_target_annotates_status(self):
        scheduler, executed = self._run_gate(lambda c: ctl(max_concurrent_jobs=1))
        self.assertEqual(len(executed), 1)
        self.assertEqual(scheduler.status.extra["control_target_key"], "db01")
        self.assertEqual(scheduler.status.extra["control_target_max_concurrent_jobs"], 1)

    def _fetch_control(self, rows, actual="partops"):
        from scheduler_backend import scheduler_database

        @__import__("contextlib").contextmanager
        def fake_open(purpose):
            yield MagicMock()

        with patch.object(scheduler_database, "open_connection", fake_open), \
                patch("database_targets.fetch_target_rows", return_value=rows), \
                patch.object(scheduler_database, "read_current_database", return_value=actual):
            return scheduler_database.fetch_control_target(SchedulerConfig())

    def test_fetch_control_target_fail_closed(self):
        row = {"target_id": 1, "target_key": "db01", "display_name": "x", "database_name": "partops",
               "credential_provider": "inherit", "is_enabled": True, "is_control_database": True}
        self.assertEqual(self._fetch_control([row]).target_key, "db01")
        with self.assertRaises(TargetConfigError):
            self._fetch_control([row], actual="other_db")
        with self.assertRaises(TargetConfigError):
            self._fetch_control([dict(row, is_enabled=False)])
        with self.assertRaises(TargetConfigError):
            self._fetch_control([])


if __name__ == "__main__":
    unittest.main()
