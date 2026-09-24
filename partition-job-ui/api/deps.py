"""Shared HTTP helpers for the FastAPI layer."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from database import DatabaseError, PgAgentNotInstalledError
from validators import ValidationError


def serialize(value: Any) -> Any:
    """JSON-safe encoding for datetimes and nested structures."""
    return jsonable_encoder(
        value,
        custom_encoder={
            datetime: lambda v: v.isoformat(sep=" "),
            date: lambda v: v.isoformat(),
        },
    )


def ok(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=serialize(payload))


def raise_for_domain(exc: Exception) -> None:
    if isinstance(exc, ValidationError):
        raise HTTPException(status_code=400, detail=exc.message) from exc
    if isinstance(exc, PgAgentNotInstalledError):
        raise HTTPException(status_code=503, detail=exc.message) from exc
    if isinstance(exc, DatabaseError):
        raise HTTPException(status_code=502, detail=exc.message) from exc
    raise exc
