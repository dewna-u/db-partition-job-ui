"""PartOps database framework manifest: load, validate, snapshot, evaluate.

Single source of truth: ``sql/framework/framework_manifest.json``.

* ``load_manifest`` / ``validate_manifest`` — structural checks (duplicates,
  required fields, signature format). Pure Python.
* ``fetch_catalog_snapshot`` — READ-ONLY catalog query (inside a
  ``SET TRANSACTION READ ONLY`` transaction that is rolled back). Never writes.
* ``evaluate_snapshot`` — compares a snapshot with the manifest and returns
  PASS / WARNING / FAIL results. Pure Python (unit-testable without a DB).

Used by ``scripts/preflight_framework.py`` (DBA preflight), the scheduler start
gate and system readiness. Python 3.9 compatible.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

PASS = "PASS"
WARNING = "WARNING"
FAIL = "FAIL"

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sql", "framework", "framework_manifest.json"
)

KINDS = frozenset({"schema", "table", "sequence", "type", "function", "trigger"})
OWNERSHIPS = frozenset({"partops", "legacy_framework", "external"})
STATUSES = frozenset(
    {"versioned", "existing", "capture_required", "legacy_must_not_run", "external_optional"}
)
SCOPES = frozenset({"api", "scheduler", "manual_run", "migration", "target_execution", "all"})
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MIGRATION_ID_RE = re.compile(r"^[0-9]{8}_[0-9]{2}$")
_ARG_TYPE_RE = re.compile(r"^[a-z][a-z0-9_ .\[\]]*$")


class ManifestError(ValueError):
    """The manifest is structurally invalid."""


# ---------------------------------------------------------------------------
# Manifest loading / validation
# ---------------------------------------------------------------------------
def normalize_identity_args(args: Optional[str]) -> str:
    """Canonical comma+space separated, lower-case type list (oidvectortypes form)."""
    if args is None:
        return ""
    parts = [" ".join(p.split()).lower() for p in str(args).split(",")]
    return ", ".join(p for p in parts if p)


def object_key(obj: Dict[str, Any]) -> str:
    kind = obj.get("kind")
    base = "{}:{}.{}".format(kind, obj.get("schema"), obj.get("name"))
    if kind == "function":
        if not obj.get("signature_known", True):
            return "{}(?)".format(base)
        return "{}({})".format(base, normalize_identity_args(obj.get("identity_args")))
    if kind == "trigger":
        return "{}@{}".format(base, obj.get("table"))
    return base


def validate_manifest(manifest: Dict[str, Any]) -> List[str]:
    """Return a list of problems (empty list = valid)."""
    problems: List[str] = []
    if manifest.get("manifest_format") != 1:
        problems.append("manifest_format must be 1")
    version = str(manifest.get("framework_version") or "")
    if not _MIGRATION_ID_RE.match(version):
        problems.append("framework_version must look like YYYYMMDD_NN (got {!r})".format(version))
    objects = manifest.get("objects")
    if not isinstance(objects, list) or not objects:
        problems.append("objects must be a non-empty list")
        return problems
    seen: Dict[str, int] = {}
    for idx, obj in enumerate(objects):
        where = "objects[{}] {}".format(idx, obj.get("name"))
        for req in ("kind", "schema", "name", "ownership", "status", "definition_in_git",
                    "purpose", "required_by", "required"):
            if req not in obj:
                problems.append("{}: missing '{}'".format(where, req))
        kind = obj.get("kind")
        if kind not in KINDS:
            problems.append("{}: unknown kind {!r}".format(where, kind))
        for ident_field in ("schema", "name"):
            if not _IDENT_RE.match(str(obj.get(ident_field) or "")):
                problems.append("{}: invalid {} identifier".format(where, ident_field))
        if obj.get("ownership") not in OWNERSHIPS:
            problems.append("{}: invalid ownership {!r}".format(where, obj.get("ownership")))
        if obj.get("status") not in STATUSES:
            problems.append("{}: invalid status {!r}".format(where, obj.get("status")))
        for scope in obj.get("required_by") or []:
            if scope not in SCOPES:
                problems.append("{}: invalid required_by scope {!r}".format(where, scope))
        if kind == "function":
            signature_known = obj.get("signature_known", True)
            if signature_known and "identity_args" not in obj:
                problems.append("{}: function needs identity_args (or signature_known=false)".format(where))
            for arg in [a for a in normalize_identity_args(obj.get("identity_args")).split(", ") if a]:
                if not _ARG_TYPE_RE.match(arg):
                    problems.append("{}: invalid argument type {!r}".format(where, arg))
            if obj.get("security") not in (None, "invoker", "definer"):
                problems.append("{}: security must be invoker/definer".format(where))
            if obj.get("security") == "definer" and not obj.get("search_path"):
                problems.append("{}: SECURITY DEFINER requires a fixed search_path expectation".format(where))
        if kind == "trigger" and not obj.get("table"):
            problems.append("{}: trigger needs 'table'".format(where))
        # PartOps-owned objects must be versioned in Git; legacy objects that
        # are not in Git must be marked capture_required / legacy_must_not_run.
        if obj.get("ownership") == "partops":
            if obj.get("definition_in_git") != "yes" or obj.get("status") != "versioned":
                problems.append("{}: PartOps-owned object must be versioned in Git".format(where))
            for src in obj.get("source_files") or []:
                if not os.path.isfile(os.path.join(os.path.dirname(MANIFEST_PATH), "..", "..", src)):
                    problems.append("{}: source file not found: {}".format(where, src))
        if obj.get("definition_in_git") == "no" and obj.get("status") in ("versioned",):
            problems.append("{}: status 'versioned' but definition_in_git='no'".format(where))
        if obj.get("status") == "capture_required" and obj.get("definition_in_git") != "no":
            problems.append("{}: capture_required objects must have definition_in_git='no'".format(where))
        key = object_key(obj)
        if key in seen:
            problems.append("{}: duplicate object {} (also objects[{}])".format(where, key, seen[key]))
        else:
            seen[key] = idx
    return problems


def load_manifest(path: str = MANIFEST_PATH, *, validate: bool = True) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    if validate:
        problems = validate_manifest(manifest)
        if problems:
            raise ManifestError("Invalid framework manifest: " + "; ".join(problems))
    return manifest


def missing_definitions(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Objects whose authoritative definition is NOT in Git (capture required)."""
    return [o for o in manifest["objects"] if o.get("status") == "capture_required"]


def objects_in_scope(manifest: Dict[str, Any], scope: Optional[str]) -> List[Dict[str, Any]]:
    if scope in (None, "all"):
        return list(manifest["objects"])
    return [
        o for o in manifest["objects"]
        if scope in (o.get("required_by") or []) or "all" in (o.get("required_by") or [])
    ]


def expected_roles(manifest: Dict[str, Any]) -> List[str]:
    return [r["name"] for r in manifest.get("roles") or []]


# ---------------------------------------------------------------------------
# Read-only catalog snapshot
# ---------------------------------------------------------------------------
_SNAPSHOT_SQL = """
SELECT json_build_object(
  'database', current_database(),
  'current_user', current_user,
  'server_version', current_setting('server_version'),
  'version_string', version(),
  'roles', (SELECT COALESCE(json_agg(r.rolname ORDER BY r.rolname), '[]'::json)
              FROM pg_roles r WHERE r.rolname::text = ANY(%(roles)s::text[])),
  'schemas', (SELECT COALESCE(json_agg(json_build_object(
                'name', n.nspname, 'owner', pg_get_userbyid(n.nspowner))), '[]'::json)
                FROM pg_namespace n WHERE n.nspname::text = ANY(%(schemas)s::text[])),
  'functions', (SELECT COALESCE(json_agg(json_build_object(
       'schema', n.nspname,
       'name', p.proname,
       'identity_args', oidvectortypes(p.proargtypes),
       'result', pg_get_function_result(p.oid),
       'prokind', p.prokind::text,
       'security_definer', p.prosecdef,
       'owner', pg_get_userbyid(p.proowner),
       'config', p.proconfig,
       'public_execute', (p.proacl IS NULL OR EXISTS (
            SELECT 1 FROM aclexplode(p.proacl) a
             WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')),
       'current_user_execute', has_function_privilege(p.oid, 'EXECUTE'),
       'execute_roles', (SELECT COALESCE(json_object_agg(r.rolname,
            has_function_privilege(r.oid, p.oid, 'EXECUTE')), '{}'::json)
            FROM pg_roles r WHERE r.rolname::text = ANY(%(roles)s::text[]))
     ) ORDER BY n.nspname, p.proname, p.oid), '[]'::json)
     FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname::text = ANY(%(schemas)s::text[]) AND p.proname::text = ANY(%(function_names)s::text[])),
  'relations', (SELECT COALESCE(json_agg(json_build_object(
       'schema', n.nspname,
       'name', c.relname,
       'relkind', c.relkind::text,
       'owner', pg_get_userbyid(c.relowner),
       'columns', (SELECT COALESCE(json_agg(a.attname::text ORDER BY a.attnum), '[]'::json)
                     FROM pg_attribute a
                    WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
       'role_privileges', (SELECT COALESCE(json_object_agg(r.rolname, json_build_object(
            'SELECT', has_table_privilege(r.oid, c.oid, 'SELECT'),
            'INSERT', has_table_privilege(r.oid, c.oid, 'INSERT'),
            'UPDATE', has_table_privilege(r.oid, c.oid, 'UPDATE'),
            'DELETE', has_table_privilege(r.oid, c.oid, 'DELETE'),
            'USAGE', CASE WHEN c.relkind = 'S'
                          THEN has_sequence_privilege(r.oid, c.oid, 'USAGE') END)), '{}'::json)
            FROM pg_roles r WHERE r.rolname::text = ANY(%(roles)s::text[]))
     ) ORDER BY n.nspname, c.relname), '[]'::json)
     FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname::text = ANY(%(schemas)s::text[]) AND c.relname::text = ANY(%(relation_names)s::text[])
      AND c.relkind IN ('r', 'p', 'S', 'v')),
  'types', (SELECT COALESCE(json_agg(json_build_object(
       'schema', n.nspname, 'name', t.typname)), '[]'::json)
     FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname::text = ANY(%(schemas)s::text[]) AND t.typname::text = ANY(%(type_names)s::text[])),
  'triggers', (SELECT COALESCE(json_agg(json_build_object(
       'schema', n.nspname, 'table', c.relname, 'name', tg.tgname,
       'enabled', tg.tgenabled::text)), '[]'::json)
     FROM pg_trigger tg JOIN pg_class c ON c.oid = tg.tgrelid
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT tg.tgisinternal AND n.nspname::text = ANY(%(schemas)s::text[])
      AND tg.tgname::text = ANY(%(trigger_names)s::text[]))
) AS snapshot
"""

_VERSION_SQL = (
    "SELECT max(migration_id)::text AS version FROM {schema}.partops_framework_version"
)

_CONTROL_TARGET_SQL = """
SELECT count(*) FILTER (WHERE t.is_control_database)                       AS control_rows,
       max(t.database_name) FILTER (WHERE t.is_control_database)             AS control_database,
       bool_and(t.is_enabled) FILTER (WHERE t.is_control_database)           AS control_enabled,
       count(*)                                                              AS target_rows
  FROM {schema}.partops_database_target AS t
"""

_LEGACY_RUNNER_SQL = """
SELECT j.jobid AS job_id, j.jobname AS job_name, j.jobenabled AS enabled, s.jstenabled AS step_enabled
  FROM pgagent.pga_job j JOIN pgagent.pga_jobstep s ON s.jstjobid = j.jobid
 WHERE s.jstcode ILIKE '%run_partition_create_jobs%'
    OR s.jstcode ILIKE '%run_partition_drop_jobs%'
"""


def snapshot_parameters(manifest: Dict[str, Any]) -> Dict[str, Any]:
    objs = manifest["objects"]
    return {
        "roles": expected_roles(manifest),
        "schemas": sorted({o["schema"] for o in objs}),
        "function_names": sorted({o["name"] for o in objs if o["kind"] == "function"}),
        "relation_names": sorted({o["name"] for o in objs if o["kind"] in ("table", "sequence")}),
        "type_names": sorted({o["name"] for o in objs if o["kind"] == "type"}),
        "trigger_names": sorted({o["name"] for o in objs if o["kind"] == "trigger"}),
    }


def fetch_catalog_snapshot(conn: Any, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only catalog snapshot.

    Runs inside a transaction started with SET TRANSACTION READ ONLY (the
    server rejects any write) and always rolls it back. The connection must
    be idle (no open transaction) and not in autocommit mode.
    """
    schema = manifest.get("default_schema") or "mubasher_oms"
    if not _IDENT_RE.match(schema):
        raise ManifestError("default_schema must be a plain identifier")
    params = snapshot_parameters(manifest)
    with conn.cursor() as cur:
        try:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(_SNAPSHOT_SQL, params)
            row = cur.fetchone()
            snap = _row_value(row, "snapshot")
            if isinstance(snap, str):
                snap = json.loads(snap)
            snap = dict(snap or {})
            cur.execute("SELECT to_regclass(%(t)s) IS NOT NULL AS ok",
                        {"t": "{}.partops_framework_version".format(schema)})
            has_version = bool(_row_value(cur.fetchone(), "ok"))
            snap["framework_version"] = None
            snap["framework_version_table"] = has_version
            if has_version:
                cur.execute("SAVEPOINT partops_version")
                try:
                    cur.execute(_VERSION_SQL.format(schema=schema))
                    snap["framework_version"] = _row_value(cur.fetchone(), "version")
                    snap["framework_version_readable"] = True
                except Exception:  # noqa: BLE001 — e.g. no SELECT grant
                    cur.execute("ROLLBACK TO SAVEPOINT partops_version")
                    snap["framework_version_readable"] = False
            snap["control_target"] = None
            cur.execute("SELECT to_regclass(%(t)s) IS NOT NULL AS ok",
                        {"t": "{}.partops_database_target".format(schema)})
            if bool(_row_value(cur.fetchone(), "ok")):
                cur.execute("SAVEPOINT partops_targets")
                try:
                    cur.execute(_CONTROL_TARGET_SQL.format(schema=schema))
                    row = cur.fetchone()
                    snap["control_target"] = {
                        "control_rows": _row_value(row, "control_rows", 0),
                        "control_database": _row_value(row, "control_database", 1),
                        "control_enabled": _row_value(row, "control_enabled", 2),
                        "target_rows": _row_value(row, "target_rows", 3),
                    }
                except Exception:  # noqa: BLE001 — e.g. no SELECT grant
                    cur.execute("ROLLBACK TO SAVEPOINT partops_targets")
            cur.execute("SELECT to_regclass('pgagent.pga_jobstep') IS NOT NULL AS ok")
            snap["legacy_runner_steps"] = None
            if bool(_row_value(cur.fetchone(), "ok")):
                cur.execute("SAVEPOINT partops_pgagent")
                try:
                    cur.execute(_LEGACY_RUNNER_SQL)
                    rows = cur.fetchall()
                    snap["legacy_runner_steps"] = [
                        {"job_id": _row_value(r, "job_id", 0), "job_name": _row_value(r, "job_name", 1),
                         "enabled": _row_value(r, "enabled", 2), "step_enabled": _row_value(r, "step_enabled", 3)}
                        for r in rows
                    ]
                except Exception:  # noqa: BLE001 — pgAgent not readable
                    cur.execute("ROLLBACK TO SAVEPOINT partops_pgagent")
        finally:
            conn.rollback()
    return snap


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    status: str
    object: str
    check: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"status": self.status, "object": self.object, "check": self.check, "detail": self.detail}


@dataclass
class FrameworkReport:
    results: List[CheckResult] = field(default_factory=list)
    expected_version: Optional[str] = None
    database_version: Optional[str] = None

    def add(self, status: str, obj: str, check: str, detail: str) -> None:
        self.results.append(CheckResult(status, obj, check, detail))

    @property
    def failures(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def warnings(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == WARNING]

    @property
    def ok(self) -> bool:
        return not self.failures

    def counts(self) -> Dict[str, int]:
        out = {PASS: 0, WARNING: 0, FAIL: 0}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    @property
    def overall(self) -> str:
        if self.failures:
            return FAIL
        if self.warnings:
            return WARNING
        return PASS


def _search_path_of(config: Optional[Sequence[str]]) -> Optional[str]:
    for item in config or []:
        if str(item).lower().startswith("search_path="):
            return str(item).split("=", 1)[1]
    return None


def search_path_is_safe(value: Optional[str]) -> bool:
    """Fixed, schema-qualified path with pg_temp last; no $user / public."""
    if not value:
        return False
    parts = [p.strip().strip('"').lower() for p in value.split(",") if p.strip()]
    if not parts or parts[-1] != "pg_temp":
        return False
    return not any(p in ("$user", "public", '"$user"') for p in parts)


def compare_versions(expected: Optional[str], actual: Optional[str]) -> str:
    """'equal' | 'older' | 'newer' | 'unknown' (YYYYMMDD_NN compares lexically)."""
    if not actual or not _MIGRATION_ID_RE.match(str(actual)):
        return "unknown"
    if not expected:
        return "unknown"
    if actual == expected:
        return "equal"
    return "older" if actual < expected else "newer"


def evaluate_snapshot(
    manifest: Dict[str, Any],
    snapshot: Dict[str, Any],
    *,
    scope: Optional[str] = None,
    check_current_user_execute: bool = False,
    include_version: bool = True,
    include_roles: bool = True,
) -> FrameworkReport:
    """Compare a catalog snapshot with the manifest. Never touches a database.

    ``include_version`` / the control-target registry check apply to the
    CONTROL database only; managed targets other than the control database
    are evaluated with ``scope='target_execution', include_version=False,
    include_roles=False`` (Phase 5C).
    """
    report = FrameworkReport(expected_version=manifest.get("framework_version"))
    present_roles = set(snapshot.get("roles") or [])
    functions = snapshot.get("functions") or []
    relations = {(r["schema"], r["name"]): r for r in snapshot.get("relations") or []}
    types = {(t["schema"], t["name"]) for t in snapshot.get("types") or []}
    triggers = {(t["schema"], t["table"], t["name"]) for t in snapshot.get("triggers") or []}
    schemas = {s["name"] for s in snapshot.get("schemas") or []}

    for role in (manifest.get("roles") or []) if include_roles else []:
        if role["name"] in present_roles:
            report.add(PASS, "role:" + role["name"], "exists", "role present")
        else:
            report.add(FAIL if role.get("required") else WARNING, "role:" + role["name"],
                       "exists", "role not found")

    for obj in objects_in_scope(manifest, scope):
        key = object_key(obj)
        required = bool(obj.get("required"))
        absent_status = FAIL if required else WARNING
        kind = obj["kind"]
        if obj.get("status") == "legacy_must_not_run":
            continue  # handled by the legacy-runner check below
        if kind == "schema":
            if obj["name"] in schemas:
                report.add(PASS, key, "exists", "schema present")
            else:
                report.add(absent_status, key, "exists", "schema missing")
            continue
        if kind == "type":
            if (obj["schema"], obj["name"]) in types:
                report.add(PASS, key, "exists", "type present")
            else:
                report.add(absent_status, key, "exists", "type missing")
            continue
        if kind == "trigger":
            if (obj["schema"], obj["table"], obj["name"]) in triggers:
                report.add(PASS, key, "exists", "trigger present")
            else:
                report.add(absent_status, key, "exists", "trigger missing")
            continue
        if kind in ("table", "sequence"):
            rel = relations.get((obj["schema"], obj["name"]))
            if rel is None:
                report.add(absent_status, key, "exists", "{} missing".format(kind))
                continue
            report.add(PASS, key, "exists", "{} present".format(kind))
            missing_cols = [c for c in obj.get("required_columns") or [] if c not in (rel.get("columns") or [])]
            if missing_cols:
                report.add(FAIL, key, "columns", "missing columns: " + ", ".join(missing_cols))
            elif obj.get("required_columns"):
                report.add(PASS, key, "columns", "all required columns present")
            _check_owner(report, key, obj, rel.get("owner"))
            privs = rel.get("role_privileges") or {}
            for role, needed in (obj.get("table_privileges") or {}).items():
                _check_role_privs(report, key, role, needed, privs, present_roles, expect=True)
            for role, forbidden in (obj.get("forbidden_privileges") or {}).items():
                _check_role_privs(report, key, role, forbidden, privs, present_roles, expect=False)
            for role in obj.get("usage_roles") or []:
                _check_role_privs(report, key, role, ["USAGE"], privs, present_roles, expect=True)
            continue
        if kind == "function":
            _evaluate_function(report, obj, key, functions, present_roles,
                               required, check_current_user_execute)

    if include_version:
        _evaluate_version(report, manifest, snapshot)
        _evaluate_control_target(report, manifest, snapshot)
    _evaluate_legacy_runners(report, snapshot, scope)
    return report


def _evaluate_control_target(report: FrameworkReport, manifest: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
    """Exactly one enabled control-database target, naming THIS database."""
    names = {o["name"] for o in manifest["objects"]}
    if "partops_database_target" not in names:
        return
    info = snapshot.get("control_target")
    obj = "control_target"
    if info is None:
        report.add(FAIL, obj, "registry",
                   "partops_database_target missing or unreadable (apply 20261117_01; grant SELECT)")
        return
    if int(info.get("control_rows") or 0) != 1:
        report.add(FAIL, obj, "registry",
                   "expected exactly one control-database target, found {}".format(info.get("control_rows")))
        return
    if info.get("control_database") != snapshot.get("database"):
        report.add(FAIL, obj, "identity",
                   "control target names {!r} but connected database is {!r}".format(
                       info.get("control_database"), snapshot.get("database")))
        return
    if not info.get("control_enabled"):
        report.add(FAIL, obj, "enabled", "control-database target is disabled")
        return
    report.add(PASS, obj, "registry", "control target = {} ({} target(s) registered)".format(
        info.get("control_database"), info.get("target_rows")))


def _check_owner(report: FrameworkReport, key: str, obj: Dict[str, Any], owner: Optional[str]) -> None:
    expected = obj.get("expected_owner")
    if not expected:
        return
    if owner == expected:
        report.add(PASS, key, "owner", "owner {}".format(owner))
    else:
        strict = obj.get("owner_strict", True) or obj.get("security") == "definer"
        report.add(FAIL if strict else WARNING, key, "owner",
                   "owner is {} (expected {})".format(owner, expected))


def _check_role_privs(report, key, role, privileges, privs, present_roles, *, expect: bool) -> None:
    if role not in present_roles:
        report.add(WARNING, key, "grant:" + role, "role {} not present; cannot verify".format(role))
        return
    have = privs.get(role) or {}
    for priv in privileges:
        value = bool(have.get(priv))
        if expect and not value:
            report.add(FAIL, key, "grant:" + role, "missing {} for {}".format(priv, role))
        elif not expect and value:
            report.add(FAIL, key, "grant:" + role, "unexpected {} for {}".format(priv, role))
        else:
            report.add(PASS, key, "grant:" + role,
                       "{} {} for {}".format("has" if expect else "no", priv, role))


def _evaluate_function(report, obj, key, functions, present_roles, required, check_current_user_execute) -> None:
    same_name = [f for f in functions if f["schema"] == obj["schema"] and f["name"] == obj["name"]]
    absent_status = FAIL if required else WARNING
    if not obj.get("signature_known", True):
        if same_name:
            report.add(WARNING, key, "exists",
                       "present ({} overload(s)); signature not yet known to PartOps — capture & review".format(len(same_name)))
        else:
            report.add(absent_status if required else WARNING, key, "exists",
                       "absent (signature unknown; capture required)")
        return
    want = normalize_identity_args(obj.get("identity_args"))
    match = [f for f in same_name if normalize_identity_args(f.get("identity_args")) == want]
    if not match:
        if same_name:
            sigs = "; ".join("({})".format(f.get("identity_args")) for f in same_name)
            report.add(FAIL if required else WARNING, key, "signature",
                       "wrong signature: found {} expected ({})".format(sigs, want))
        else:
            report.add(absent_status, key, "exists", "function missing")
        return
    fn = match[0]
    report.add(PASS, key, "exists", "function present with expected signature")
    if obj.get("status") == "capture_required":
        report.add(WARNING, key, "review",
                   "definition not in Git — capture and security review pending (not approved)")
    security = obj.get("security")
    if security:
        is_definer = bool(fn.get("security_definer"))
        if (security == "definer") != is_definer:
            report.add(FAIL, key, "security",
                       "is {} (expected {})".format("DEFINER" if is_definer else "INVOKER", security.upper()))
        else:
            report.add(PASS, key, "security", security.upper())
    sp = _search_path_of(fn.get("config"))
    if fn.get("security_definer"):
        if not search_path_is_safe(sp):
            report.add(FAIL, key, "search_path",
                       "SECURITY DEFINER without a safe fixed search_path (got {!r})".format(sp))
        else:
            report.add(PASS, key, "search_path", sp)
    expected_sp = obj.get("search_path")
    if expected_sp and not fn.get("security_definer"):
        if (sp or "").replace(" ", "") == expected_sp.replace(" ", ""):
            report.add(PASS, key, "search_path", sp)
        else:
            report.add(WARNING, key, "search_path", "got {!r} expected {!r}".format(sp, expected_sp))
    _check_owner(report, key, obj, fn.get("owner"))
    if obj.get("public_execute") is False:
        if fn.get("public_execute"):
            report.add(FAIL, key, "public_execute", "PUBLIC can EXECUTE (expected revoked)")
        else:
            report.add(PASS, key, "public_execute", "PUBLIC EXECUTE revoked")
    role_exec = fn.get("execute_roles") or {}
    for role in obj.get("execute_roles") or []:
        if role not in present_roles:
            report.add(WARNING, key, "execute:" + role, "role {} not present; cannot verify".format(role))
        elif role_exec.get(role):
            report.add(PASS, key, "execute:" + role, "EXECUTE granted")
        else:
            report.add(FAIL, key, "execute:" + role, "missing EXECUTE for {}".format(role))
    # Trigger functions are invoked by the trigger mechanism; PostgreSQL does
    # not check EXECUTE for the firing role, so do not require it.
    is_trigger_fn = str(fn.get("result") or "").strip().lower() == "trigger"
    # Internal helpers (Phase 5D) are only called from SECURITY DEFINER
    # functions running as the owner; app roles must NOT execute them.
    is_internal = bool(obj.get("internal"))
    if (check_current_user_execute and not is_trigger_fn and not is_internal
            and not fn.get("current_user_execute")):
        report.add(FAIL, key, "execute:current_user", "connected role cannot EXECUTE")


def _evaluate_version(report: FrameworkReport, manifest: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
    expected = manifest.get("framework_version")
    actual = snapshot.get("framework_version")
    report.database_version = actual
    obj = "framework_version"
    if not snapshot.get("framework_version_table"):
        report.add(FAIL, obj, "version",
                   "database framework version UNKNOWN (partops_framework_version missing); "
                   "application expects {}".format(expected))
        return
    if snapshot.get("framework_version_readable") is False:
        report.add(FAIL, obj, "version", "cannot read partops_framework_version (missing SELECT grant)")
        return
    cmp = compare_versions(expected, actual)
    if cmp == "equal":
        report.add(PASS, obj, "version", "application expects {} — database reports {}".format(expected, actual))
    elif cmp == "older":
        report.add(FAIL, obj, "version",
                   "database framework {} is OUTDATED; application expects {}".format(actual, expected))
    elif cmp == "newer":
        report.add(WARNING, obj, "version",
                   "database framework {} is NEWER than application expectation {}".format(actual, expected))
    else:
        report.add(FAIL, obj, "version", "database framework version unknown ({!r})".format(actual))


def _evaluate_legacy_runners(report: FrameworkReport, snapshot: Dict[str, Any], scope: Optional[str]) -> None:
    steps = snapshot.get("legacy_runner_steps")
    if steps is None:
        report.add(WARNING, "legacy_runners", "pgagent",
                   "pgAgent not readable — verify manually that run_partition_create_jobs / "
                   "run_partition_drop_jobs are not scheduled against this database")
        return
    active = [s for s in steps if s.get("enabled") and s.get("step_enabled")]
    if active:
        names = ", ".join("{}({})".format(s.get("job_name"), s.get("job_id")) for s in active)
        report.add(FAIL if scope == "scheduler" else WARNING, "legacy_runners", "pgagent",
                   "enabled pgAgent job(s) call legacy runners: {} — would duplicate realtime "
                   "CREATE/DROP".format(names))
    else:
        report.add(PASS, "legacy_runners", "pgagent", "no enabled pgAgent step calls legacy runners")


def format_report(report: FrameworkReport, *, only_problems: bool = False) -> str:
    lines = []
    for r in report.results:
        if only_problems and r.status == PASS:
            continue
        lines.append("{:<7} {:<95} {:<22} {}".format(r.status, r.object, r.check, r.detail))
    c = report.counts()
    lines.append("")
    lines.append("RESULT: {}  (PASS={} WARNING={} FAIL={})  expected framework {} / database {}".format(
        report.overall, c[PASS], c[WARNING], c[FAIL], report.expected_version, report.database_version))
    return "\n".join(lines)


def scheduler_gate(manifest: Dict[str, Any], snapshot: Dict[str, Any]) -> FrameworkReport:
    """Checks that must PASS before the scheduler may dispatch anything."""
    return evaluate_snapshot(manifest, snapshot, scope="scheduler", check_current_user_execute=True)


def iter_inventory_rows(manifest: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for o in manifest["objects"]:
        yield {
            "kind": o["kind"],
            "schema": o["schema"],
            "name": o["name"],
            "signature": ("({})".format(o.get("identity_args")) if o["kind"] == "function"
                          and o.get("signature_known", True) else
                          ("(unknown)" if o["kind"] == "function" else "")),
            "ownership": o["ownership"],
            "status": o["status"],
            "in_git": o["definition_in_git"],
            "required_by": ", ".join(o.get("required_by") or []) or "—",
            "required": "yes" if o.get("required") else "no",
            "security": o.get("security") or ("unknown" if o["kind"] == "function" else "—"),
            "owner": o.get("expected_owner") or "—",
            "grants": "; ".join(
                ["EXECUTE→" + r for r in o.get("execute_roles") or []]
                + ["{}→{}".format("/".join(v), k) for k, v in (o.get("table_privileges") or {}).items()]
                + ["USAGE→" + r for r in o.get("usage_roles") or []]
            ) or "—",
            "called_by": ", ".join(o.get("called_by") or []) or "—",
            "source": ", ".join(o.get("source_files") or []) or "NOT IN GIT",
            "purpose": o.get("purpose", ""),
        }
