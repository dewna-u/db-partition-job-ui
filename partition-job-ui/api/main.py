"""FastAPI presentation API — wraps existing database/scheduler modules."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import cron, dashboard, jobs, logs, pgagent, readiness, scheduler

app = FastAPI(
    title="Partition Manager API",
    description="HTTP facade over existing partition-job database and scheduler clients.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
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


@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "partition-manager-api"}
