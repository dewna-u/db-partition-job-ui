from __future__ import annotations

from fastapi import APIRouter

from api.deps import ok
from scheduler_client import fetch_scheduler_status, notify_scheduler_refresh

router = APIRouter(tags=["scheduler"])


@router.get("/scheduler/status")
def scheduler_status():
    reachable, status, message = fetch_scheduler_status()
    return ok({"ok": reachable, "status": status, "message": message})


@router.post("/scheduler/refresh")
def scheduler_refresh():
    ok_flag, message = notify_scheduler_refresh()
    return ok({"ok": ok_flag, "message": message})
