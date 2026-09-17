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

from dotenv import load_dotenv

# Prefer realtime overrides for scheduler URLs without forcing password overwrite.
load_dotenv(".env.realtime", override=False)
load_dotenv(".env", override=False)

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
