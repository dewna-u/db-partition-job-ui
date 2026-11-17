"""Phase 5B — database framework manifest, preflight, capture script, version
gate. No database is contacted (catalog snapshots are synthetic)."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import MagicMock, patch

import framework_manifest as fm
from scheduler_backend.models import ScheduledJob, SchedulerConfig
from scheduler_backend.scheduler import PartitionScheduler

ROOT = os.path.dirname(os.path.abspath(__file__))
SQL = os.path.join(ROOT, "sql")
CAPTURE = os.path.join(SQL, "capture_reference_framework.sql")
CAPTURED_DIR = os.path.join(SQL, "framework", "captured")
VERSION_MIG = os.path.join(SQL, "migrations", "20261110_03_partops_framework_version.sql")
EXPECTED_MISSING = {
    "insert_data_to_partition_job_table",
    "cron_to_interval_or_next_run",
    "cron_field_to_int_array",
    "create_any_table_partition",
    "drop_any_table_partition",
}


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _sql_files():
    for dirpath, _dirs, files in os.walk(SQL):
        if os.path.join("sql", "tests") in dirpath:
            continue
        for name in files:
            if name.endswith(".sql"):
                yield os.path.join(dirpath, name)


def _strip_sql(text):
    """Remove comments, dollar-quoted and single-quoted literals, psql meta lines."""
    text = re.sub(r"\$([A-Za-z_]*)\$.*?\$\1\$", " ", text, flags=re.S)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("\\"))


def complete_snapshot(manifest):
    """Synthetic catalog snapshot of a database that satisfies the manifest."""
    roles = fm.expected_roles(manifest)
    snap = {
        "database": "disposable", "current_user": "partition_job_scheduler",
        "server_version": "16", "roles": list(roles), "schemas": [], "functions": [],
        "relations": [], "types": [], "triggers": [],
        "framework_version_table": True, "framework_version_readable": True,
        "framework_version": manifest["framework_version"], "legacy_runner_steps": [],
        # Phase 5C: exactly one enabled control target naming this database.
        "control_target": {"control_rows": 1, "control_database": "disposable",
                           "control_enabled": True, "target_rows": 1},
    }
    for o in manifest["objects"]:
        k = o["kind"]
        if k == "schema":
            snap["schemas"].append({"name": o["name"], "owner": "mubasher_oms"})
        elif k == "type":
            snap["types"].append({"schema": o["schema"], "name": o["name"]})
        elif k == "trigger":
            snap["triggers"].append({"schema": o["schema"], "table": o["table"], "name": o["name"], "enabled": "O"})
        elif k in ("table", "sequence"):
            privs = {r: {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": True, "USAGE": True} for r in roles}
            for role, forbidden in (o.get("forbidden_privileges") or {}).items():
                for priv in forbidden:
                    privs[role][priv] = False
            snap["relations"].append({
                "schema": o["schema"], "name": o["name"], "relkind": "r" if k == "table" else "S",
                "owner": o.get("expected_owner", "mubasher_oms"),
                "columns": list(o.get("required_columns") or []), "role_privileges": privs,
            })
        elif k == "function" and o.get("signature_known", True) and o["status"] != "legacy_must_not_run":
            sp = o.get("search_path")
            snap["functions"].append({
                "schema": o["schema"], "name": o["name"], "identity_args": o.get("identity_args", ""),
                "result": "trigger" if o["name"].startswith("fn_") else "void",
                "security_definer": o.get("security") == "definer",
                "owner": o.get("expected_owner", "mubasher_oms"),
                "config": ["search_path=" + sp] if sp else None,
                "public_execute": False, "current_user_execute": True,
                "execute_roles": {r: True for r in roles},
            })
    return snap


def _fn(snap, name):
    return next(f for f in snap["functions"] if f["name"] == name)


def _rel(snap, name):
    return next(r for r in snap["relations"] if r["name"] == name)


class ManifestValidityTests(unittest.TestCase):
    def setUp(self):
        self.m = fm.load_manifest()

    def test_manifest_is_valid(self):
        self.assertEqual(fm.validate_manifest(self.m), [])

    def test_missing_definitions_exact(self):
        self.assertEqual({o["name"] for o in fm.missing_definitions(self.m)}, EXPECTED_MISSING)

    def test_get_upcoming_is_partops_owned_and_in_git(self):
        o = next(o for o in self.m["objects"] if o["name"] == "get_upcoming_partition_jobs")
        self.assertEqual((o["ownership"], o["definition_in_git"]), ("partops", "yes"))

    def test_duplicate_detection(self):
        bad = copy.deepcopy(self.m)
        bad["objects"].append(copy.deepcopy(bad["objects"][-1]))
        self.assertTrue(any("duplicate object" in p for p in fm.validate_manifest(bad)))

    def test_signature_and_rule_validation(self):
        bad = copy.deepcopy(self.m)
        f = next(o for o in bad["objects"] if o["name"] == "run_partition_job_manual")
        f["identity_args"] = "numeric; DROP TABLE x"
        g = next(o for o in bad["objects"] if o["name"] == "update_data_to_partition_job_table")
        del g["search_path"]
        h = next(o for o in bad["objects"] if o["name"] == "reconcile_missed_partition_job")
        h["definition_in_git"] = "no"
        c = next(o for o in bad["objects"] if o["name"] == "create_any_table_partition")
        c["definition_in_git"] = "yes"
        problems = " | ".join(fm.validate_manifest(bad))
        self.assertIn("invalid argument type", problems)
        self.assertIn("SECURITY DEFINER requires a fixed search_path", problems)
        self.assertIn("PartOps-owned object must be versioned", problems)
        self.assertIn("capture_required objects must have definition_in_git='no'", problems)
        bad2 = copy.deepcopy(self.m)
        bad2["framework_version"] = "5B"
        self.assertTrue(any("framework_version" in p for p in fm.validate_manifest(bad2)))
        bad3 = copy.deepcopy(self.m)
        del next(o for o in bad3["objects"] if o["name"] == "get_upcoming_partition_jobs")["identity_args"]
        self.assertTrue(any("needs identity_args" in p for p in fm.validate_manifest(bad3)))

    def test_normalize_identity_args(self):
        self.assertEqual(fm.normalize_identity_args("Numeric,  TIMESTAMP   without time zone"),
                         "numeric, timestamp without time zone")
        self.assertEqual(fm.normalize_identity_args(""), "")


class ManifestRepositoryConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.m = fm.load_manifest()
        self.created = {}
        for path in _sql_files():
            for name in re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+mubasher_oms\.([a-z_]+)\s*\(",
                                   _read(path), flags=re.I):
                self.created.setdefault(name, set()).add(os.path.relpath(path, ROOT))

    def test_no_capture_required_function_has_a_body_anywhere(self):
        """Guard against invented production bodies."""
        for name in EXPECTED_MISSING:
            self.assertNotIn(name, self.created, "a body for {} appeared in sql/".format(name))

    def test_every_sql_function_is_in_manifest(self):
        names = {o["name"] for o in self.m["objects"] if o["kind"] == "function"}
        for name in self.created:
            self.assertIn(name, names, name)

    def test_partops_functions_defined_in_their_source_files(self):
        for o in self.m["objects"]:
            if o["kind"] == "function" and o["ownership"] == "partops":
                srcs = {s.replace("/", os.sep) for s in o["source_files"]}
                self.assertTrue(srcs & self.created.get(o["name"], set()), o["name"])

    def test_python_callables_are_in_manifest(self):
        import database
        from scheduler_backend.models import SchedulerConfig as C

        names = {o["name"] for o in self.m["objects"]}
        cfg = C()
        for name in (
            database.DEFAULT_PARTITION_JOB_FUNCTION, database.DEFAULT_PARTITION_JOB_UPDATE_FUNCTION,
            database.DEFAULT_MANUAL_RUN_FUNCTION, database.DEFAULT_PARTITION_JOB_TABLE,
            database.DEFAULT_PARTITION_JOB_LOG_TABLE, database.DEFAULT_PARTITION_JOB_SEQUENCE,
            database.DEFAULT_PARTITION_JOB_ATTENTION_TABLE, database.DEFAULT_ATTENTION_RECOVERY_FUNCTION,
            database.DEFAULT_ATTENTION_ACKNOWLEDGE_FUNCTION, cfg.upcoming_function,
            cfg.scheduled_run_function, cfg.reconcile_function, cfg.attention_table,
        ):
            self.assertIn(name, names, name)

    def test_insert_signature_matches_python_contract(self):
        import database

        o = next(o for o in self.m["objects"] if o["name"] == "insert_data_to_partition_job_table")
        self.assertEqual(fm.normalize_identity_args(o["identity_args"]),
                         fm.normalize_identity_args(database.INSERT_FUNCTION_ARGUMENT_TYPES))
        sql = database.build_create_partition_job_sql()
        for pname in o["parameter_names"]:
            self.assertIn(pname + " ", sql)

    def test_captured_directory_has_placeholders_not_bodies(self):
        files = set(os.listdir(CAPTURED_DIR))
        for name in EXPECTED_MISSING:
            self.assertIn(name + ".CAPTURE_REQUIRED.md", files)
            self.assertNotIn(name + ".sql", files)
            self.assertIn("NOT in Git", _read(os.path.join(CAPTURED_DIR, name + ".CAPTURE_REQUIRED.md")))

    def test_inventory_check_command(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "framework_inventory", os.path.join(ROOT, "scripts", "framework_inventory.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(["--check"]), 0)
        text = _read(mod.INVENTORY_PATH)
        self.assertIn("PARTOPS-OWNED OBJECTS", text)
        self.assertIn("EXISTING / LEGACY DATABASE FRAMEWORK OBJECTS", text)

    def test_framework_version_matches_version_migration(self):
        mig_dir = os.path.join(SQL, "migrations")
        recorded = {}
        for f in sorted(os.listdir(mig_dir)):
            if f.endswith(".sql"):
                for a, b in re.findall(r"\('(\d{8}_\d{2})', '(\d{8}_\d{2})'", _read(os.path.join(mig_dir, f))):
                    self.assertEqual(a, b)
                    self.assertTrue(f.startswith(a), (f, a))  # a migration records its OWN id
                    recorded[a] = f
        self.assertIn("20261110_03", recorded)
        self.assertEqual(max(recorded), self.m["framework_version"])
        migrations = sorted(f[:11] for f in os.listdir(mig_dir) if re.match(r"\d{8}_\d{2}_", f))
        self.assertEqual(migrations[-1], self.m["framework_version"])

    def test_plan_ids_covered_by_integration_sql(self):
        plan = _read(os.path.join(SQL, "framework", "INTEGRATION_TEST_PLAN.md"))
        sql = _read(os.path.join(SQL, "tests", "disposable", "40_integration_full.sql"))
        ids = set(re.findall(r"^\| ((?:C|D|S|SEC|T)\d) \|", plan, flags=re.M))
        self.assertGreaterEqual(len(ids), 20)
        lock_sql = _read(os.path.join(SQL, "tests", "disposable", "30_target_lock.sql"))
        for i in ids:
            if i == "S4":  # needs a second concurrent session → 30_target_lock.sql (T8)
                self.assertIn("T8 OK", lock_sql)
                continue
            self.assertIn("ITEST {} OK".format(i), sql, i)


class SearchPathAndVersionTests(unittest.TestCase):
    def test_search_path_safety(self):
        self.assertTrue(fm.search_path_is_safe("pg_catalog, mubasher_oms, pg_temp"))
        for bad in (None, "", "mubasher_oms", "public, pg_temp", '"$user", public',
                    "pg_temp, mubasher_oms", "mubasher_oms, public, pg_temp"):
            self.assertFalse(fm.search_path_is_safe(bad), bad)

    def test_compare_versions(self):
        self.assertEqual(fm.compare_versions("20261110_03", "20261110_03"), "equal")
        self.assertEqual(fm.compare_versions("20261110_03", "20261013_02"), "older")
        self.assertEqual(fm.compare_versions("20261110_03", "20261201_01"), "newer")
        self.assertEqual(fm.compare_versions("20261110_03", None), "unknown")
        self.assertEqual(fm.compare_versions("20261110_03", "v5"), "unknown")


class PreflightEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.m = fm.load_manifest()
        self.snap = complete_snapshot(self.m)

    def _eval(self, snap, scope=None, cue=False):
        return fm.evaluate_snapshot(self.m, snap, scope=scope, check_current_user_execute=cue)

    def _has(self, report, status, obj_part, check_part, detail_part=""):
        return any(r.status == status and obj_part in r.object and check_part in r.check
                   and detail_part in r.detail for r in report.results)

    def test_complete_database_passes_except_review_warnings(self):
        report = self._eval(self.snap, cue=True)
        self.assertEqual(report.failures, [])
        # Present-but-uncaptured legacy functions are never "approved".
        self.assertTrue(self._has(report, fm.WARNING, "cron_to_interval_or_next_run", "review", "not approved"))
        self.assertTrue(self._has(report, fm.PASS, "framework_version", "version"))

    def test_missing_function(self):
        self.snap["functions"] = [f for f in self.snap["functions"] if f["name"] != "create_any_table_partition"]
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "create_any_table_partition", "exists", "missing"))

    def test_wrong_signature(self):
        _fn(self.snap, "cron_to_interval_or_next_run")["identity_args"] = "character varying"
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "cron_to_interval_or_next_run", "signature", "wrong signature"))

    def test_wrong_security_mode(self):
        _fn(self.snap, "update_data_to_partition_job_table")["security_definer"] = False
        _fn(self.snap, "run_partition_job_manual")["security_definer"] = True
        r = self._eval(self.snap)
        self.assertTrue(self._has(r, fm.FAIL, "update_data_to_partition_job_table", "security"))
        self.assertTrue(self._has(r, fm.FAIL, "run_partition_job_manual", "security"))

    def test_unsafe_search_path(self):
        _fn(self.snap, "update_data_to_partition_job_table")["config"] = ["search_path=public, mubasher_oms"]
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "update_data_to_partition_job_table", "search_path"))
        _fn(self.snap, "update_data_to_partition_job_table")["config"] = None
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "update_data_to_partition_job_table", "search_path"))

    def test_wrong_owner_strict_and_lenient(self):
        _fn(self.snap, "update_data_to_partition_job_table")["owner"] = "postgres"
        _fn(self.snap, "run_partition_job_scheduled")["owner"] = "postgres"
        r = self._eval(self.snap)
        self.assertTrue(self._has(r, fm.FAIL, "update_data_to_partition_job_table", "owner"))
        self.assertTrue(self._has(r, fm.WARNING, "run_partition_job_scheduled", "owner"))

    def test_public_execute_and_missing_grant(self):
        _fn(self.snap, "reconcile_missed_partition_job")["public_execute"] = True
        _fn(self.snap, "run_partition_job_attention_recovery")["execute_roles"]["partition_job_ui"] = False
        r = self._eval(self.snap)
        self.assertTrue(self._has(r, fm.FAIL, "reconcile_missed_partition_job", "public_execute"))
        self.assertTrue(self._has(r, fm.FAIL, "run_partition_job_attention_recovery", "execute:partition_job_ui"))

    def test_missing_table_column_and_forbidden_privilege(self):
        _rel(self.snap, "partitioning_job_table_log")["columns"].remove("execution_duration_ms")
        _rel(self.snap, "partition_job_attention")["role_privileges"]["partition_job_ui"]["DELETE"] = True
        r = self._eval(self.snap)
        self.assertTrue(self._has(r, fm.FAIL, "partitioning_job_table_log", "columns", "execution_duration_ms"))
        self.assertTrue(self._has(r, fm.FAIL, "partition_job_attention", "grant:partition_job_ui", "unexpected DELETE"))
        self.snap["relations"] = [x for x in self.snap["relations"] if x["name"] != "partition_job_attention"]
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "partition_job_attention", "exists", "missing"))

    def test_missing_phase5a_function(self):
        self.snap["functions"] = [f for f in self.snap["functions"] if f["name"] != "reconcile_missed_partition_job"]
        r = self._eval(self.snap, scope="scheduler")
        self.assertFalse(r.ok)
        self.assertTrue(self._has(r, fm.FAIL, "reconcile_missed_partition_job", "exists"))

    def test_version_states(self):
        self.snap["framework_version"] = "20261013_02"
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "framework_version", "version", "OUTDATED"))
        self.snap["framework_version"] = "20271231_01"
        self.assertTrue(self._has(self._eval(self.snap), fm.WARNING, "framework_version", "version", "NEWER"))
        self.snap["framework_version_table"] = False
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "framework_version", "version", "UNKNOWN"))
        self.snap["framework_version_table"] = True
        self.snap["framework_version_readable"] = False
        self.assertTrue(self._has(self._eval(self.snap), fm.FAIL, "framework_version", "version", "SELECT"))

    def test_role_absent_and_legacy_runners(self):
        self.snap["roles"].remove("partition_job_ui")
        r = self._eval(self.snap)
        self.assertTrue(self._has(r, fm.FAIL, "role:partition_job_ui", "exists"))
        self.snap["legacy_runner_steps"] = [{"job_id": 7, "job_name": "legacy", "enabled": True, "step_enabled": True}]
        self.assertTrue(self._has(self._eval(self.snap, scope="scheduler"), fm.FAIL, "legacy_runners", "pgagent"))
        self.assertTrue(self._has(self._eval(self.snap), fm.WARNING, "legacy_runners", "pgagent"))
        self.snap["legacy_runner_steps"] = None
        self.assertTrue(self._has(self._eval(self.snap), fm.WARNING, "legacy_runners", "pgagent", "not readable"))

    def test_scheduler_gate_requires_current_user_execute_but_not_for_triggers(self):
        _fn(self.snap, "fn_partition_job_attention_guard")["current_user_execute"] = False
        self.assertTrue(fm.scheduler_gate(self.m, self.snap).ok)
        _fn(self.snap, "create_any_table_partition")["current_user_execute"] = False
        report = fm.scheduler_gate(self.m, self.snap)
        self.assertFalse(report.ok)
        self.assertTrue(self._has(report, fm.FAIL, "create_any_table_partition", "execute:current_user"))

    def test_scopes_filter_objects(self):
        self.snap["functions"] = [f for f in self.snap["functions"] if f["name"] != "insert_data_to_partition_job_table"]
        self.assertTrue(fm.scheduler_gate(self.m, self.snap).ok)  # API-only object
        self.assertFalse(self._eval(self.snap, scope="api").ok)

    def test_format_report(self):
        text = fm.format_report(self._eval(self.snap), only_problems=True)
        self.assertIn("RESULT:", text)
        self.assertNotIn("\nPASS ", text)


class PreflightCliTests(unittest.TestCase):
    def _cli(self, snap, *extra):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "preflight_framework", os.path.join(ROOT, "scripts", "preflight_framework.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(snap, handle)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = mod.main(["--snapshot-in", path] + list(extra))
        return rc, out.getvalue()

    def test_exit_codes_and_json(self):
        m = fm.load_manifest()
        snap = complete_snapshot(m)
        rc, out = self._cli(snap, "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["overall"], "WARNING")
        self.assertEqual(data["expected_framework_version"], m["framework_version"])
        rc, _ = self._cli(snap, "--strict")
        self.assertEqual(rc, 1)
        snap["functions"] = [f for f in snap["functions"] if f["name"] != "run_partition_job_scheduled"]
        rc, out = self._cli(snap)
        self.assertEqual(rc, 2)
        self.assertIn("RESULT: FAIL", out)

    def test_live_mode_requires_expected_database(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "preflight_framework", os.path.join(ROOT, "scripts", "preflight_framework.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main([]), 3)


class CaptureScriptReadOnlyTests(unittest.TestCase):
    FORBIDDEN = (
        "CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "GRANT", "REVOKE",
        "CALL", "COPY", "VACUUM", "REINDEX", "CLUSTER", "LOCK", "COMMENT", "SECURITY", "COMMIT",
        "MERGE", "REFRESH", "IMPORT", "DO", "EXECUTE", "PREPARE", "LISTEN", "NOTIFY",
        "NEXTVAL", "SETVAL", "SET_CONFIG", "PG_ADVISORY_LOCK", "PG_TRY_ADVISORY",
        "PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND", "LO_IMPORT", "LO_EXPORT", "DBLINK",
        "PG_RELOAD_CONF", "PG_SWITCH_WAL",
    )

    def _check(self, path):
        stripped = _strip_sql(_read(path)).upper()
        for word in self.FORBIDDEN:
            self.assertIsNone(re.search(r"\b{}\b".format(word), stripped), "{} in {}".format(word, path))
        self.assertNotRegex(stripped, r"\bFOR\s+(NO\s+KEY\s+)?UPDATE\b")
        self.assertNotRegex(stripped, r"MUBASHER_OMS\.[A-Z_]+\s*\(")  # never calls framework routines

    def test_new_capture_script_is_read_only(self):
        self._check(CAPTURE)
        text = _read(CAPTURE)
        self.assertIn("READ-ONLY", text[:600])
        stripped = _strip_sql(text)
        statements = [s.strip() for s in stripped.split(";") if s.strip()]
        self.assertEqual(statements[0].upper(), "BEGIN TRANSACTION READ ONLY")
        self.assertEqual(statements[-1].upper(), "ROLLBACK")

    def test_legacy_capture_and_preflight_are_read_only(self):
        self._check(os.path.join(SQL, "capture_reference_framework_functions.sql"))
        self._check(os.path.join(SQL, "preflight_realtime_database.sql"))

    def test_capture_lists_every_manifest_function(self):
        text = _read(CAPTURE)
        for o in fm.load_manifest()["objects"]:
            if o["kind"] == "function" and o["schema"] == "mubasher_oms":
                self.assertIn("'{}'".format(o["name"]), text, o["name"])

    def test_capture_extracts_definitions_and_metadata(self):
        text = _read(CAPTURE)
        for needle in ("pg_get_functiondef", "pg_get_triggerdef", "pg_get_viewdef", "prosecdef",
                       "proconfig", "aclexplode", "oidvectortypes", "pg_get_constraintdef",
                       "pg_get_indexdef", "md5(pg_get_functiondef"):
            self.assertIn(needle, text)


class SnapshotReadOnlyTests(unittest.TestCase):
    def test_snapshot_uses_read_only_transaction_and_rolls_back(self):
        executed = []
        cur = MagicMock()
        cur.execute.side_effect = lambda sql, params=None: executed.append(sql)
        cur.fetchone.side_effect = [
            {"snapshot": {"roles": [], "functions": []}}, {"ok": True}, {"version": "20261110_03"},
            # Phase 5C: control-target registry lookup.
            {"ok": True}, {"control_rows": 1, "control_database": "db", "control_enabled": True,
                           "target_rows": 2},
            {"ok": False},
        ]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        snap = fm.fetch_catalog_snapshot(conn, fm.load_manifest())
        self.assertEqual(executed[0], "SET TRANSACTION READ ONLY")
        conn.rollback.assert_called_once()
        self.assertEqual(snap["framework_version"], "20261110_03")
        joined = _strip_sql("\n".join(executed)).upper()
        for word in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "GRANT", "COMMIT"):
            self.assertIsNone(re.search(r"\b{}\b".format(word), joined), word)


class FrameworkVersionMigrationTests(unittest.TestCase):
    def test_version_migration_shape(self):
        text = _read(VERSION_MIG)
        self.assertIn("BEGIN;", text)
        self.assertIn("COMMIT;", text)
        self.assertIn("ON CONFLICT (migration_id) DO NOTHING", text)
        self.assertIn("REVOKE ALL ON TABLE mubasher_oms.partops_framework_version FROM PUBLIC", text)
        self.assertIn("OWNER TO mubasher_oms", text)
        grants = re.findall(r"GRANT ([A-Z, ]+) ON mubasher_oms\.partops_framework_version", text)
        self.assertEqual(set(grants), {"SELECT"})
        prereq = text.index("Framework version not recorded")
        self.assertLess(prereq, text.index("INSERT INTO mubasher_oms.partops_framework_version"))
        for fn in ("reconcile_missed_partition_job", "run_partition_job_attention_recovery",
                   "partition_job_config_param_allowed"):
            self.assertIn(fn, text[:prereq])

    def test_application_never_writes_version(self):
        for name in ("database.py", "framework_manifest.py", "system_readiness.py",
                     os.path.join("scheduler_backend", "scheduler_database.py"),
                     os.path.join("scheduler_backend", "scheduler.py")):
            text = _read(os.path.join(ROOT, name))
            self.assertNotRegex(text, r"(?i)(insert\s+into|update|delete\s+from)\s+\S*partops_framework_version")


class SchedulerFrameworkGateTests(unittest.TestCase):
    T = datetime(2026, 11, 10, 2, 0)

    def _report(self, ok):
        m = fm.load_manifest()
        snap = complete_snapshot(m)
        if not ok:
            snap["functions"] = [f for f in snap["functions"] if f["name"] != "cron_to_interval_or_next_run"]
        return fm.scheduler_gate(m, snap)

    def _run(self, gate_side_effect, seconds=0.6):
        cfg = SchedulerConfig(reconcile_seconds=60, framework_check_enabled=True)
        scheduler = PartitionScheduler(cfg)
        calls = {"execute": 0, "reconcile": 0, "gate": 0, "fetch": 0}

        def gate(c):
            calls["gate"] += 1
            return gate_side_effect(calls["gate"])

        def fetch(c):
            calls["fetch"] += 1
            return [ScheduledJob(5, self.T, -1.0, "J", True)]

        def execute(c, job_id, expected):
            calls["execute"] += 1
            return {"status": "EXECUTED", "job_id": job_id, "message": "", "is_create": True,
                    "next_run_time": datetime(2026, 11, 11, 2, 0)}

        def reconcile(*a, **k):
            calls["reconcile"] += 1
            return {"status": "MISSED_RESCHEDULED", "job_id": 5, "message": "", "is_create": True,
                    "next_run_time": None}

        async def scenario():
            task = asyncio.create_task(scheduler.run())
            await asyncio.sleep(seconds)
            scheduler.request_shutdown()
            await asyncio.wait_for(task, timeout=3)

        from database_targets import DatabaseTarget

        control = DatabaseTarget(target_id=1, target_key="db01", display_name="DB-01",
                                 database_name="disposable", is_control_database=True)
        with patch("scheduler_backend.scheduler.check_framework_compatibility", side_effect=gate), \
                patch("scheduler_backend.scheduler.fetch_control_target", return_value=control), \
                patch("scheduler_backend.scheduler.remote_recover_attempts", return_value=[]), \
                patch("scheduler_backend.scheduler.fetch_targets", return_value={}), \
                patch("scheduler_backend.scheduler.count_unknown_outcomes", return_value=0), \
                patch("scheduler_backend.scheduler.fetch_upcoming_jobs", side_effect=fetch), \
                patch("scheduler_backend.scheduler.execute_scheduled_job", side_effect=execute), \
                patch("scheduler_backend.scheduler.reconcile_occurrence", side_effect=reconcile), \
                patch("scheduler_backend.scheduler.count_failed_jobs", return_value=0), \
                patch("scheduler_backend.scheduler.count_pending_attention", return_value=0):
            asyncio.run(scenario())
        return scheduler, calls

    def test_blocked_gate_dispatches_nothing_and_does_not_spin(self):
        scheduler, calls = self._run(lambda n: self._report(ok=False))
        self.assertEqual(calls["execute"], 0)
        self.assertEqual(calls["reconcile"], 0)
        self.assertEqual(calls["gate"], 1)  # rate-limited (recheck 300 s)
        self.assertLessEqual(calls["fetch"], 2)
        self.assertEqual(scheduler.status.extra["framework_status"], "blocked")
        self.assertTrue(any("cron_to_interval_or_next_run" in f
                            for f in scheduler.status.extra["framework_failures"]))

    def test_unknown_gate_blocks(self):
        def boom(n):
            raise RuntimeError("db down")
        scheduler, calls = self._run(boom)
        self.assertEqual(calls["execute"], 0)
        self.assertEqual(scheduler.status.extra["framework_status"], "unknown")

    def test_passing_gate_allows_dispatch(self):
        scheduler, calls = self._run(lambda n: self._report(ok=True))
        self.assertEqual(calls["execute"], 1)
        self.assertEqual(scheduler.status.extra["framework_status"], "ready")
        self.assertEqual(scheduler.status.extra["framework_database_version"],
                         fm.load_manifest()["framework_version"])
        self.assertEqual(scheduler.status.extra["control_target_key"], "db01")

    def test_loader_always_enables_gate(self):
        from scheduler_backend import scheduler_database

        with patch.object(scheduler_database, "_load_env"):
            cfg = scheduler_database.load_scheduler_config()
        self.assertTrue(cfg.framework_check_enabled)
        with patch.dict(os.environ, {"PARTITION_SCHEDULER_FRAMEWORK_RECHECK_SECONDS": "5"}), \
                patch.object(scheduler_database, "_load_env"):
            with self.assertRaises(RuntimeError):
                scheduler_database.load_scheduler_config()


class ReadinessFrameworkCheckTests(unittest.TestCase):
    def test_readiness_maps_report(self):
        import system_readiness as sr

        m = fm.load_manifest()
        snap = complete_snapshot(m)
        snap["functions"] = [f for f in snap["functions"] if f["name"] != "insert_data_to_partition_job_table"]
        conn = MagicMock()

        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *a):
                return False

        with patch.object(sr, "_connection", return_value=_Ctx()), \
                patch("framework_manifest.fetch_catalog_snapshot", return_value=snap):
            checks = sr.check_framework_manifest()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], sr.STATUS_FAILED)
        self.assertIn("insert_data_to_partition_job_table", json.dumps(checks[0]["details"]))
        self.assertIn("application expects " + m["framework_version"], checks[0]["summary"])


class DisposableHarnessStaticTests(unittest.TestCase):
    def test_harness_is_disposable_only(self):
        text = _read(os.path.join(ROOT, "scripts", "disposable_db", "run_disposable_db_test.sh"))
        self.assertIn("--i-understand-this-is-disposable", text)
        self.assertIn("initdb", text)
        self.assertIn("listen_addresses=127.0.0.1", text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn("FRAMEWORK DEFINITION NOT AVAILABLE", text)
        self.assertNotRegex(text, r"(?i)ssh |systemctl|aws |datadog")
        self.assertLess(text.index('if [ "$FULL" = "1" ]; then\n  step "10 full integration"'), len(text))


if __name__ == "__main__":
    unittest.main()
