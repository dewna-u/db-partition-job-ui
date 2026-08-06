"""Pure helpers for pgAgent job auto-fill. Never execute retrieved SQL."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

_SQL_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_SQL_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# SELECT/CALL/PERFORM schema.function(
_FUNC_CALL_RE = re.compile(
    rf"\b(?:SELECT|CALL|PERFORM)\s+({_IDENT})\s*\.\s*({_IDENT})\s*\(",
    re.IGNORECASE,
)

_PARTITION_OF_RE = re.compile(
    rf"\bPARTITION\s+OF\s+({_IDENT})\s*\.\s*({_IDENT})\b",
    re.IGNORECASE,
)
_ALTER_TABLE_RE = re.compile(
    rf"\bALTER\s+TABLE\s+({_IDENT})\s*\.\s*({_IDENT})\b",
    re.IGNORECASE,
)
_INSERT_INTO_RE = re.compile(
    rf"\bINSERT\s+INTO\s+({_IDENT})\s*\.\s*({_IDENT})\b",
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(
    rf"\bUPDATE\s+({_IDENT})\s*\.\s*({_IDENT})\b",
    re.IGNORECASE,
)
_DELETE_FROM_RE = re.compile(
    rf"\bDELETE\s+FROM\s+({_IDENT})\s*\.\s*({_IDENT})\b",
    re.IGNORECASE,
)
_TRUNCATE_RE = re.compile(
    rf"\bTRUNCATE\s+(?:TABLE\s+)?({_IDENT})\s*\.\s*({_IDENT})\b",
    re.IGNORECASE,
)

_PARTITION_UNIT_ASSIGN_RE = re.compile(
    r"\b(?:v_|p_)?partition_unit\s*:=\s*'?(day|week|month|year)'?",
    re.IGNORECASE,
)
_PARTITION_PERIOD_ASSIGN_RE = re.compile(
    r"\b(?:v_|p_)?partition_period\s*:=\s*'?(\d+)'?",
    re.IGNORECASE,
)
_CREATE_DROP_INTERVAL_ASSIGN_RE = re.compile(
    r"\b(?:v_|p_)?(?:is_)?create_drop_interval\s*:="
    r"[^;]*?(\d+)\s*(day|days|week|weeks|month|months)\b",
    re.IGNORECASE,
)

# Common pattern used by the existing partition functions, for example:
#   next_date := cur_date + INTERVAL '1 month';
# The increment is the strongest signal for the partition width when the
# function does not explicitly assign partition_unit / partition_period.
_NEXT_DATE_INCREMENT_RE = re.compile(
    r"\bnext_date\s*:?=\s*[^;]*?\+\s*interval\s*"
    r"'\s*(\d+)\s*(day|days|week|weeks|month|months|year|years)\s*'",
    re.IGNORECASE,
)

# Existing CREATE/DROP wrapper functions often calculate the first partition
# boundary from cur_date.  We use the *last* interval literal in that
# assignment as the lead/retention interval.  This intentionally ignores
# inner normalisation offsets such as `start_date + interval '7 days'`.
_CUR_DATE_ASSIGNMENT_RE = re.compile(
    r"\bcur_date\s*:?=\s*([^;]+);",
    re.IGNORECASE | re.DOTALL,
)
_INTERVAL_LITERAL_RE = re.compile(
    r"\binterval\s*'\s*(\d+)\s*(day|days|week|weeks|month|months)\s*'",
    re.IGNORECASE,
)

_SECRET_DB_CONFIG_KEYS = frozenset(
    {
        "password",
        "pwd",
        "pass",
        "secret",
        "connstr",
        "connection_string",
        "jstconnstr",
        "dsn",
    }
)

DEFAULT_DB_CONFIG_OBJECT: dict[str, Any] = {
    "work_mem": "512MB",
    "maintenance_work_mem": "1GB",
}


def infer_is_create(job_name: str) -> Optional[bool]:
    """
    Infer Create Partitions from the job name.

    Returns True / False when unambiguous, otherwise None.
    Uses case-insensitive whole-token matching (underscores and punctuation split tokens).
    """
    tokens = {token.upper() for token in _TOKEN_RE.findall(job_name or "")}
    has_create = "CREATE" in tokens
    has_drop = "DROP" in tokens
    if has_create and not has_drop:
        return True
    if has_drop and not has_create:
        return False
    return None


def strip_sql_comments(sql_text: str) -> str:
    text = sql_text or ""
    text = _SQL_COMMENT_BLOCK_RE.sub(" ", text)
    text = _SQL_COMMENT_LINE_RE.sub(" ", text)
    return text


def extract_called_functions(sql_text: str) -> list[tuple[str, str]]:
    """
    Return unique (schema, function) pairs from safe SELECT/CALL/PERFORM forms.

    Does not execute the SQL. Unqualified calls are ignored.
    """
    cleaned = strip_sql_comments(sql_text)
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _FUNC_CALL_RE.finditer(cleaned):
        pair = (match.group(1), match.group(2))
        if pair not in seen:
            seen.add(pair)
            found.append(pair)
    return found


def extract_single_called_function(
    sql_text: str,
) -> tuple[Optional[tuple[str, str]], Optional[str]]:
    """
    Return ((schema, name), warning).

    warning is set when zero or multiple distinct functions are found.
    """
    functions = extract_called_functions(sql_text)
    if not functions:
        return None, (
            "No schema-qualified function call was found in the job step. "
            "Table Schema, Table Name, Partition Unit, Partition Period, "
            "and Create/Drop Interval were left unchanged."
        )
    if len(functions) > 1:
        return None, (
            "Multiple schema-qualified function calls were found in the job step. "
            "The target function was not selected automatically."
        )
    return functions[0], None


def count_top_level_call_arguments(
    sql_text: str, schema: str, function_name: str
) -> Optional[int]:
    """Count top-level arguments for schema.function(...), or None if uncertain."""
    cleaned = strip_sql_comments(sql_text)
    pattern = re.compile(
        rf"\b(?:SELECT|CALL|PERFORM)\s+{re.escape(schema)}\s*\.\s*"
        rf"{re.escape(function_name)}\s*\(",
        re.IGNORECASE,
    )
    match = pattern.search(cleaned)
    if not match:
        return None
    start = match.end()
    depth = 1
    args_empty = True
    arg_count = 0
    i = start
    in_single = False
    in_dollar = False
    while i < len(cleaned):
        ch = cleaned[i]
        nxt = cleaned[i + 1] if i + 1 < len(cleaned) else ""
        if in_single:
            if ch == "'" and nxt == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_dollar:
            if ch == "$" and nxt == "$":
                in_dollar = False
                i += 2
                continue
            i += 1
            continue
        if ch == "'":
            in_single = True
            args_empty = False
            i += 1
            continue
        if ch == "$" and nxt == "$":
            in_dollar = True
            args_empty = False
            i += 2
            continue
        if ch == "(":
            depth += 1
            args_empty = False
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                if args_empty:
                    return 0
                return arg_count + 1
            i += 1
            continue
        if ch == "," and depth == 1:
            arg_count += 1
            args_empty = False
            i += 1
            continue
        if not ch.isspace():
            args_empty = False
        i += 1
    return None


def extract_target_tables(function_definition: str) -> list[tuple[str, str]]:
    """Return unique qualified application-table references from high-confidence SQL."""
    cleaned = strip_sql_comments(function_definition or "")
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for regex in (
        _PARTITION_OF_RE,
        _ALTER_TABLE_RE,
        _INSERT_INTO_RE,
        _UPDATE_RE,
        _DELETE_FROM_RE,
        _TRUNCATE_RE,
    ):
        for match in regex.finditer(cleaned):
            schema = match.group(1)
            table = match.group(2)
            key = (schema.lower(), table.lower())
            if key in seen:
                continue
            if schema.lower() in {"pg_catalog", "information_schema", "pg_toast"}:
                continue
            seen.add(key)
            pairs.append((schema, table))
    return pairs


def extract_single_target_table(
    function_definition: str,
) -> tuple[Optional[tuple[str, str]], Optional[str]]:
    """Return ((schema, table), warning) when exactly one high-confidence table is found."""
    tables = extract_target_tables(function_definition)

    partition_of = []
    cleaned = strip_sql_comments(function_definition or "")
    seen: set[tuple[str, str]] = set()
    for match in _PARTITION_OF_RE.finditer(cleaned):
        pair = (match.group(1), match.group(2))
        key = (pair[0].lower(), pair[1].lower())
        if key not in seen:
            seen.add(key)
            partition_of.append(pair)
    if len(partition_of) == 1:
        return partition_of[0], None

    if not tables:
        return None, (
            "No clear qualified target table was found in the called function. "
            "Table Schema and Table Name were left unchanged."
        )
    if len(tables) > 1:
        return None, (
            "Multiple possible target tables were found in the called function. "
            "Table Schema and Table Name were left unchanged."
        )
    return tables[0], None


def extract_partition_settings(
    function_definition: str,
) -> tuple[dict[str, Any], list[str]]:
    """
    Extract partition unit / period / create-drop interval only when explicit.

    Returns (values, warnings).
    """
    cleaned = strip_sql_comments(function_definition or "")
    values: dict[str, Any] = {}
    warnings: list[str] = []

    units = [m.group(1).lower() for m in _PARTITION_UNIT_ASSIGN_RE.finditer(cleaned)]
    unique_units = list(dict.fromkeys(units))
    if len(unique_units) == 1:
        values["partition_unit"] = unique_units[0]
    elif len(unique_units) > 1:
        warnings.append(
            "Multiple partition unit values were found in the function definition. "
            "Partition Unit was left unchanged."
        )
    else:
        # Fall back to the actual boundary increment used by the partition
        # function (for example next_date := cur_date + interval '1 month').
        increments = [
            (int(m.group(1)), m.group(2).lower().rstrip("s"))
            for m in _NEXT_DATE_INCREMENT_RE.finditer(cleaned)
        ]
        unique_increments = list(dict.fromkeys(increments))
        if len(unique_increments) == 1:
            values["partition_unit"] = unique_increments[0][1]
        else:
            warnings.append(
                "Partition Unit could not be determined reliably from the function definition."
            )

    periods = [int(m.group(1)) for m in _PARTITION_PERIOD_ASSIGN_RE.finditer(cleaned)]
    unique_periods = list(dict.fromkeys(periods))
    if len(unique_periods) == 1 and unique_periods[0] >= 1:
        values["partition_period"] = unique_periods[0]
    elif len(unique_periods) > 1:
        warnings.append(
            "Multiple partition period values were found in the function definition. "
            "Partition Period was left unchanged."
        )
    else:
        increments = [
            (int(m.group(1)), m.group(2).lower().rstrip("s"))
            for m in _NEXT_DATE_INCREMENT_RE.finditer(cleaned)
        ]
        unique_increments = list(dict.fromkeys(increments))
        if len(unique_increments) == 1 and unique_increments[0][0] >= 1:
            values["partition_period"] = unique_increments[0][0]
        else:
            warnings.append(
                "Partition Period could not be determined reliably from the function definition."
            )

    intervals: list[tuple[int, str]] = []
    for match in _CREATE_DROP_INTERVAL_ASSIGN_RE.finditer(cleaned):
        amount = int(match.group(1))
        unit = match.group(2).lower().rstrip("s")
        if amount >= 1 and unit in {"day", "week", "month"}:
            intervals.append((amount, unit))
    unique_intervals = list(dict.fromkeys(intervals))
    if len(unique_intervals) == 1:
        values["create_drop_amount"] = unique_intervals[0][0]
        values["create_drop_unit"] = unique_intervals[0][1]
    elif len(unique_intervals) > 1:
        warnings.append(
            "Multiple create/drop interval values were found in the function definition. "
            "Create/Drop Interval was left unchanged."
        )
    else:
        # For the existing wrapper functions the first partition boundary is
        # derived from cur_date.  Take the outer/last interval in that
        # assignment, e.g. `... + interval '7 days' + interval '2 months'`
        # becomes 2 months rather than the inner 7-day date-normalisation.
        inferred_offsets: list[tuple[int, str]] = []
        for assignment in _CUR_DATE_ASSIGNMENT_RE.finditer(cleaned):
            literals = list(_INTERVAL_LITERAL_RE.finditer(assignment.group(1)))
            if literals:
                last = literals[-1]
                amount = int(last.group(1))
                unit = last.group(2).lower().rstrip("s")
                if amount >= 1:
                    inferred_offsets.append((amount, unit))
        unique_offsets = list(dict.fromkeys(inferred_offsets))
        if len(unique_offsets) == 1:
            values["create_drop_amount"] = unique_offsets[0][0]
            values["create_drop_unit"] = unique_offsets[0][1]
        else:
            warnings.append(
                "Create/Drop Interval could not be determined reliably from the function definition."
            )

    return values, warnings


def _as_bool_list(value: Any) -> list[bool]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [bool(item) for item in value]
    if isinstance(value, str):
        inner = value.strip()
        if inner.startswith("{") and inner.endswith("}"):
            inner = inner[1:-1]
        if not inner.strip():
            return []
        result: list[bool] = []
        for part in inner.split(","):
            token = part.strip().lower()
            result.append(token in {"t", "true", "1", "yes"})
        return result
    return []


def _selected_indexes(flags: list[bool]) -> list[int]:
    return [index for index, flag in enumerate(flags) if flag]


def _cron_field_from_flags(flags: list[bool], expected_length: int) -> Optional[str]:
    if not flags:
        return None
    if expected_length and len(flags) < expected_length:
        return None
    usable = flags[:expected_length] if expected_length else flags
    selected = _selected_indexes(usable)
    if not selected:
        # pgAgent treats an all-false schedule array as a wildcard for that
        # dimension (minutes, hours, weekdays, month-days, or months).
        return "*"
    if len(selected) == len(usable):
        return "*"
    return ",".join(str(index) for index in selected)


def convert_pgagent_schedule_to_cron(
    minutes: Any,
    hours: Any,
    monthdays: Any,
    months: Any,
    weekdays: Any,
) -> tuple[Optional[str], list[str]]:
    """
    Convert pgAgent boolean arrays to a six-field schedule:

        second minute hour day-of-month month day-of-week

    Seconds are always 0. Returns (schedule_or_None, warnings).
    """
    warnings: list[str] = []
    minute_flags = _as_bool_list(minutes)
    hour_flags = _as_bool_list(hours)
    monthday_flags = _as_bool_list(monthdays)
    month_flags = _as_bool_list(months)
    weekday_flags = _as_bool_list(weekdays)

    minute_field = _cron_field_from_flags(minute_flags, 60)
    hour_field = _cron_field_from_flags(hour_flags, 24)
    month_field = _cron_field_from_flags(month_flags, 12)
    weekday_field = _cron_field_from_flags(weekday_flags, 7)

    last_day = False
    day_flags_for_cron = monthday_flags[:31] if monthday_flags else []
    if len(monthday_flags) >= 32 and monthday_flags[31]:
        last_day = True
    day_field = _cron_field_from_flags(day_flags_for_cron, 31)

    if last_day:
        warnings.append(
            "The pgAgent schedule uses last-day-of-month, which cannot be represented "
            "safely in the six-field schedule format. Job Schedule was left unchanged."
        )
        return None, warnings

    # pgAgent monthdays[0] = day 1 ... [30] = day 31. Cron day-of-month is 1-31.
    if day_field and day_field != "*":
        cron_days = []
        for token in day_field.split(","):
            cron_days.append(str(int(token) + 1))
        day_field = ",".join(cron_days)

    # pgAgent months[0] = January. Cron months are 1-12.
    if month_field and month_field != "*":
        cron_months = []
        for token in month_field.split(","):
            cron_months.append(str(int(token) + 1))
        month_field = ",".join(cron_months)

    if None in (minute_field, hour_field, day_field, month_field, weekday_field):
        warnings.append(
            "The pgAgent schedule could not be converted into a valid six-field schedule. "
            "Job Schedule was left unchanged."
        )
        return None, warnings

    # Cron ORs day-of-month and day-of-week when both are restricted; pgAgent ANDs them.
    if day_field != "*" and weekday_field != "*":
        warnings.append(
            "The pgAgent schedule restricts both day-of-month and day-of-week. "
            "That combination cannot be represented safely in cron. "
            "Job Schedule was left unchanged."
        )
        return None, warnings

    schedule = f"0 {minute_field} {hour_field} {day_field} {month_field} {weekday_field}"
    return schedule, warnings


def infer_frequency(
    schedule: str,
) -> tuple[Optional[tuple[int, str]], Optional[str]]:
    """
    Infer frequency only for unambiguous common cases.

    Returns ((amount, unit), warning).
    """
    fields = (schedule or "").split()
    if len(fields) == 6:
        _second, minute, hour, day, month, weekday = fields
    elif len(fields) == 5:
        minute, hour, day, month, weekday = fields
    else:
        return None, "Frequency could not be inferred from the schedule."

    def _single_number(field: str) -> bool:
        return bool(re.fullmatch(r"\d+", field))

    if month != "*":
        return None, (
            "Frequency was not inferred because the schedule is not a simple "
            "repeating minute/hour/day/week pattern."
        )

    # Every hour: one minute, every hour, every day, every weekday.
    if _single_number(minute) and hour == "*" and day == "*" and weekday == "*":
        return (1, "hour"), None

    # Every day: one minute, one hour, every day, every weekday.
    if (
        _single_number(minute)
        and _single_number(hour)
        and day == "*"
        and weekday == "*"
    ):
        return (1, "day"), None

    # Every week: one minute, one hour, every month-day, one weekday.
    if (
        _single_number(minute)
        and _single_number(hour)
        and day == "*"
        and _single_number(weekday)
    ):
        return (1, "week"), None

    # Same day every month — interval would be 1 month, which the form cannot represent.
    if (
        _single_number(minute)
        and _single_number(hour)
        and _single_number(day)
        and weekday == "*"
    ):
        return None, (
            "The schedule looks like the same day every month. "
            "Frequency was left unchanged because the form only accepts "
            "minute, hour, day, or week."
        )

    return None, "Frequency could not be inferred reliably from the schedule."


def build_db_config_json(dbname: Optional[str]) -> Optional[str]:
    """Build the expected JSON object, adding dbname when available. No secrets."""
    if not dbname or not str(dbname).strip():
        return None
    payload = dict(DEFAULT_DB_CONFIG_OBJECT)
    payload["dbname"] = str(dbname).strip()
    return json.dumps(payload, indent=2)


def sanitize_db_config_object(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop credential-like keys if a dict is ever merged into db config."""
    return {
        key: value
        for key, value in raw.items()
        if str(key).lower() not in _SECRET_DB_CONFIG_KEYS
    }