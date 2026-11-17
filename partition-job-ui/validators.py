"""Input validation for partition job configuration."""

from __future__ import annotations

import json
import re
from typing import Any

from job_autofill import validate_six_field_cron

# PostgreSQL unquoted identifier: starts with letter/underscore, then letters, digits, underscores.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_FORBIDDEN_CHARS = ('"', "'", ";", "--", "/*", "*/", "\\", " ", "\t", "\n", "\r")

ALLOWED_PARTITION_UNITS = frozenset({"day", "week", "month", "year"})
ALLOWED_FREQUENCY_UNITS = frozenset(
    {"minute", "hour", "day", "week", "month", "year"}
)
ALLOWED_CREATE_DROP_UNITS = frozenset({"day", "week", "month", "year"})

MAX_JOB_NAME_LENGTH = 100
MAX_IDENTIFIER_LENGTH = 63


class ValidationError(Exception):
    """Raised when user input fails validation."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def trim_text(value: Any) -> str:
    """Return a trimmed string; treat None as empty."""
    if value is None:
        return ""
    return str(value).strip()


def validate_job_name(job_name: str) -> str:
    name = trim_text(job_name)
    if not name:
        raise ValidationError("Job Name is required.")
    if len(name) > MAX_JOB_NAME_LENGTH:
        raise ValidationError(
            f"Job Name must be at most {MAX_JOB_NAME_LENGTH} characters."
        )
    return name


def _validate_identifier(value: str, field_label: str) -> str:
    name = trim_text(value)
    if not name:
        raise ValidationError(f"{field_label} is required.")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValidationError(
            f"{field_label} must be at most {MAX_IDENTIFIER_LENGTH} characters."
        )
    lower = name.lower()
    for token in _FORBIDDEN_CHARS:
        if token in name or (token.strip() and token in lower):
            raise ValidationError(
                f"{field_label} contains invalid characters. "
                "Use a normal PostgreSQL unquoted identifier "
                "(letters, digits, and underscores only)."
            )
    if not _IDENTIFIER_RE.match(name):
        raise ValidationError(
            f"{field_label} must be a valid PostgreSQL unquoted identifier "
            "(start with a letter or underscore; letters, digits, and underscores only)."
        )
    return name


def validate_table_schema(schema: str) -> str:
    return _validate_identifier(schema, "Table Schema")


def validate_table_name(table_name: str) -> str:
    return _validate_identifier(table_name, "Table Name")


# ---------------------------------------------------------------------------
# db_config_para allowlist (Phase 5A).
#
# Every key is applied by the database with set_config(key, value, true) inside
# run_partition_job_scheduled / run_partition_job_manual, so only performance /
# timeout session parameters are accepted. Identity- or resolution-changing
# settings (role, session_authorization, search_path, ...) are never allowed.
#
# MUST stay identical to mubasher_oms.partition_job_config_param_allowed() in
# sql/migrations/20261110_01_partition_job_attention.sql (unit-tested).
# ---------------------------------------------------------------------------
ALLOWED_DB_CONFIG_PARAMETERS = frozenset(
    {
        "lock_timeout",
        "statement_timeout",
        "idle_in_transaction_session_timeout",
        "work_mem",
        "maintenance_work_mem",
        "effective_io_concurrency",
        "random_page_cost",
        "max_parallel_maintenance_workers",
        # Value-restricted: ISO output only (legacy routines use 'ISO, DMY').
        # Non-ISO styles such as 'Redwood, SHOW_TIME' break psycopg timestamp
        # parsing and historically triggered the scheduler retry loop.
        "datestyle",
    }
)
DB_CONFIG_VALUE_MAX_LENGTH = 64
_DB_CONFIG_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_DATESTYLE_VALUE_RE = re.compile(
    r"^\s*iso\s*(,\s*(dmy|mdy|ymd)\s*)?$", re.IGNORECASE
)


def is_allowed_db_config_parameter(key: str, value: str) -> bool:
    """Python mirror of partition_job_config_param_allowed(key, value)."""
    if key is None or value is None:
        return False
    name = str(key).strip().lower()
    text = str(value)
    if len(text) < 1 or len(text) > DB_CONFIG_VALUE_MAX_LENGTH:
        return False
    if name == "datestyle":
        return bool(_DATESTYLE_VALUE_RE.match(text))
    if name in ALLOWED_DB_CONFIG_PARAMETERS:
        return bool(_DB_CONFIG_VALUE_RE.match(text))
    return False


def validate_db_config_parameters(config: dict[str, Any]) -> dict[str, Any]:
    """Reject unsupported db_config_para keys/values with a clear message.

    Keys are normalised to lower case (PostgreSQL GUC names are
    case-insensitive). Values must be strings or numbers.
    """
    normalised: dict[str, Any] = {}
    for raw_key, value in config.items():
        key = str(raw_key).strip().lower()
        if not key:
            raise ValidationError("Database Configuration contains an empty parameter name.")
        if key in normalised:
            raise ValidationError(
                f"Database Configuration parameter '{key}' is specified more than once."
            )
        if key not in ALLOWED_DB_CONFIG_PARAMETERS:
            allowed = ", ".join(sorted(ALLOWED_DB_CONFIG_PARAMETERS))
            raise ValidationError(
                f"Database Configuration parameter '{raw_key}' is not supported. "
                f"Allowed parameters: {allowed}."
            )
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValidationError(
                f"Database Configuration parameter '{key}' must be a string or number."
            )
        if not is_allowed_db_config_parameter(key, str(value)):
            if key == "datestyle":
                raise ValidationError(
                    "Database Configuration 'datestyle' must use ISO output "
                    "(for example 'ISO, DMY' or 'ISO, MDY')."
                )
            raise ValidationError(
                f"Database Configuration value for '{key}' is not valid. Use a plain "
                "value such as '30s', '512MB' or 4 (max "
                f"{DB_CONFIG_VALUE_MAX_LENGTH} characters)."
            )
        normalised[key] = value
    return normalised


def validate_db_config_json(raw: str) -> dict[str, Any]:
    text = trim_text(raw)
    if not text:
        raise ValidationError("Database Configuration JSON is required.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise ValidationError(
            "Database Configuration must be valid JSON."
        ) from None
    if not isinstance(parsed, dict):
        raise ValidationError(
            "Database Configuration must be a JSON object (not an array, string, number, boolean, or null)."
        )
    return validate_db_config_parameters(parsed)


def validate_cron_schedule(schedule: str) -> str:
    """Validate the six-field schedule: second minute hour day-of-month month day-of-week."""
    text = trim_text(schedule)
    if not text:
        raise ValidationError("Job Schedule is required.")
    normalised = " ".join(text.split())
    error = validate_six_field_cron(normalised)
    if error:
        raise ValidationError(error)
    return normalised


def validate_positive_int(value: Any, field_label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_label} must be a whole number.") from None
    if number < 1:
        raise ValidationError(f"{field_label} must be greater than zero.")
    return number


def validate_partition_unit(unit: str) -> str:
    value = trim_text(unit).lower()
    if value not in ALLOWED_PARTITION_UNITS:
        raise ValidationError(
            "Partition Unit must be one of: day, week, month, year."
        )
    return value


def validate_frequency_unit(unit: str) -> str:
    value = trim_text(unit).lower()
    if value not in ALLOWED_FREQUENCY_UNITS:
        raise ValidationError(
            "Frequency unit must be one of: minute, hour, day, week, month, year."
        )
    return value


def validate_create_drop_unit(unit: str) -> str:
    value = trim_text(unit).lower()
    if value not in ALLOWED_CREATE_DROP_UNITS:
        raise ValidationError(
            "Create/Drop Interval unit must be one of: day, week, month, year."
        )
    return value


def to_interval_string(amount: int, unit: str) -> str:
    """Build a PostgreSQL interval literal from a validated amount and unit."""
    # Pluralise for amounts other than 1 (PostgreSQL accepts both forms).
    if amount == 1:
        return f"{amount} {unit}"
    # minute -> minutes, hour -> hours, day -> days, week -> weeks, month -> months
    return f"{amount} {unit}s"


_INTERVAL_PARTS_RE = re.compile(
    r"^\s*(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s*$",
    re.IGNORECASE,
)


def parse_interval_parts(value: Any) -> tuple[int, str]:
    """
    Parse a PostgreSQL-ish interval literal such as '2 months' into (amount, unit).

    Unit is returned in singular form matching the form validators.
    """
    text = trim_text(value)
    if not text:
        raise ValidationError("Interval value is required.")
    match = _INTERVAL_PARTS_RE.match(text)
    if not match:
        raise ValidationError(
            f"Could not parse interval '{text}'. Expected forms like '1 day' or '2 months'."
        )
    amount = int(match.group(1))
    unit = match.group(2).lower().rstrip("s")
    if amount < 1:
        raise ValidationError("Interval amount must be greater than zero.")
    return amount, unit


def validate_form_data(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Validate and normalise all form fields.

    Returns a dict ready for create_partition_job().
    Raises ValidationError on the first failure.
    """
    job_name = validate_job_name(raw.get("job_name", ""))
    table_schema = validate_table_schema(raw.get("table_schema", ""))
    table_name = validate_table_name(raw.get("table_name", ""))
    db_config = validate_db_config_json(raw.get("db_config", ""))
    job_schedule = validate_cron_schedule(raw.get("job_schedule", ""))

    frequency_amount = validate_positive_int(
        raw.get("frequency_amount"), "Frequency"
    )
    frequency_unit = validate_frequency_unit(raw.get("frequency_unit", ""))
    frequency = to_interval_string(frequency_amount, frequency_unit)

    partition_unit = validate_partition_unit(raw.get("partition_unit", ""))
    partition_period = validate_positive_int(
        raw.get("partition_period"), "Partition Period"
    )

    create_drop_amount = validate_positive_int(
        raw.get("create_drop_amount"), "Create/Drop Interval"
    )
    create_drop_unit = validate_create_drop_unit(
        raw.get("create_drop_unit", "")
    )
    create_drop_interval = to_interval_string(
        create_drop_amount, create_drop_unit
    )

    next_run_time = raw.get("next_run_time")
    if next_run_time is None:
        raise ValidationError("Next Run Time is required.")

    return {
        "job_name": job_name,
        "is_enabled": bool(raw.get("is_enabled", True)),
        "table_schema": table_schema,
        "table_name": table_name,
        "db_config": db_config,
        "job_schedule": job_schedule,
        "frequency": frequency,
        "next_run_time": next_run_time,
        "partition_unit": partition_unit,
        "partition_period": partition_period,
        "is_create": bool(raw.get("is_create", True)),
        "create_drop_interval": create_drop_interval,
    }
