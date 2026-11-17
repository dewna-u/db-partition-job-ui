"""Hub-local database targets (Phase 5C).

One PartOps installation per hub manages a few databases (normally two). There
is NO global multi-hub control plane.

* CONTROL DATABASE — the existing PartOps database (``DB_*`` env / the
  scheduler's ``.env.realtime``). Holds jobs, the target registry
  (``mubasher_oms.partops_database_target``), attention items, history and the
  framework version. Reached through the existing control connection helpers
  (``database._connection`` / ``scheduler_database.open_connection``).
* MANAGED TARGETS — databases whose partitions PartOps maintains. The control
  database is itself the default target (``is_control_database``). Reached only
  through :func:`open_target_connection`, which resolves per-target
  credentials, verifies ``current_database()`` and never pools connections.

Nothing here writes to any database. Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, List, Optional

from db_credentials import (
    DatabaseCredentialError,
    invalidate_database_secret_cache,
    is_authentication_error,
    resolve_database_credentials,
    resolve_target_credentials,
    secret_cache_key,
)

logger = logging.getLogger(__name__)

TARGET_TABLE = "partops_database_target"
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DBNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,254}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9/_+=.@:-]{1,512}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
PROVIDERS = frozenset({"inherit", "env", "aws"})
SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})

# Health states (one per target; never shared between targets).
HEALTHY = "HEALTHY"
HEALTHY_WITH_WARNINGS = "HEALTHY_WITH_WARNINGS"
DISABLED = "DISABLED"
CONFIG_ERROR = "CONFIG_ERROR"
UNREACHABLE = "UNREACHABLE"
AUTH_FAILED = "AUTH_FAILED"
WRONG_DATABASE = "WRONG_DATABASE"
FRAMEWORK_INCOMPATIBLE = "FRAMEWORK_INCOMPATIBLE"
HEALTH_STATES = (
    HEALTHY, HEALTHY_WITH_WARNINGS, DISABLED, CONFIG_ERROR, UNREACHABLE,
    AUTH_FAILED, WRONG_DATABASE, FRAMEWORK_INCOMPATIBLE,
)

# Manifest scope evaluated on a managed target that is NOT the control DB:
# only what must exist in the database that receives the DDL.
TARGET_EXECUTION_SCOPE = "target_execution"


class TargetConfigError(ValueError):
    """A registry row is invalid (fail closed; never connect with it)."""


class TargetConnectionError(RuntimeError):
    """Connecting to a target failed. ``kind`` is a health state."""

    def __init__(self, target_key: str, kind: str, message: str) -> None:
        self.target_key = target_key
        self.kind = kind
        super().__init__("target {}: {}".format(target_key, message))


@dataclass(frozen=True)
class DatabaseTarget:
    """Validated connection identity of one managed database (no secrets)."""

    target_id: int
    target_key: str
    display_name: str
    database_name: str
    host: Optional[str] = None
    port: Optional[int] = None
    credential_provider: str = "inherit"
    secret_id: Optional[str] = None
    aws_region: Optional[str] = None
    ssl_mode: Optional[str] = None
    environment: Optional[str] = None
    is_enabled: bool = True
    is_control_database: bool = False
    max_concurrent_jobs: int = 1

    def __post_init__(self) -> None:
        problems = validate_target_fields(self)
        if problems:
            raise TargetConfigError(
                "Invalid database target {!r}: {}".format(self.target_key, "; ".join(problems))
            )

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "DatabaseTarget":
        def opt(name: str) -> Optional[str]:
            value = row.get(name)
            text = str(value).strip() if value is not None else ""
            return text or None

        port = row.get("port")
        return cls(
            target_id=int(row["target_id"]),
            target_key=str(row["target_key"]),
            display_name=str(row.get("display_name") or row["target_key"]),
            database_name=str(row["database_name"]),
            host=opt("host"),
            port=int(port) if port is not None else None,
            credential_provider=str(row.get("credential_provider") or "inherit"),
            secret_id=opt("secret_id"),
            aws_region=opt("aws_region"),
            ssl_mode=opt("ssl_mode"),
            environment=opt("environment"),
            is_enabled=bool(row.get("is_enabled", True)),
            is_control_database=bool(row.get("is_control_database", False)),
            max_concurrent_jobs=int(row.get("max_concurrent_jobs") or 1),
        )

    def to_public_dict(self) -> Dict[str, Any]:
        """API/status view. Never contains passwords; secret IDENTIFIER hidden."""
        return {
            "target_id": self.target_id,
            "target_key": self.target_key,
            "display_name": self.display_name,
            "database_name": self.database_name,
            "host": self.host,
            "port": self.port,
            "credential_provider": self.credential_provider,
            "secret_configured": bool(self.secret_id),
            "aws_region": self.aws_region,
            "ssl_mode": self.ssl_mode,
            "environment": self.environment,
            "is_enabled": self.is_enabled,
            "is_control_database": self.is_control_database,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            # Phase 5D: every enabled target is executable (db01 via the local
            # executor, managed targets via the remote orchestrator).
            "executable": self.is_enabled,
        }

    def __repr__(self) -> str:  # never print secret identifiers in logs
        return "DatabaseTarget(id={}, key={!r}, db={!r}, control={}, enabled={})".format(
            self.target_id, self.target_key, self.database_name,
            self.is_control_database, self.is_enabled)


def validate_target_fields(t: DatabaseTarget) -> List[str]:
    problems: List[str] = []
    if not isinstance(t.target_id, int) or t.target_id < 1:
        problems.append("target_id must be a positive integer")
    if not _KEY_RE.match(t.target_key or ""):
        problems.append("target_key must match ^[a-z][a-z0-9_]{0,31}$")
    if not _DBNAME_RE.match(t.database_name or ""):
        problems.append("database_name invalid")
    if t.host is not None and not _HOST_RE.match(t.host):
        problems.append("host invalid")
    if t.port is not None and not (1 <= int(t.port) <= 65535):
        problems.append("port out of range")
    if not t.is_control_database and not t.host:
        problems.append("a non-control target needs host")
    if t.credential_provider not in PROVIDERS:
        problems.append("credential_provider must be inherit/env/aws")
    if t.credential_provider == "aws" and not t.secret_id:
        problems.append("aws targets need secret_id")
    if t.secret_id is not None and not _SECRET_RE.match(t.secret_id):
        problems.append("secret_id must be a secret name/ARN identifier")
    if t.aws_region is not None and not _REGION_RE.match(t.aws_region):
        problems.append("aws_region invalid")
    if t.ssl_mode is not None and t.ssl_mode not in SSL_MODES:
        problems.append("ssl_mode invalid")
    if not (1 <= int(t.max_concurrent_jobs) <= 10):
        problems.append("max_concurrent_jobs must be 1..10")
    return problems


def metric_target_tag(target: Optional[DatabaseTarget]) -> Optional[str]:
    """Low-cardinality metric tag value (the registry key, e.g. 'db01')."""
    if target is None:
        return None
    return target.target_key if _KEY_RE.match(target.target_key) else None


# ---------------------------------------------------------------------------
# Registry access (CONTROL database; read-only)
# ---------------------------------------------------------------------------
TARGETS_SQL = """
SELECT target_id, target_key, display_name, host, port, database_name,
       credential_provider, secret_id, aws_region, ssl_mode, environment,
       is_enabled, is_control_database, max_concurrent_jobs
  FROM {schema}.partops_database_target
 ORDER BY target_id
"""


def load_targets_from_rows(rows: List[Dict[str, Any]]) -> Dict[int, DatabaseTarget]:
    """Validate registry rows. One invalid row raises (fail closed for it only
    when callers load individually; listing reports it as CONFIG_ERROR)."""
    out: Dict[int, DatabaseTarget] = {}
    for row in rows:
        target = DatabaseTarget.from_row(row)
        out[target.target_id] = target
    return out


def fetch_target_rows(conn: Any, schema: str = "mubasher_oms") -> List[Dict[str, Any]]:
    """Read registry rows over an existing CONTROL connection (read-only)."""
    if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", schema):
        raise TargetConfigError("schema must be a plain identifier")
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(TARGETS_SQL.format(schema=schema))
        return [dict(r) for r in cur.fetchall()]


def control_target(targets: Dict[int, DatabaseTarget]) -> Optional[DatabaseTarget]:
    controls = [t for t in targets.values() if t.is_control_database]
    return controls[0] if len(controls) == 1 else None


# ---------------------------------------------------------------------------
# Per-target credentials and connections (MANAGED target databases)
# ---------------------------------------------------------------------------
def _env_name(target: DatabaseTarget, suffix: str) -> str:
    return "PARTOPS_TARGET_{}_{}".format(target.target_key.upper(), suffix)


def resolve_credentials_for_target(target: DatabaseTarget) -> Dict[str, str]:
    """Credentials for exactly this target. AWS fails closed (no env fallback)."""
    if target.credential_provider == "inherit":
        return resolve_database_credentials()
    if target.credential_provider == "aws":
        return resolve_target_credentials(
            target.secret_id or "", target.aws_region,
            fallback_user=os.environ.get(_env_name(target, "DB_USER")),
        )
    # env — local development only.
    user = (os.environ.get(_env_name(target, "DB_USER")) or "").strip()
    password = os.environ.get(_env_name(target, "DB_PASSWORD"))
    if not user or not password:
        raise DatabaseCredentialError(
            "Target {} uses env credentials but {} / {} are not set.".format(
                target.target_key, _env_name(target, "DB_USER"), _env_name(target, "DB_PASSWORD"))
        )
    return {"user": user, "password": password}


def invalidate_target_credentials(target: DatabaseTarget) -> None:
    """Drop ONLY this target's cached secret (other targets unaffected)."""
    if target.credential_provider == "aws" and target.secret_id:
        invalidate_database_secret_cache(secret_cache_key(target.secret_id, target.aws_region))
    elif target.credential_provider == "inherit":
        invalidate_database_secret_cache()


def target_connection_kwargs(target: DatabaseTarget, creds: Dict[str, str]) -> Dict[str, Any]:
    """psycopg kwargs. The control target inherits host/port/sslmode from env."""
    host = target.host or (os.environ.get("DB_HOST") or "").strip()
    if not host:
        raise TargetConnectionError(target.target_key, CONFIG_ERROR, "no host configured")
    port_raw = target.port or (os.environ.get("DB_PORT") or "5432")
    try:
        timeout = int(os.environ.get("DB_CONNECT_TIMEOUT") or "5")
    except ValueError:
        timeout = 5
    return {
        "host": host,
        "port": int(port_raw),
        "dbname": target.database_name,
        "user": creds["user"],
        "password": creds["password"],
        "sslmode": target.ssl_mode or (os.environ.get("DB_SSLMODE") or "prefer").strip() or "prefer",
        "connect_timeout": max(1, min(timeout, 30)),
        "application_name": "partops-target-{}".format(target.target_key),
    }


@contextmanager
def open_target_connection(
    target: DatabaseTarget,
    purpose: str,
    *,
    connect: Optional[Callable[..., Any]] = None,
) -> Generator[Any, None, None]:
    """One short-lived connection to ``target``; identity verified; always closed.

    * disabled target → TargetConnectionError(DISABLED) without connecting
    * credential problem → CONFIG_ERROR / AUTH_FAILED
    * auth failure → invalidate ONLY this target's secret, retry once
    * ``current_database()`` must equal ``target.database_name`` (WRONG_DATABASE)
    Errors name the target_key, never credentials.
    """
    if not target.is_enabled:
        raise TargetConnectionError(target.target_key, DISABLED, "target is disabled")
    if connect is None:
        from psycopg import connect as psycopg_connect

        connect = psycopg_connect

    def _creds() -> Dict[str, str]:
        try:
            return resolve_credentials_for_target(target)
        except DatabaseCredentialError as exc:
            raise TargetConnectionError(target.target_key, CONFIG_ERROR, str(exc)) from None

    conn = None
    try:
        try:
            conn = connect(**target_connection_kwargs(target, _creds()))
        except TargetConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001 — classify without leaking
            if is_authentication_error(exc):
                logger.warning("event=target_auth_failed target=%s (retrying once)", target.target_key)
                invalidate_target_credentials(target)
                try:
                    conn = connect(**target_connection_kwargs(target, _creds()))
                except TargetConnectionError:
                    raise
                except Exception as exc2:  # noqa: BLE001
                    kind = AUTH_FAILED if is_authentication_error(exc2) else UNREACHABLE
                    raise TargetConnectionError(target.target_key, kind, type(exc2).__name__) from None
            else:
                raise TargetConnectionError(target.target_key, UNREACHABLE, type(exc).__name__) from None
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            row = cur.fetchone()
        actual = row[0] if row else None
        conn.rollback()
        if actual != target.database_name:
            raise TargetConnectionError(
                target.target_key, WRONG_DATABASE,
                "connected to {!r}, registry expects {!r}".format(actual, target.database_name))
        logger.debug("event=target_connection_open target=%s purpose=%s", target.target_key, purpose)
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                logger.exception("event=target_connection_close_error target=%s", target.target_key)


def check_target_health(
    target: DatabaseTarget,
    *,
    connect: Optional[Callable[..., Any]] = None,
    manifest: Optional[Dict[str, Any]] = None,
    snapshot_fn: Optional[Callable[[Any, Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """READ-ONLY health of ONE target (connection, identity, framework scope).

    Reuses the Phase 5B manifest preflight: the control database is evaluated
    with the full scheduler scope incl. framework version; other targets with
    the ``target_execution`` scope (no version/registry checks). Exceptions are
    contained so one target can never affect another's result.
    """
    from framework_manifest import (
        evaluate_snapshot,
        fetch_catalog_snapshot,
        load_manifest,
    )

    checked_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    result: Dict[str, Any] = {
        "target_id": target.target_id,
        "target_key": target.target_key,
        "is_control_database": target.is_control_database,
        "checked_at": checked_at,
        "framework": None,
    }
    if not target.is_enabled:
        result.update(state=DISABLED, detail="Target is disabled; not contacted.")
        return result
    try:
        manifest = manifest or load_manifest()
        snapshot_fn = snapshot_fn or fetch_catalog_snapshot
        with open_target_connection(target, "health check", connect=connect) as conn:
            snapshot = snapshot_fn(conn, manifest)
        if target.is_control_database:
            report = evaluate_snapshot(manifest, snapshot, scope="scheduler")
        else:
            report = evaluate_snapshot(
                manifest, snapshot, scope=TARGET_EXECUTION_SCOPE,
                include_version=False, include_roles=False)
    except TargetConnectionError as exc:
        result.update(state=exc.kind, detail=str(exc))
        return result
    except Exception as exc:  # noqa: BLE001 — contained per target
        if type(exc).__name__ == "InsufficientPrivilege":
            # Connected and identity verified, but this role cannot read the
            # framework schema: the target cannot run jobs as configured.
            logger.warning("event=target_health_privilege target=%s", target.target_key)
            result.update(state=FRAMEWORK_INCOMPATIBLE,
                          detail="Connected, but the configured role lacks privileges on the framework schema.")
            return result
        logger.exception("event=target_health_error target=%s", target.target_key)
        result.update(state=CONFIG_ERROR, detail="health check error: {}".format(type(exc).__name__))
        return result
    counts = report.counts()
    result["framework"] = {
        "overall": report.overall,
        "counts": counts,
        "expected_version": report.expected_version if target.is_control_database else None,
        "database_version": report.database_version if target.is_control_database else None,
        "problems": [r.as_dict() for r in report.results if r.status != "PASS"][:30],
    }
    if report.failures:
        result.update(state=FRAMEWORK_INCOMPATIBLE,
                      detail="{} framework check(s) failed.".format(counts["FAIL"]))
    elif report.warnings:
        result.update(state=HEALTHY_WITH_WARNINGS,
                      detail="Connected; {} warning(s) (e.g. legacy definitions not captured).".format(counts["WARNING"]))
    else:
        result.update(state=HEALTHY, detail="Connected; framework compatible.")
    return result
