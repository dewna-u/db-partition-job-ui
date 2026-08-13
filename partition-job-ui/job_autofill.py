"""Pure helpers for pgAgent job auto-fill. Never execute retrieved SQL."""

from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Optional, Set

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

_SQL_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_SQL_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# A pgAgent step invokes a routine as either SELECT schema.function(...) or
# CALL [schema.]procedure(...). SELECT/PERFORM require a schema qualifier,
# because an unqualified SELECT would also match ordinary expressions such as
# SELECT now(). CALL can only invoke a procedure, so it is accepted with or
# without a schema and the schema is resolved from the catalog instead.
_QUALIFIED_ROUTINE_CALL_RE = re.compile(
    rf"\b(SELECT|PERFORM|CALL)\s+({_IDENT})\s*\.\s*({_IDENT})\s*\(",
    re.IGNORECASE,
)
_UNQUALIFIED_CALL_RE = re.compile(
    rf"\b(CALL)\s+({_IDENT})\s*\(",
    re.IGNORECASE,
)

INVOCATION_CALL = "CALL"
INVOCATION_SELECT = "SELECT"

# pg_proc.prokind
PROKIND_FUNCTION = "f"
PROKIND_PROCEDURE = "p"

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

# A regclass literal names a relation directly, for example
#   inhparent = 's01_holdings_summary'::regclass
# and may or may not carry a schema.
_REGCLASS_LITERAL_RE = re.compile(
    rf"'\s*({_IDENT})\s*(?:\.\s*({_IDENT})\s*)?'\s*::\s*regclass",
    re.IGNORECASE,
)

# Unqualified relation references. The negative lookahead keeps these from
# firing on a qualified name, which the qualified patterns above already cover.
_ALTER_TABLE_UNQUALIFIED_RE = re.compile(
    rf"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?({_IDENT})\b(?!\s*\.)",
    re.IGNORECASE,
)
_PARTITION_OF_UNQUALIFIED_RE = re.compile(
    rf"\bPARTITION\s+OF\s+({_IDENT})\b(?!\s*\.)",
    re.IGNORECASE,
)

# Relation names that are never the partitioned application table.
_NON_TARGET_TABLE_NAMES = frozenset(
    {
        "pg_class",
        "pg_inherits",
        "pg_namespace",
        "pg_partitioned_table",
        "pg_tables",
        "pg_proc",
    }
)

_SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})

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
    r"[^;]*?(\d+)\s*(day|days|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)

_INTERVAL_UNITS = r"day|days|week|weeks|month|months|year|years"

# Common pattern used by the existing partition functions, for example:
#   next_date := cur_date + INTERVAL '1 month';
# The increment is the strongest signal for the partition width when the
# routine does not explicitly assign partition_unit / partition_period.
_NEXT_DATE_INCREMENT_RE = re.compile(
    rf"\bnext_date\s*:?=\s*[^;]*?\+\s*interval\s*'\s*(\d+)\s*({_INTERVAL_UNITS})\s*'",
    re.IGNORECASE,
)

# A boundary variable advancing by itself, for example
#   v_start := v_start + INTERVAL '1 month';
# This is how a partition loop walks forward regardless of naming convention,
# so it identifies the partition width in procedures that do not use next_date.
_BOUNDARY_SELF_INCREMENT_RE = re.compile(
    rf"\b({_IDENT})\s*:=\s*\1\s*\+\s*interval\s*'\s*(\d+)\s*({_INTERVAL_UNITS})\s*'",
    re.IGNORECASE,
)

# generate_series(from, to, INTERVAL 'n unit') — the step is the partition width.
_GENERATE_SERIES_STEP_RE = re.compile(
    rf"\bgenerate_series\s*\([^;()]*?,[^;()]*?,\s*(?:interval\s*)?"
    rf"'\s*(\d+)\s*({_INTERVAL_UNITS})\s*'",
    re.IGNORECASE | re.DOTALL,
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
    rf"\binterval\s*'\s*(\d+)\s*({_INTERVAL_UNITS})\s*'",
    re.IGNORECASE,
)

# Partition DDL evidence. These are matched against the function body *with SQL
# string literals left intact*, because partition DDL is normally assembled
# inside EXECUTE format('...') and stripping literals would discard the only
# proof of what a function actually does.
_ADD_PARTITION_RE = re.compile(r"\bADD\s+PARTITION\b", re.IGNORECASE)
_DROP_PARTITION_RE = re.compile(r"\bDROP\s+PARTITION\b", re.IGNORECASE)
_ATTACH_PARTITION_RE = re.compile(r"\bATTACH\s+PARTITION\b", re.IGNORECASE)
_DETACH_PARTITION_RE = re.compile(r"\bDETACH\s+PARTITION\b", re.IGNORECASE)
_DROP_TABLE_RE = re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)
# Declarative PostgreSQL partitioning: CREATE TABLE child PARTITION OF parent.
# The bounded gap keeps an unrelated CREATE TABLE elsewhere in the body from
# pairing with a distant PARTITION OF.
_CREATE_TABLE_PARTITION_OF_RE = re.compile(
    r"\bCREATE\s+TABLE\b[\s\S]{0,400}?\bPARTITION\s+OF\b", re.IGNORECASE
)

# RAISE messages describe behaviour in prose and routinely contain words such as
# "drop table", so they are removed before evidence matching.
_RAISE_STATEMENT_RE = re.compile(
    r"\bRAISE\s+(?:DEBUG|LOG|INFO|NOTICE|WARNING|EXCEPTION)\b[^;]*;",
    re.IGNORECASE,
)

_CREATE_EVIDENCE_RULES = (
    ("ALTER TABLE ... ADD PARTITION", _ADD_PARTITION_RE),
    ("CREATE TABLE ... PARTITION OF", _CREATE_TABLE_PARTITION_OF_RE),
    ("ALTER TABLE ... ATTACH PARTITION", _ATTACH_PARTITION_RE),
)
_DROP_EVIDENCE_RULES = (
    ("ALTER TABLE ... DROP PARTITION", _DROP_PARTITION_RE),
    ("ALTER TABLE ... DETACH PARTITION", _DETACH_PARTITION_RE),
    ("DROP TABLE", _DROP_TABLE_RE),
)

OPERATION_CREATE = "CREATE"
OPERATION_DROP = "DROP"
OPERATION_AMBIGUOUS = "AMBIGUOUS"
OPERATION_UNKNOWN = "UNKNOWN"

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

_QUOTED_LITERAL = r"'(?:[^']|'')*'"
_UNQUOTED_VALUE = r"[A-Za-z0-9_.+-]+"
_CONFIG_PARAM = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"

# A configuration SET must begin a statement. Requiring a preceding statement
# boundary is what stops "UPDATE t SET col = 'x'" from being read as database
# configuration. The trailing semicolon is a lookahead so that it stays
# available as the boundary of the statement that follows.
_SET_STATEMENT_RE = re.compile(
    r"(?:\A|;|\bBEGIN\b|\bTHEN\b|\bELSE\b|\bLOOP\b|\$[A-Za-z_]*\$)\s*"
    rf"SET\s+(?:LOCAL\s+|SESSION\s+)?({_CONFIG_PARAM})\s*(?:=|\bTO\b)\s*"
    rf"({_QUOTED_LITERAL}|{_UNQUOTED_VALUE})\s*(?=;)",
    re.IGNORECASE,
)

# set_config(parameter, value, is_local)
_SET_CONFIG_CALL_RE = re.compile(
    rf"\bset_config\s*\(\s*({_QUOTED_LITERAL}|[^,()]+?)\s*,\s*"
    rf"({_QUOTED_LITERAL}|[^,()]+?)\s*,",
    re.IGNORECASE,
)

# SET forms that change session state rather than a configuration parameter.
_NON_CONFIG_SET_KEYWORDS = frozenset(
    {
        "authorization",
        "constraints",
        "local",
        "names",
        "role",
        "schema",
        "session",
        "time",
        "transaction",
    }
)

# A value meaning "reset this parameter", not a concrete setting.
_NON_CONFIG_SET_VALUES = frozenset({"default"})


def infer_is_create(job_name: str) -> Optional[bool]:
    """
    Infer Create Partitions from a job or function *name*.

    This is a weak fallback only. A name is metadata; the executable function
    body is authoritative and is inspected by
    infer_partition_operation_from_function(). Callers must not use this to
    override body evidence.

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


class PartitionOperationEvidence(NamedTuple):
    """Outcome of inspecting a partition function body."""

    operation: str
    is_create: Optional[bool]
    reason: str
    create_evidence: tuple
    drop_evidence: tuple


def executable_sql_for_analysis(function_definition: str) -> str:
    """
    Return the parts of a function definition that actually execute.

    Comments and RAISE messages are removed because they describe behaviour
    rather than perform it. SQL string literals are deliberately KEPT: dynamic
    partition DDL lives inside EXECUTE format('...'), and discarding literals
    would remove the strongest available evidence.
    """
    cleaned = strip_sql_comments(function_definition or "")
    return _RAISE_STATEMENT_RE.sub(" ", cleaned)


def infer_partition_operation_from_function(
    function_definition: str,
) -> PartitionOperationEvidence:
    """
    Determine CREATE vs DROP from the executable body of a partition function.

    Evidence is collected from partition DDL patterns rather than from any
    single substring, so names, comments and prose cannot influence the result.
    Conflicting or absent evidence yields no verdict — an unknown operation is
    reported instead of a guess.
    """
    analysed = executable_sql_for_analysis(function_definition)
    create_evidence = tuple(
        label for label, regex in _CREATE_EVIDENCE_RULES if regex.search(analysed)
    )
    drop_evidence = tuple(
        label for label, regex in _DROP_EVIDENCE_RULES if regex.search(analysed)
    )

    if create_evidence and not drop_evidence:
        return PartitionOperationEvidence(
            OPERATION_CREATE,
            True,
            "Detected partition-creating SQL in the routine definition: "
            + ", ".join(create_evidence)
            + ".",
            create_evidence,
            drop_evidence,
        )
    if drop_evidence and not create_evidence:
        return PartitionOperationEvidence(
            OPERATION_DROP,
            False,
            "Detected partition-removing SQL in the routine definition: "
            + ", ".join(drop_evidence)
            + ".",
            create_evidence,
            drop_evidence,
        )
    if create_evidence and drop_evidence:
        return PartitionOperationEvidence(
            OPERATION_AMBIGUOUS,
            None,
            "The routine definition contains both partition-creating SQL ("
            + ", ".join(create_evidence)
            + ") and partition-removing SQL ("
            + ", ".join(drop_evidence)
            + ").",
            create_evidence,
            drop_evidence,
        )
    return PartitionOperationEvidence(
        OPERATION_UNKNOWN,
        None,
        "No partition-creating or partition-removing SQL was found in the "
        "routine definition.",
        create_evidence,
        drop_evidence,
    )


def infer_is_create_from_definition(function_definition: str) -> Optional[bool]:
    """
    Infer Create Partitions from function SQL behaviour.

    Thin wrapper over infer_partition_operation_from_function() for callers that
    only need the boolean. Returns True / False when unambiguous, otherwise None.
    """
    return infer_partition_operation_from_function(function_definition).is_create


def strip_sql_comments(sql_text: str) -> str:
    text = sql_text or ""
    text = _SQL_COMMENT_BLOCK_RE.sub(" ", text)
    text = _SQL_COMMENT_LINE_RE.sub(" ", text)
    return text


class CalledRoutine(NamedTuple):
    """A routine invocation parsed out of a pgAgent job step."""

    schema: Optional[str]
    name: str
    invocation: str
    argument_count: Optional[int]

    @property
    def expected_prokind(self) -> str:
        """CALL can only invoke a procedure; SELECT/PERFORM only a function."""
        return (
            PROKIND_PROCEDURE
            if self.invocation == INVOCATION_CALL
            else PROKIND_FUNCTION
        )

    @property
    def display_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


def extract_called_routines(sql_text: str) -> list[CalledRoutine]:
    """
    Return the routines a job step invokes, in the order they appear.

    Handles `SELECT schema.function(...)`, `CALL schema.procedure(...)` and
    `CALL procedure(...)`, including calls wrapped in BEGIN ... END and spread
    over several lines. Never executes the SQL; comments are stripped first.
    An unqualified schema stays None so it can be resolved from the catalog
    rather than assumed.
    """
    cleaned = strip_sql_comments(sql_text)
    found: list[CalledRoutine] = []
    seen: set = set()

    for regex, qualified in (
        (_QUALIFIED_ROUTINE_CALL_RE, True),
        (_UNQUALIFIED_CALL_RE, False),
    ):
        for match in regex.finditer(cleaned):
            keyword = match.group(1).upper()
            invocation = (
                INVOCATION_CALL if keyword == INVOCATION_CALL else INVOCATION_SELECT
            )
            if qualified:
                schema: Optional[str] = match.group(2)
                name = match.group(3)
            else:
                schema = None
                name = match.group(2)
            key = ((schema or "").lower(), name.lower())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                CalledRoutine(
                    schema=schema,
                    name=name,
                    invocation=invocation,
                    argument_count=_scan_top_level_arguments(cleaned, match.end()),
                )
            )
    return found


def extract_single_called_routine(
    sql_text: str,
) -> tuple[Optional[CalledRoutine], Optional[str]]:
    """Return (routine, warning); warning is set when zero or several are found."""
    routines = extract_called_routines(sql_text)
    if not routines:
        return None, (
            "No PostgreSQL routine call was found in the job step. A partition step "
            "should call SELECT schema.function(...) or CALL [schema.]procedure(...). "
            "Table Schema, Table Name, Partition Unit, Partition Period, "
            "and Create/Drop Interval were left unchanged."
        )
    if len(routines) > 1:
        names = ", ".join(routine.display_name for routine in routines)
        return None, (
            f"The job step calls more than one routine ({names}). The target routine "
            "was not selected automatically."
        )
    return routines[0], None


def extract_called_functions(sql_text: str) -> list[tuple[str, str]]:
    """
    Return unique schema-qualified (schema, name) pairs called by the SQL.

    Kept for callers that need a qualified pair specifically, such as detecting
    the two generic scanner jobs by the function they call.
    """
    pairs: list[tuple[str, str]] = []
    for routine in extract_called_routines(sql_text):
        if routine.schema:
            pairs.append((routine.schema, routine.name))
    return pairs


def extract_single_called_function(
    sql_text: str,
) -> tuple[Optional[tuple[str, str]], Optional[str]]:
    """Return ((schema, name), warning) for a schema-qualified call only."""
    routine, warning = extract_single_called_routine(sql_text)
    if routine is None:
        return None, warning
    if routine.schema is None:
        return None, (
            f"The job step calls {routine.name} without a schema, so it could not be "
            "identified from the step text alone."
        )
    return (routine.schema, routine.name), None


def _scan_top_level_arguments(cleaned: str, start: int) -> Optional[int]:
    """
    Count comma-separated arguments starting just after an opening parenthesis.

    Nested parentheses, string literals and dollar quotes are skipped, so commas
    inside expressions such as TO_CHAR(DATE_TRUNC('year', d), 'YYYY') are not
    counted as additional arguments. Returns None if the call is unterminated.
    """
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


def extract_target_table_references(
    routine_definition: str,
) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Return (qualified pairs, unqualified names) referenced as relations.

    Works on function and procedure definitions alike. Unqualified names are
    returned so the caller can resolve them against the catalog instead of
    guessing a schema. A name that also appears qualified is not repeated in the
    unqualified list.
    """
    cleaned = strip_sql_comments(routine_definition or "")

    qualified: list[tuple[str, str]] = list(extract_target_tables(routine_definition))
    seen_qualified = {(schema.lower(), table.lower()) for schema, table in qualified}

    unqualified: list[str] = []
    seen_unqualified: set = set()

    def _add_unqualified(name: str) -> None:
        key = name.lower()
        if key in _NON_TARGET_TABLE_NAMES or key in seen_unqualified:
            return
        seen_unqualified.add(key)
        unqualified.append(name)

    for match in _REGCLASS_LITERAL_RE.finditer(cleaned):
        first, second = match.group(1), match.group(2)
        if second:
            key = (first.lower(), second.lower())
            if first.lower() not in _SYSTEM_SCHEMAS and key not in seen_qualified:
                seen_qualified.add(key)
                qualified.append((first, second))
        else:
            _add_unqualified(first)

    for regex in (_ALTER_TABLE_UNQUALIFIED_RE, _PARTITION_OF_UNQUALIFIED_RE):
        for match in regex.finditer(cleaned):
            _add_unqualified(match.group(1))

    qualified_names = {table.lower() for _schema, table in qualified}
    unqualified = [name for name in unqualified if name.lower() not in qualified_names]
    return qualified, unqualified


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


def partition_boundary_increments(routine_definition: str) -> list:
    """
    Return the interval steps by which the routine advances partition boundaries.

    Only advancement/generation idioms count: a self-incrementing boundary
    variable, a next_date assignment, or a generate_series step. Interval
    literals used for retention windows, look-ahead offsets or plain date
    arithmetic are deliberately excluded, because they are not the partition
    width even though they appear in the same routine.
    """
    cleaned = strip_sql_comments(routine_definition or "")
    increments: list = []
    for regex, amount_group, unit_group in (
        (_NEXT_DATE_INCREMENT_RE, 1, 2),
        (_BOUNDARY_SELF_INCREMENT_RE, 2, 3),
        (_GENERATE_SERIES_STEP_RE, 1, 2),
    ):
        for match in regex.finditer(cleaned):
            amount = int(match.group(amount_group))
            unit = match.group(unit_group).lower().rstrip("s")
            if amount >= 1:
                increments.append((amount, unit))
    return list(dict.fromkeys(increments))


def extract_partition_settings(
    routine_definition: str,
) -> tuple[dict[str, Any], list[str]]:
    """
    Extract partition unit / period / create-drop interval only when explicit.

    Works on function and procedure definitions alike. Returns (values, warnings).
    """
    cleaned = strip_sql_comments(routine_definition or "")
    values: dict[str, Any] = {}
    warnings: list[str] = []
    increments = partition_boundary_increments(routine_definition)

    units = [m.group(1).lower() for m in _PARTITION_UNIT_ASSIGN_RE.finditer(cleaned)]
    unique_units = list(dict.fromkeys(units))
    if len(unique_units) == 1:
        values["partition_unit"] = unique_units[0]
    elif len(unique_units) > 1:
        warnings.append(
            "Multiple partition unit values were found in the routine definition. "
            "Partition Unit was left unchanged."
        )
    elif len(increments) == 1:
        values["partition_unit"] = increments[0][1]
    else:
        warnings.append(
            "Partition Unit could not be determined reliably from the routine "
            "definition. Set the partition size to match how the routine "
            "subdivides its date range."
        )

    periods = [int(m.group(1)) for m in _PARTITION_PERIOD_ASSIGN_RE.finditer(cleaned)]
    unique_periods = list(dict.fromkeys(periods))
    if len(unique_periods) == 1 and unique_periods[0] >= 1:
        values["partition_period"] = unique_periods[0]
    elif len(unique_periods) > 1:
        warnings.append(
            "Multiple partition period values were found in the routine definition. "
            "Partition Period was left unchanged."
        )
    elif len(increments) == 1:
        values["partition_period"] = increments[0][0]
    else:
        warnings.append(
            "Partition Period could not be determined reliably from the routine "
            "definition."
        )

    intervals: list[tuple[int, str]] = []
    for match in _CREATE_DROP_INTERVAL_ASSIGN_RE.finditer(cleaned):
        amount = int(match.group(1))
        unit = match.group(2).lower().rstrip("s")
        if amount >= 1 and unit in {"day", "week", "month", "year"}:
            intervals.append((amount, unit))
    unique_intervals = list(dict.fromkeys(intervals))
    if len(unique_intervals) == 1:
        values["create_drop_amount"] = unique_intervals[0][0]
        values["create_drop_unit"] = unique_intervals[0][1]
    elif len(unique_intervals) > 1:
        warnings.append(
            "Multiple create/drop interval values were found in the routine definition. "
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
            # Deliberately unresolved. A routine can contain several unrelated
            # interval literals (look-ahead offsets, retention windows, date
            # normalisation), and picking one would produce a plausible but wrong
            # configuration, so the user is asked instead.
            warnings.append(
                "Create/Drop Interval could not be determined reliably from the "
                "routine definition, because no single look-ahead or retention "
                "interval could be identified. Enter how far ahead partitions "
                "should be created, or how long they should be retained."
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


def _single_cron_number(field: str) -> bool:
    return bool(re.fullmatch(r"\d+", field or ""))


def _cyclic_month_gap(months: list) -> Optional[int]:
    """
    Return the constant gap between selected months, or None if it is uneven.

    Months are cyclic, so the wrap from the last selection back to the first
    counts as a gap too: [3, 6, 9, 12] gives 3, while [1, 4, 12] gives None.
    """
    ordered = sorted(set(months))
    if not ordered:
        return None
    if len(ordered) == 1:
        return 12
    gaps = [
        second - first for first, second in zip(ordered, ordered[1:])
    ]
    gaps.append(ordered[0] + 12 - ordered[-1])
    first_gap = gaps[0]
    if any(gap != first_gap for gap in gaps):
        return None
    return first_gap


def _infer_month_restricted_frequency(
    minute: str, hour: str, day: str, month: str, weekday: str
) -> tuple[Optional[tuple[int, str]], Optional[str]]:
    """
    Infer the cadence of a schedule that only runs in specific months.

    A single selected month is an annual job; an evenly spaced set of months is
    a fixed month interval. Anything uneven stays calendar-driven, because
    forcing a frequency onto it would misrepresent the schedule.
    """
    if not (
        _single_cron_number(minute)
        and _single_cron_number(hour)
        and _single_cron_number(day)
        and _is_cron_wildcard(weekday)
    ):
        return None, (
            "Frequency was not inferred because the schedule restricts specific "
            "months without a single fixed time and day-of-month. The Job Schedule "
            "still drives execution; set Frequency manually."
        )

    months = _parse_cron_field(month, 1, 12, _MONTH_NAME_VALUES)
    if not months:
        return None, "Frequency could not be inferred from the schedule."

    gap = _cyclic_month_gap(list(months))
    if gap is None:
        return None, (
            "Frequency was not inferred because the selected months are unevenly "
            "spaced, so no single repeating interval describes them. The Job "
            "Schedule still drives execution; set Frequency manually."
        )
    if gap == 12:
        return (1, "year"), None
    return (gap, "month"), None


def infer_frequency(
    schedule: str,
) -> tuple[Optional[tuple[int, str]], Optional[str]]:
    """
    Infer how often the job runs from a simple cron schedule.

    Frequency is the run cadence (not the partition period).
    Returns ((amount, unit), warning).
    """
    fields = (normalize_cron_expression(schedule) or schedule or "").split()
    if len(fields) == 6:
        _second, minute, hour, day, month, weekday = fields
    elif len(fields) == 5:
        minute, hour, day, month, weekday = fields
    else:
        return None, "Frequency could not be inferred from the schedule."

    if not _is_cron_wildcard(month):
        return _infer_month_restricted_frequency(minute, hour, day, month, weekday)

    # Every minute: every minute of every hour/day.
    if minute == "*" and hour == "*" and day == "*" and weekday == "*":
        return (1, "minute"), None

    # Every hour: one fixed minute, every hour.
    if (
        _single_cron_number(minute)
        and hour == "*"
        and day == "*"
        and weekday == "*"
    ):
        return (1, "hour"), None

    # Every day: one fixed minute and hour, every calendar day.
    if (
        _single_cron_number(minute)
        and _single_cron_number(hour)
        and day == "*"
        and weekday == "*"
    ):
        return (1, "day"), None

    # Every week: one fixed minute/hour and one weekday.
    if (
        _single_cron_number(minute)
        and _single_cron_number(hour)
        and day == "*"
        and _single_cron_number(weekday)
    ):
        return (1, "week"), None

    # Same calendar day every month (for example 0 0 2 26 * *).
    if (
        _single_cron_number(minute)
        and _single_cron_number(hour)
        and _single_cron_number(day)
        and weekday == "*"
    ):
        return (1, "month"), None

    return None, (
        "Frequency could not be inferred reliably from the schedule. "
        "Complex schedules were left unchanged."
    )


_MONTH_NAME_VALUES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_WEEKDAY_NAME_VALUES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

# "?" means "no specific value" in Quartz-style schedules; treated as a wildcard.
_CRON_WILDCARDS = ("*", "?")


def _is_cron_wildcard(field: str) -> bool:
    return (field or "").strip() in _CRON_WILDCARDS


def _cron_token_value(token: str, names: Optional[dict]) -> Optional[int]:
    text = (token or "").strip().lower()
    if names and text in names:
        return names[text]
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def _parse_cron_field(
    field: str,
    minimum: int,
    maximum: int,
    names: Optional[dict] = None,
) -> Optional[Set[int]]:
    """
    Parse a single cron field into allowed integers, or None if unsupported.

    Supports wildcards (* and ?), single values, comma lists, ranges, step
    expressions on wildcards and ranges, and symbolic month/day names.
    """
    text = (field or "").strip()
    if not text:
        return None
    if text in _CRON_WILDCARDS:
        return set(range(minimum, maximum + 1))

    values: Set[int] = set()
    for part in text.split(","):
        token = part.strip()
        if not token:
            return None

        step = 1
        if "/" in token:
            base, _, step_text = token.partition("/")
            if not re.fullmatch(r"\d+", step_text.strip()):
                return None
            step = int(step_text)
            if step < 1:
                return None
            token = base.strip()
            if not token:
                return None

        if token in _CRON_WILDCARDS:
            start, end = minimum, maximum
        elif "-" in token:
            start_text, _, end_text = token.partition("-")
            start_value = _cron_token_value(start_text, names)
            end_value = _cron_token_value(end_text, names)
            if start_value is None or end_value is None:
                return None
            start, end = start_value, end_value
        else:
            single = _cron_token_value(token, names)
            if single is None:
                return None
            start = end = single

        if start > end or start < minimum or end > maximum:
            return None
        values.update(range(start, end + 1, step))

    return values if values else None


CRON_FIELD_BOUNDS = (
    ("second", 0, 59, None),
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day-of-month", 1, 31, None),
    ("month", 1, 12, _MONTH_NAME_VALUES),
    ("day-of-week", 0, 6, _WEEKDAY_NAME_VALUES),
)


def normalize_cron_expression(schedule: str) -> Optional[str]:
    """
    Rewrite a valid six-field schedule into purely numeric form.

    Symbolic names become numbers and "?" becomes "*", so downstream inference
    works on one canonical representation. Returns None if the schedule is not
    six fields.
    """
    fields = (schedule or "").split()
    if len(fields) != 6:
        return None

    normalised: list[str] = []
    for value, (_name, _minimum, _maximum, names) in zip(fields, CRON_FIELD_BOUNDS):
        if value == "?":
            normalised.append("*")
            continue
        if names:
            def _replace(match: "re.Match") -> str:
                mapped = names.get(match.group(0).lower())
                return str(mapped) if mapped is not None else match.group(0)

            value = re.sub(r"[A-Za-z]+", _replace, value)
        normalised.append(value)
    return " ".join(normalised)

_MONTH_LABELS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_WEEKDAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


def validate_six_field_cron(schedule: str) -> Optional[str]:
    """
    Validate a six-field schedule (second minute hour day-of-month month day-of-week).

    Returns None when valid, otherwise a user-facing error message.
    """
    fields = (schedule or "").split()
    if len(fields) != 6:
        return (
            "Job Schedule must contain exactly six whitespace-separated fields: "
            "second minute hour day-of-month month day-of-week."
        )
    for value, (name, minimum, maximum, names) in zip(fields, CRON_FIELD_BOUNDS):
        if _parse_cron_field(value, minimum, maximum, names) is None:
            return (
                f"The {name} field of the Job Schedule is invalid: '{value}'. "
                f"Use * or ?, a number between {minimum} and {maximum}, a list, "
                "a range, or a step value."
            )
    return None


def describe_schedule(schedule: str) -> Optional[str]:
    """Return a short human-readable meaning for common six-field schedules."""
    if validate_six_field_cron(schedule):
        return None
    normalised = normalize_cron_expression(schedule)
    if not normalised:
        return None
    second, minute, hour, day, month, weekday = normalised.split()

    def _step(field: str) -> Optional[int]:
        if field.startswith("*/"):
            try:
                return int(field[2:])
            except ValueError:
                return None
        return None

    if _step(second) and minute == "*" and hour == "*":
        return f"Every {_step(second)} seconds"
    if _step(minute) and hour == "*" and day == "*" and month == "*":
        return f"Every {_step(minute)} minutes"
    if second == "0" and minute == "*" and hour == "*":
        return "Every minute"

    if not (
        _single_cron_number(second)
        and _single_cron_number(minute)
        and _single_cron_number(hour)
    ):
        return None

    at_time = f"{int(hour):02d}:{int(minute):02d}"
    if not _is_cron_wildcard(month):
        months = _parse_cron_field(month, 1, 12, _MONTH_NAME_VALUES)
        if not months or not _single_cron_number(day) or not _is_cron_wildcard(weekday):
            return None
        ordered = sorted(months)
        if len(ordered) == 1:
            label = _MONTH_LABELS[ordered[0] - 1]
            return f"{at_time} on {label} {int(day)} every year"
        gap = _cyclic_month_gap(ordered)
        if gap is None:
            labels = ", ".join(_MONTH_LABELS[value - 1] for value in ordered)
            return f"{at_time} on day {int(day)} of {labels}"
        return f"{at_time} on day {int(day)}, every {gap} months"
    if day == "*" and weekday == "*":
        return f"Every day at {at_time}"
    if day == "*" and _single_cron_number(weekday):
        return f"Every {_WEEKDAY_NAMES[int(weekday)]} at {at_time}"
    if _single_cron_number(day) and weekday == "*":
        return f"{at_time} on day {int(day)} of every month"
    return None


def _cron_weekday(dt: datetime) -> int:
    """Map datetime to cron weekday where Sunday=0 ... Saturday=6."""
    return (dt.weekday() + 1) % 7


def _normalize_schedule_datetime(
    value: Optional[datetime],
    reference: datetime,
) -> Optional[datetime]:
    """Align tz-awareness with reference so comparisons stay consistent."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def calculate_next_run(
    schedule: str,
    *,
    now: Optional[datetime] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Return the next future occurrence of a five- or six-field cron schedule.

    Six-field format: second minute hour day-of-month month day-of-week.
    jscstart / jscend only constrain the allowed window; they are not used as
    the next-run value themselves.
    """
    fields = (schedule or "").split()
    if len(fields) == 6:
        second_field, minute_field, hour_field, day_field, month_field, weekday_field = (
            fields
        )
    elif len(fields) == 5:
        second_field = "0"
        minute_field, hour_field, day_field, month_field, weekday_field = fields
    else:
        return None

    seconds = _parse_cron_field(second_field, 0, 59)
    minutes = _parse_cron_field(minute_field, 0, 59)
    hours = _parse_cron_field(hour_field, 0, 23)
    days = _parse_cron_field(day_field, 1, 31)
    months = _parse_cron_field(month_field, 1, 12, _MONTH_NAME_VALUES)
    weekdays = _parse_cron_field(weekday_field, 0, 6, _WEEKDAY_NAME_VALUES)
    if None in (seconds, minutes, hours, days, months, weekdays):
        return None

    assert seconds is not None
    assert minutes is not None
    assert hours is not None
    assert days is not None
    assert months is not None
    assert weekdays is not None

    reference = now if isinstance(now, datetime) else datetime.now()
    reference = reference.replace(microsecond=0)

    bound_start = _normalize_schedule_datetime(start_time, reference)
    bound_end = _normalize_schedule_datetime(end_time, reference)

    # Next run must be strictly after "now".
    candidate = reference + timedelta(seconds=1)
    if bound_start is not None and candidate < bound_start:
        candidate = bound_start

    # Search at most ~5 years ahead to avoid infinite loops on impossible crons.
    limit = candidate + timedelta(days=366 * 5)
    if bound_end is not None and bound_end < limit:
        limit = bound_end

    day_restricted = not _is_cron_wildcard(day_field)
    weekday_restricted = not _is_cron_wildcard(weekday_field)

    while candidate <= limit:
        if candidate.month not in months:
            year = candidate.year + (1 if candidate.month == 12 else 0)
            month = 1 if candidate.month == 12 else candidate.month + 1
            candidate = candidate.replace(
                year=year, month=month, day=1, hour=0, minute=0, second=0
            )
            continue

        day_ok = candidate.day in days
        weekday_ok = _cron_weekday(candidate) in weekdays
        # Converted pgAgent schedules never restrict both day and weekday at once.
        # When both are wildcards, any day matches. When only one is restricted,
        # require that restriction. If both somehow restricted, use cron OR.
        if day_restricted and weekday_restricted:
            date_ok = day_ok or weekday_ok
        elif day_restricted:
            date_ok = day_ok
        elif weekday_restricted:
            date_ok = weekday_ok
        else:
            date_ok = True

        if not date_ok:
            next_day = (candidate + timedelta(days=1)).replace(
                hour=0, minute=0, second=0
            )
            candidate = next_day
            continue

        if candidate.hour not in hours:
            advanced = False
            for hour in sorted(hours):
                if hour > candidate.hour:
                    candidate = candidate.replace(
                        hour=hour, minute=0, second=0
                    )
                    advanced = True
                    break
            if not advanced:
                candidate = (candidate + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0
                )
            continue

        if candidate.minute not in minutes:
            advanced = False
            for minute in sorted(minutes):
                if minute > candidate.minute:
                    candidate = candidate.replace(minute=minute, second=0)
                    advanced = True
                    break
            if not advanced:
                candidate = candidate + timedelta(hours=1)
                candidate = candidate.replace(minute=0, second=0)
            continue

        if candidate.second not in seconds:
            advanced = False
            for second in sorted(seconds):
                if second > candidate.second:
                    candidate = candidate.replace(second=second)
                    advanced = True
                    break
            if not advanced:
                candidate = candidate + timedelta(minutes=1)
                candidate = candidate.replace(second=0)
            continue

        # Guard against impossible day-of-month values (e.g. Feb 31).
        last_day = monthrange(candidate.year, candidate.month)[1]
        if candidate.day > last_day:
            candidate = candidate.replace(
                year=candidate.year + (1 if candidate.month == 12 else 0),
                month=1 if candidate.month == 12 else candidate.month + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
            )
            continue

        if bound_end is not None and candidate > bound_end:
            return None
        return candidate

    return None


def _unquote_sql_literal(token: str) -> Optional[str]:
    """Return the text of a single-quoted SQL literal, or None if unquoted."""
    text = (token or "").strip()
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    return None


def extract_db_config_from_function(
    function_definition: str,
) -> tuple[dict[str, Any], list[str]]:
    """
    Extract the session configuration a partition function applies to itself.

    Only statically known configuration statements are recognised:
    `SET name = value`, `SET name TO value`, and `set_config('name', 'value', ...)`.
    Comments and RAISE messages are removed first, values are parsed rather than
    evaluated, and a parameter whose value is only known at run time is reported
    as a warning instead of being guessed.

    Returns ({}, warnings) when the function configures nothing. No value is ever
    invented, so an empty result means the function genuinely sets nothing.
    """
    analysed = executable_sql_for_analysis(function_definition)
    warnings: list[str] = []
    found: list[tuple[int, str, str]] = []

    for match in _SET_STATEMENT_RE.finditer(analysed):
        name = match.group(1)
        raw_value = match.group(2)
        if name.lower() in _NON_CONFIG_SET_KEYWORDS:
            continue
        value = _unquote_sql_literal(raw_value)
        if value is None:
            if raw_value.lower() in _NON_CONFIG_SET_VALUES:
                continue
            value = raw_value
        found.append((match.start(), name.lower(), value))

    for match in _SET_CONFIG_CALL_RE.finditer(analysed):
        name = _unquote_sql_literal(match.group(1))
        if name is None:
            warnings.append(
                "A set_config() call builds its parameter name at run time, so it "
                "was not added to Database Configuration."
            )
            continue
        value = _unquote_sql_literal(match.group(2))
        if value is None:
            warnings.append(
                f"set_config('{name}', ...) uses a run-time value, so no value was "
                "assumed. Add it manually if this job needs it."
            )
            continue
        found.append((match.start(), name.lower(), value))

    # Textual order approximates execution order in the straight-line bodies these
    # partition functions use; the last assignment wins.
    found.sort(key=lambda item: item[0])
    config: dict[str, Any] = {}
    conflicting: set = set()
    for _position, name, value in found:
        if name in config and config[name] != value:
            conflicting.add(name)
        config[name] = value

    for name in sorted(conflicting):
        warnings.append(
            f"The function sets {name} more than once with different values. "
            f"The last statement in the body was used ('{config[name]}'). "
            "Review it before saving."
        )

    return sanitize_db_config_object(config), warnings


def build_db_config_json(config: Optional[dict[str, Any]] = None) -> str:
    """
    Serialise an extracted configuration object for the UI.

    Always valid JSON, and always exactly what was extracted — an empty
    configuration renders as {} rather than as invented defaults.
    """
    payload = sanitize_db_config_object(dict(config or {}))
    return json.dumps(payload, indent=2)


def sanitize_db_config_object(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop credential-like keys if a dict is ever merged into db config."""
    return {
        key: value
        for key, value in raw.items()
        if str(key).lower() not in _SECRET_DB_CONFIG_KEYS
    }