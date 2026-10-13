"""Shared database credential resolution for API and realtime scheduler.

Reads only already-loaded ``os.environ``. Does not open or parse env files.
Callers keep their component-specific environment loading.

Providers:
  * ``env`` (default) — ``DB_USER`` / ``DB_PASSWORD``
  * ``aws`` — AWS Secrets Manager via EC2 instance role / boto3 chain

AWS mode never falls back to ``DB_PASSWORD``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PROVIDER_ENV = "env"
PROVIDER_AWS = "aws"
_ALLOWED_PROVIDERS = frozenset({PROVIDER_ENV, PROVIDER_AWS})

DEFAULT_CACHE_TTL_SECONDS = 300.0

# PostgreSQL SQLSTATEs that indicate authentication / credential failure.
AUTH_SQLSTATES = frozenset({"28000", "28P01"})


class DatabaseCredentialError(RuntimeError):
    """Fail-closed credential resolution error (never includes secret values)."""


# Process-local cache: (username_or_None, password, monotonic_expiry)
_cache_lock = threading.Lock()
_cached_entry: Optional[Tuple[Optional[str], str, float]] = None

# Lazy boto3 client (created only in AWS mode on first fetch).
_sm_client = None
_sm_client_lock = threading.Lock()


def secret_provider() -> str:
    """Return configured provider name (default ``env`` for safe rollout)."""
    raw = (os.environ.get("PARTITION_DB_SECRET_PROVIDER") or PROVIDER_ENV).strip().lower()
    if not raw:
        return PROVIDER_ENV
    if raw not in _ALLOWED_PROVIDERS:
        raise DatabaseCredentialError(
            "Invalid PARTITION_DB_SECRET_PROVIDER={!r}; allowed: env, aws.".format(raw)
        )
    return raw


def cache_ttl_seconds() -> float:
    raw = (os.environ.get("PARTITION_DB_SECRET_CACHE_TTL_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise DatabaseCredentialError(
            "PARTITION_DB_SECRET_CACHE_TTL_SECONDS must be a positive number."
        ) from exc
    if value < 1.0:
        raise DatabaseCredentialError(
            "PARTITION_DB_SECRET_CACHE_TTL_SECONDS must be >= 1."
        )
    return value


def invalidate_database_secret_cache() -> None:
    """Drop the in-process Secrets Manager cache (e.g. after auth failure)."""
    global _cached_entry
    with _cache_lock:
        _cached_entry = None
    logger.info("Database secret cache invalidated")


def is_authentication_error(exc: BaseException) -> bool:
    """Return True when a psycopg/DB-API error indicates bad credentials."""
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is not None and str(sqlstate) in AUTH_SQLSTATES:
        return True
    # Import lazily so env-mode unit tests never need psycopg error types.
    try:
        from psycopg import Error as PsycopgError
        from psycopg.errors import InvalidPassword, InvalidAuthorizationSpecification
    except Exception:  # noqa: BLE001
        return False
    if isinstance(exc, (InvalidPassword, InvalidAuthorizationSpecification)):
        return True
    if isinstance(exc, PsycopgError):
        msg = str(exc).lower()
        if "password authentication failed" in msg or "authentication failed" in msg:
            return True
    return False


def resolve_database_credentials() -> Dict[str, str]:
    """Resolve username/password for a PostgreSQL connection.

    Returns:
        ``{"user": "...", "password": "..."}``

    Never logs secret material. AWS mode never reads ``DB_PASSWORD``.
    """
    provider = secret_provider()
    if provider == PROVIDER_ENV:
        return _resolve_env_credentials()
    if provider == PROVIDER_AWS:
        return _resolve_aws_credentials()
    raise DatabaseCredentialError(
        "Unsupported credential provider: {!r}".format(provider)
    )


def credential_provider_status() -> Dict[str, Any]:
    """Safe readiness-oriented status (no secrets)."""
    try:
        provider = secret_provider()
    except DatabaseCredentialError as exc:
        return {
            "credential_provider": None,
            "credential_configuration": "failed",
            "error": str(exc),
        }

    if provider == PROVIDER_ENV:
        user_ok = bool((os.environ.get("DB_USER") or "").strip())
        pass_ok = bool((os.environ.get("DB_PASSWORD") or "").strip())
        ready = user_ok and pass_ok
        return {
            "credential_provider": PROVIDER_ENV,
            "credential_configuration": "ready" if ready else "incomplete",
            "db_user_configured": user_ok,
            "db_password_configured": pass_ok,
        }

    secret_id = (os.environ.get("PARTITION_DB_SECRET_ID") or "").strip()
    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    return {
        "credential_provider": PROVIDER_AWS,
        "credential_configuration": "ready" if secret_id else "incomplete",
        "secret_id_configured": bool(secret_id),
        "aws_region_configured": bool(region),
        # Do not call Secrets Manager from readiness.
        "cache_ttl_seconds": cache_ttl_seconds(),
    }


def _resolve_env_credentials() -> Dict[str, str]:
    user = (os.environ.get("DB_USER") or "").strip()
    password = os.environ.get("DB_PASSWORD")
    if password is None or password == "":
        raise DatabaseCredentialError(
            "DB_PASSWORD is required when PARTITION_DB_SECRET_PROVIDER=env."
        )
    if not user:
        raise DatabaseCredentialError(
            "DB_USER is required when PARTITION_DB_SECRET_PROVIDER=env."
        )
    return {"user": user, "password": password}


def _resolve_aws_credentials() -> Dict[str, str]:
    secret_user, password = _get_aws_secret_cached()
    env_user = (os.environ.get("DB_USER") or "").strip()
    user = (secret_user or env_user or "").strip()
    if not user:
        raise DatabaseCredentialError(
            "Database username missing: secret has no username and DB_USER is unset."
        )
    if not password:
        raise DatabaseCredentialError(
            "Database password missing from Secrets Manager secret."
        )
    return {"user": user, "password": password}


def _get_aws_secret_cached() -> Tuple[Optional[str], str]:
    global _cached_entry
    now = time.monotonic()
    with _cache_lock:
        if _cached_entry is not None:
            user, password, expiry = _cached_entry
            if now < expiry:
                return user, password

    user, password = get_database_secret()
    ttl = cache_ttl_seconds()
    with _cache_lock:
        _cached_entry = (user, password, time.monotonic() + ttl)
    logger.info("Database secret cache refreshed (provider=aws)")
    return user, password


def get_database_secret() -> Tuple[Optional[str], str]:
    """Fetch AWSCURRENT secret from Secrets Manager (uncached).

    Returns:
        ``(username_or_None, password)``

    Raises:
        DatabaseCredentialError on any failure (fail closed; no env fallback).
    """
    secret_id = (os.environ.get("PARTITION_DB_SECRET_ID") or "").strip()
    if not secret_id:
        raise DatabaseCredentialError(
            "PARTITION_DB_SECRET_ID is required when PARTITION_DB_SECRET_PROVIDER=aws."
        )

    client = _secrets_manager_client()
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except Exception as exc:  # noqa: BLE001 — map AWS errors without leaking secrets
        raise _map_aws_error(exc) from None

    secret_string = response.get("SecretString")
    if not secret_string:
        # Do not surface SecretBinary contents.
        if response.get("SecretBinary") is not None:
            raise DatabaseCredentialError(
                "Secrets Manager secret is binary; PartOps expects a JSON SecretString."
            )
        raise DatabaseCredentialError(
            "Secrets Manager response contained no SecretString."
        )

    try:
        payload = json.loads(secret_string)
    except (TypeError, ValueError) as exc:
        raise DatabaseCredentialError(
            "Secrets Manager SecretString is not valid JSON."
        ) from exc

    if not isinstance(payload, dict):
        raise DatabaseCredentialError(
            "Secrets Manager JSON must be an object with password (and optional username)."
        )

    password = payload.get("password")
    if password is None or password == "":
        raise DatabaseCredentialError(
            "Secrets Manager JSON is missing a non-empty password field."
        )
    if not isinstance(password, str):
        raise DatabaseCredentialError(
            "Secrets Manager JSON password field must be a string."
        )

    username = payload.get("username")
    if username is not None and username != "":
        if not isinstance(username, str):
            raise DatabaseCredentialError(
                "Secrets Manager JSON username field must be a string when present."
            )
        return username, password
    return None, password


def _secrets_manager_client() -> Any:
    global _sm_client
    if _sm_client is not None:
        return _sm_client
    with _sm_client_lock:
        if _sm_client is not None:
            return _sm_client
        try:
            import boto3
        except ImportError as exc:
            raise DatabaseCredentialError(
                "boto3 is required when PARTITION_DB_SECRET_PROVIDER=aws."
            ) from exc
        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or ""
        ).strip() or None
        # Lazy client — uses EC2 instance role / default credential chain.
        kwargs: Dict[str, Any] = {"service_name": "secretsmanager"}
        if region:
            kwargs["region_name"] = region
        _sm_client = boto3.client(**kwargs)
        logger.info("Database credential provider initialized: aws")
        return _sm_client


def _map_aws_error(exc: BaseException) -> DatabaseCredentialError:
    """Convert botocore/AWS errors into safe DatabaseCredentialError messages."""
    name = type(exc).__name__
    # botocore ClientError exposes response error code.
    response = getattr(exc, "response", None)
    code = None
    if isinstance(response, dict):
        err = response.get("Error") or {}
        code = err.get("Code")

    if code in ("AccessDeniedException", "AccessDenied", "UnrecognizedClientException"):
        return DatabaseCredentialError(
            "Secrets Manager GetSecretValue denied (AccessDenied). "
            "Check the EC2 instance role permissions."
        )
    if code in ("ResourceNotFoundException", "ResourceNotFound"):
        return DatabaseCredentialError(
            "Secrets Manager secret was not found (ResourceNotFound)."
        )
    if code in ("InvalidRequestException", "ValidationException"):
        return DatabaseCredentialError(
            "Secrets Manager request was invalid (configuration error)."
        )
    # Network / timeout / generic — still fail closed, never mention env password.
    return DatabaseCredentialError(
        "Secrets Manager retrieval failed ({})".format(code or name)
    )


def _reset_for_tests() -> None:
    """Clear cache and lazy client — unit tests only."""
    global _cached_entry, _sm_client
    with _cache_lock:
        _cached_entry = None
    with _sm_client_lock:
        _sm_client = None
