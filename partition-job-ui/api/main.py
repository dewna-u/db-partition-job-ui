"""FastAPI presentation API — wraps existing database/scheduler modules."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.routers import cron, dashboard, jobs, logs, pgagent, readiness, scheduler, system
from observability import emit_api_request

app = FastAPI(
    title="PartOps API",
    description="GTN EDB Partition Operations Platform — HTTP facade over partition-job database and scheduler clients.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(readiness.router, prefix="/api")
app.include_router(scheduler.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(pgagent.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(cron.router, prefix="/api")
app.include_router(system.router, prefix="/api")


@app.middleware("http")
async def partops_metrics_middleware(request: Request, call_next):
    """Low-cardinality API metrics only (method + status class; no path tags)."""
    try:
        response = await call_next(request)
    except Exception:
        emit_api_request(method=request.method, status_code=500)
        raise
    emit_api_request(method=request.method, status_code=int(response.status_code))
    return response


@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "partops-api"}
