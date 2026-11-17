"""Best-effort HTTP signals to the realtime scheduler backend.

Used by Streamlit AFTER the database connection for a config write has closed.
Never treats HTTP as authoritative scheduling input — PostgreSQL remains truth.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip() or default


def scheduler_refresh_url() -> str:
    return _env(
        "PARTITION_SCHEDULER_REFRESH_URL",
        "http://127.0.0.1:8765/internal/scheduler/refresh",
    )


def scheduler_status_url() -> str:
    return _env(
        "PARTITION_SCHEDULER_STATUS_URL",
        "http://127.0.0.1:8765/internal/scheduler/status",
    )


def refresh_timeout_seconds() -> float:
    try:
        return max(0.2, float(_env("PARTITION_SCHEDULER_REFRESH_TIMEOUT_SECONDS", "2")))
    except ValueError:
        return 2.0


def notify_scheduler_refresh() -> tuple[bool, str]:
    """
    POST a wake-up refresh signal to the scheduler backend.

    Returns (ok, message). Never raises. A failed signal must NOT roll back
    an already-committed database configuration change.
    """
    url = scheduler_refresh_url()
    timeout = refresh_timeout_seconds()
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read().decode("utf-8", errors="replace")
            if 200 <= int(status) < 300:
                return True, f"Scheduler refresh accepted (HTTP {status})."
            return False, f"Scheduler refresh unexpected HTTP {status}: {body[:200]}"
    except urllib.error.HTTPError as exc:
        logger.warning("Scheduler refresh HTTP error: %s", exc.code)
        return False, f"Scheduler refresh failed (HTTP {exc.code}). Periodic reconciliation will catch up."
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scheduler refresh unavailable: %s", exc)
        return (
            False,
            "Scheduler backend refresh unavailable. Configuration was saved; "
            "periodic reconciliation will discover it.",
        )


def fetch_scheduler_status() -> tuple[bool, Optional[dict[str, Any]], str]:
    """GET in-memory scheduler status. Never raises."""
    url = scheduler_status_url()
    timeout = refresh_timeout_seconds()
    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                return False, None, "Scheduler status payload was not an object."
            return True, data, "ok"
    except Exception as exc:  # noqa: BLE001
        logger.debug("Scheduler status unavailable: %s", exc)
        return False, None, "Scheduler backend status unavailable."


# ---------------------------------------------------------------------------
# Phase 5D — manual runs on MANAGED targets go through the scheduler's single
# remote execution engine (same claim / ledger / locks / slots / recovery).
# ---------------------------------------------------------------------------
def scheduler_manual_run_url() -> str:
    return _env(
        "PARTITION_SCHEDULER_MANUAL_RUN_URL",
        "http://127.0.0.1:8765/internal/scheduler/manual-run",
    )


def manual_run_timeout_seconds() -> float:
    try:
        return max(5.0, float(_env("PARTITION_SCHEDULER_MANUAL_RUN_TIMEOUT_SECONDS", "900")))
    except ValueError:
        return 900.0


# Transport outcomes (not database statuses).
REMOTE_RUN_UNAVAILABLE = "SCHEDULER_UNAVAILABLE"
REMOTE_RUN_TIMEOUT = "SCHEDULER_TIMEOUT"


def request_remote_manual_run(
    job_id: int, confirm_drop: bool = False, attention_id: Optional[int] = None
) -> dict[str, Any]:
    """POST a remote manual run to the scheduler and wait for its result.

    Never raises. On transport failure returns a status that tells the caller
    whether anything may have started (timeout) or certainly did not
    (unavailable). The scheduler never retries a manual run automatically.
    """
    payload = json.dumps(
        {"job_id": int(job_id), "confirm_drop": bool(confirm_drop),
         "attention_id": int(attention_id) if attention_id is not None else None}
    ).encode("utf-8")
    request = urllib.request.Request(
        scheduler_manual_run_url(), data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=manual_run_timeout_seconds()) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {"status": REMOTE_RUN_UNAVAILABLE}
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
            if isinstance(data, dict) and data.get("status"):
                return data
        except Exception:  # noqa: BLE001
            pass
        logger.warning("Remote manual run HTTP error: %s", exc.code)
        if int(exc.code) >= 500:
            # The scheduler accepted the request and then failed: whether the
            # engine already dispatched cannot be known here.
            return _no_result(f"HTTP {exc.code}")
        # 4xx: the handler refused the request before running anything.
        return {"status": REMOTE_RUN_UNAVAILABLE,
                "message": f"Scheduler rejected the request (HTTP {exc.code}); nothing was executed."}
    except Exception as exc:  # noqa: BLE001
        if _connection_refused(exc):
            # Never reached the scheduler: nothing can have started.
            logger.warning("Remote manual run: scheduler unavailable (connection refused)")
            return {"status": REMOTE_RUN_UNAVAILABLE,
                    "message": "The scheduler backend is unavailable; remote manual runs need it. "
                               "Nothing was executed."}
        # Timeout, reset, remote disconnect, … AFTER the request may have been
        # delivered: the run may have started. Never claim "nothing executed".
        logger.warning("Remote manual run: no result (%s)", type(exc).__name__)
        return _no_result(type(exc).__name__)


def _connection_refused(exc: BaseException) -> bool:
    """True only when the TCP connection to the scheduler was refused."""
    if isinstance(exc, ConnectionRefusedError):
        return True
    reason = getattr(exc, "reason", None)  # urllib.error.URLError wraps the socket error
    return isinstance(reason, ConnectionRefusedError)


def _no_result(cause: str) -> dict[str, Any]:
    return {"status": REMOTE_RUN_TIMEOUT,
            "message": "The scheduler did not return a result ({}). The run may still be in "
                       "progress or may have finished — check History and the execution "
                       "attempts. Do not re-run until you have checked.".format(cause)}
