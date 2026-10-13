"""Lightweight PartOps DogStatsD metrics (stdlib UDP only).

Architecture:
  PartOps → UDP DogStatsD 127.0.0.1:8125 → Datadog Agent → Datadog

The Datadog Agent owns the Datadog API key. This module never reads or sends
an application Datadog API key and never calls the Datadog HTTPS API.

All emit helpers are best-effort: failures never propagate into business logic.
Safe for asyncio and ``asyncio.to_thread`` worker threads (stateless UDP send).
"""

from __future__ import annotations

import logging
import os
import re
import socket
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "partops"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8125
DEFAULT_ENVIRONMENT = "unknown"

# Tag keys that must never be used (high cardinality / sensitive).
_FORBIDDEN_TAG_KEYS = frozenset(
    {
        "job_id",
        "job_name",
        "table_name",
        "table_schema",
        "schema",
        "secret_id",
        "secret_arn",
        "password",
        "dsn",
        "error",
        "message",
        "exception",
        "host",
        "hostname",
        "path",
        "url",
    }
)

_ALLOWED_TAG_KEY_RE = re.compile(r"^[a-z][a-z0-9_\.]{0,63}$")
_ALLOWED_TAG_VALUE_RE = re.compile(r"^[a-zA-Z0-9_\-:.]{1,64}$")
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_\.]{0,63}$")

# Rate-limit noisy metric-send warnings (seconds).
_WARN_INTERVAL_SECONDS = 60.0
_last_warn_mono = 0.0


def datadog_enabled() -> bool:
    raw = (os.environ.get("PARTOPS_DATADOG_ENABLED") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def dogstatsd_host() -> str:
    return (os.environ.get("PARTOPS_DOGSTATSD_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST


def dogstatsd_port() -> int:
    raw = (os.environ.get("PARTOPS_DOGSTATSD_PORT") or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    if not (1 <= port <= 65535):
        return DEFAULT_PORT
    return port


def metrics_namespace() -> str:
    raw = (os.environ.get("PARTOPS_METRICS_NAMESPACE") or DEFAULT_NAMESPACE).strip().lower()
    if not raw or not _NAMESPACE_RE.match(raw):
        return DEFAULT_NAMESPACE
    return raw


def environment_tag() -> str:
    raw = (os.environ.get("PARTOPS_ENVIRONMENT") or DEFAULT_ENVIRONMENT).strip()
    if not raw:
        return DEFAULT_ENVIRONMENT
    if not _ALLOWED_TAG_VALUE_RE.match(raw):
        return DEFAULT_ENVIRONMENT
    return raw


def observability_status() -> Dict[str, Any]:
    """Safe readiness-oriented status (never probes DogStatsD connectivity)."""
    return {
        "datadog_enabled": datadog_enabled(),
        "dogstatsd_host_configured": bool(dogstatsd_host()),
        "dogstatsd_port": dogstatsd_port(),
        "environment": environment_tag(),
        "metrics_namespace": metrics_namespace(),
        # Explicit: Agent availability is NOT a readiness dependency.
        "dogstatsd_required_for_readiness": False,
    }


def normalize_job_status(status: Optional[str]) -> str:
    """Map SQL/Python statuses to low-cardinality metric tag values."""
    if not status:
        return "unknown"
    text = str(status).strip().lower()
    mapping = {
        "executed": "executed",
        "failed": "failed",
        "failed_database": "failed_database",
        "failed_connection": "failed_connection",
        "failed_invalid_schedule": "failed_invalid_schedule",
        "skipped_locked": "skipped_locked",
        "skipped_target_locked": "skipped_target_locked",
        "skipped_disabled": "skipped_disabled",
        "skipped_rescheduled": "skipped_rescheduled",
        "skipped_not_due": "skipped_not_due",
        "skipped_circuit_breaker": "skipped_circuit_breaker",
        "not_found": "not_found",
    }
    return mapping.get(text, "other")


def normalize_operation(is_create: Any) -> Optional[str]:
    if is_create is True:
        return "create"
    if is_create is False:
        return "drop"
    return None


def normalize_secrets_failure_reason(code_or_name: Optional[str]) -> str:
    """Low-cardinality reason tags for Secrets Manager failures."""
    code = (code_or_name or "").strip()
    if code in ("access_denied", "not_found", "invalid_secret", "aws_error"):
        return code
    if code in ("AccessDeniedException", "AccessDenied", "UnrecognizedClientException"):
        return "access_denied"
    if code in ("ResourceNotFoundException", "ResourceNotFound"):
        return "not_found"
    if code in (
        "InvalidRequestException",
        "ValidationException",
        "invalid_json",
        "missing_password",
        "missing_secret_string",
        "binary_secret",
        "missing_secret_id",
        "missing_username",
    ):
        return "invalid_secret"
    return "aws_error"


def _sanitize_tags(tags: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    if not tags:
        return ()
    out = []
    for key, value in tags.items():
        k = str(key).strip().lower()
        if k in _FORBIDDEN_TAG_KEYS:
            continue
        if not _ALLOWED_TAG_KEY_RE.match(k):
            continue
        if value is None:
            continue
        v = str(value).strip()
        if not v or not _ALLOWED_TAG_VALUE_RE.match(v):
            continue
        out.append("{}:{}".format(k, v))
    return tuple(out)


def _base_tags(extra: Optional[Mapping[str, Any]] = None) -> Tuple[str, ...]:
    tags: Dict[str, Any] = {"env": environment_tag()}
    if extra:
        tags.update(extra)
    return _sanitize_tags(tags)


def _metric_name(name: str) -> str:
    ns = metrics_namespace()
    bare = name.strip()
    if bare.startswith(ns + "."):
        return bare
    if bare.startswith("partops."):
        return bare
    return "{}.{}".format(ns, bare.lstrip("."))


def _format_metric(
    metric_type: str,
    name: str,
    value: float,
    tags: Sequence[str],
    sample_rate: float = 1.0,
) -> bytes:
    # DogStatsD: metric:value|type|#tag1:v1,tag2:v2
    body = "{}:{}|{}".format(_metric_name(name), _format_value(value), metric_type)
    if sample_rate is not None and sample_rate != 1.0:
        body = "{}|@{}".format(body, sample_rate)
    if tags:
        body = "{}|#{}".format(body, ",".join(tags))
    return body.encode("utf-8")


def _format_value(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    # Prefer compact floats without scientific notation for common ints.
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _warn_throttled(message: str, *args: Any) -> None:
    global _last_warn_mono
    try:
        import time

        now = time.monotonic()
        if (now - _last_warn_mono) < _WARN_INTERVAL_SECONDS:
            logger.debug(message, *args)
            return
        _last_warn_mono = now
        logger.warning(message, *args)
    except Exception:  # noqa: BLE001
        pass


def _send(payload: bytes) -> None:
    if not datadog_enabled():
        return
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.sendto(payload, (dogstatsd_host(), dogstatsd_port()))
        finally:
            sock.close()
    except Exception as exc:  # noqa: BLE001 — never break callers
        _warn_throttled("DogStatsD send failed (ignored): %s", type(exc).__name__)


def increment(
    name: str,
    value: int = 1,
    tags: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit a DogStatsD counter. Never raises."""
    try:
        if not datadog_enabled():
            return
        payload = _format_metric("c", name, float(value), _base_tags(tags))
        _send(payload)
    except Exception:  # noqa: BLE001
        _warn_throttled("DogStatsD increment failed (ignored)")


def gauge(
    name: str,
    value: float,
    tags: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit a DogStatsD gauge. Never raises."""
    try:
        if not datadog_enabled():
            return
        payload = _format_metric("g", name, float(value), _base_tags(tags))
        _send(payload)
    except Exception:  # noqa: BLE001
        _warn_throttled("DogStatsD gauge failed (ignored)")


def timing(
    name: str,
    value_ms: float,
    tags: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit a DogStatsD timing (milliseconds). Never raises."""
    try:
        if not datadog_enabled():
            return
        if value_ms is None:
            return
        payload = _format_metric("ms", name, float(value_ms), _base_tags(tags))
        _send(payload)
    except Exception:  # noqa: BLE001
        _warn_throttled("DogStatsD timing failed (ignored)")


def histogram(
    name: str,
    value: float,
    tags: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emit a DogStatsD histogram. Never raises."""
    try:
        if not datadog_enabled():
            return
        payload = _format_metric("h", name, float(value), _base_tags(tags))
        _send(payload)
    except Exception:  # noqa: BLE001
        _warn_throttled("DogStatsD histogram failed (ignored)")


# ---------------------------------------------------------------------------
# Domain helpers (keep call sites thin)
# ---------------------------------------------------------------------------


def emit_scheduler_heartbeat() -> None:
    gauge("scheduler.heartbeat", 1)


def emit_scheduler_concurrency(
    *,
    active_jobs: int,
    max_concurrent_jobs: int,
    queue_depth: int,
    available_slots: Optional[int] = None,
    overdue_jobs: Optional[int] = None,
) -> None:
    gauge("scheduler.active_jobs", active_jobs)
    gauge("scheduler.max_concurrent_jobs", max_concurrent_jobs)
    gauge("scheduler.queue_depth", queue_depth)
    if available_slots is None:
        available_slots = max(0, int(max_concurrent_jobs) - int(active_jobs))
    gauge("scheduler.available_slots", available_slots)
    if overdue_jobs is not None:
        gauge("scheduler.overdue_jobs", overdue_jobs)


def emit_scheduler_refresh_failure() -> None:
    increment("scheduler.refresh_failure")


def emit_scheduler_execution_exception() -> None:
    increment("scheduler.execution_exception")


def emit_job_execution(
    *,
    status: Optional[str],
    is_create: Any = None,
    duration_ms: Any = None,
) -> None:
    """Emit job execution counter and optional duration (DB duration only)."""
    tags: Dict[str, Any] = {"status": normalize_job_status(status)}
    operation = normalize_operation(is_create)
    if operation is not None:
        tags["operation"] = operation
    increment("job.execution", tags=tags)
    if duration_ms is None:
        return
    try:
        ms = float(duration_ms)
    except (TypeError, ValueError):
        return
    if ms < 0:
        return
    timing("job.duration_ms", ms, tags=tags)


def emit_db_connection_failure(*, component: str) -> None:
    if component not in ("api", "scheduler"):
        component = "other"
    increment("db.connection_failure", tags={"component": component})


def emit_secrets_retrieval_failure(*, reason: str) -> None:
    increment(
        "secrets_manager.retrieval_failure",
        tags={"reason": normalize_secrets_failure_reason(reason)},
    )


def emit_secrets_refresh() -> None:
    """Emit only after a real AWS GetSecretValue fetch (not cache hits)."""
    increment("secrets_manager.refresh")


def emit_api_request(*, method: str, status_code: int) -> None:
    method_tag = (method or "OTHER").strip().upper()
    if method_tag not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        method_tag = "OTHER"
    if status_code < 100 or status_code > 599:
        status_class = "unknown"
    else:
        status_class = "{}xx".format(status_code // 100)
    tags = {"method": method_tag, "status_class": status_class}
    increment("api.request", tags=tags)
    if status_code >= 500:
        increment("api.error", tags=tags)


def format_counter_for_tests(
    name: str, value: int = 1, tags: Optional[Mapping[str, Any]] = None
) -> str:
    """Test helper: return DogStatsD payload string without sending."""
    return _format_metric("c", name, float(value), _base_tags(tags)).decode("utf-8")


def format_gauge_for_tests(
    name: str, value: float, tags: Optional[Mapping[str, Any]] = None
) -> str:
    return _format_metric("g", name, float(value), _base_tags(tags)).decode("utf-8")


def format_timing_for_tests(
    name: str, value_ms: float, tags: Optional[Mapping[str, Any]] = None
) -> str:
    return _format_metric("ms", name, float(value_ms), _base_tags(tags)).decode("utf-8")
