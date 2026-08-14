"""Partition Job Management — Streamlit UI for DBAs."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from typing import Any, Callable, Optional

import streamlit as st

from database import (
    DatabaseError,
    PgAgentNotInstalledError,
    create_partition_job,
    get_database_readiness,
    get_partition_job_logs,
    get_partition_jobs,
    get_pgagent_job_details,
    get_pgagent_jobs,
    run_partition_job_manual,
)
from job_autofill import calculate_next_run, describe_schedule, validate_six_field_cron
from validators import ValidationError, validate_form_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PAGE_TITLE = "Partition Job Management"
PAGE_SUBTITLE = (
    "Centralized management of parameterized PostgreSQL/EDB partition operations"
)

NAV_CONVERT = "Convert Existing Job"
NAV_CREATE = "Create New Job"
NAV_CONFIGURED = "Configured Jobs"
NAV_HISTORY = "Execution History"
NAV_OPTIONS = [NAV_CONVERT, NAV_CREATE, NAV_CONFIGURED, NAV_HISTORY]

# Never seed example settings here: Database Configuration is either extracted
# from the called partition routine or entered deliberately by the user.
EMPTY_DB_CONFIG = "{}"

GENERIC_DB_ERROR = (
    "The database operation failed. Check the server logs for details. "
    "This is not automatically a permission problem."
)

JOB_COLUMNS = [
    "job_id",
    "job_name",
    "enabled",
    "host_agent",
    "next_run",
    "last_run",
    "description",
]

JOB_COLUMN_LABELS = {
    "job_id": "Job ID",
    "job_name": "Job Name",
    "enabled": "Enabled",
    "host_agent": "Host Agent",
    "next_run": "Next Run",
    "last_run": "Last Run",
    "description": "Description",
}

FREQUENCY_UNITS = ["minute", "hour", "day", "week", "month", "year"]
PARTITION_UNITS = ["day", "week", "month", "year"]
INTERVAL_UNITS = ["day", "week", "month", "year"]
OPERATIONS = ["CREATE", "DROP"]
STATUS_FILTER_OPTIONS = ["All", "SUCCESS", "FAIL", "MANUAL_SUCCESS", "MANUAL_FAIL"]

CONVERT_PREFIX = "convert_"
NEW_PREFIX = "new_"

# Auto-fill key -> shared field suffix. The prefix selects which form is filled.
_AUTOFILL_TO_FIELD = {
    "job_name": "job_name",
    "is_enabled": "is_enabled",
    "table_schema": "table_schema",
    "table_name": "table_name",
    "db_config": "db_config",
    "job_schedule": "job_schedule",
    "frequency_amount": "frequency_amount",
    "frequency_unit": "frequency_unit",
    "partition_unit": "partition_unit",
    "partition_period": "partition_period",
    "create_drop_amount": "create_drop_amount",
    "create_drop_unit": "create_drop_unit",
}

_INTEGER_FIELDS = {"frequency_amount", "partition_period", "create_drop_amount"}


def _inject_css() -> None:
    """Compact corporate dark-theme polish on top of Streamlit's theme config."""
    st.markdown(
        """
<style>
    :root {
        --pj-bg: #0E1117;
        --pj-panel: #161B22;
        --pj-input: #1F2937;
        --pj-border: #30363D;
        --pj-text: #F0F3F6;
        --pj-muted: #9CA3AF;
        --pj-accent: #3B82F6;
        --pj-success-bg: #052E1C;
        --pj-success: #3FB950;
        --pj-warn-bg: #3D2E00;
        --pj-warn: #D29922;
        --pj-danger-bg: #3D1214;
        --pj-danger: #F85149;
        --pj-info-bg: #0D2140;
        --pj-info: #58A6FF;
    }
    .block-container {
        max-width: 1280px;
        padding-top: 1rem;
        padding-bottom: 2rem;
    }
    div[data-testid="stCaptionContainer"] {
        margin-top: -0.25rem;
        margin-bottom: 0.5rem;
        color: var(--pj-muted);
        font-size: 0.88rem;
    }
    h1 { font-size: 1.65rem !important; margin-bottom: 0.1rem !important; }
    h2, h3 { margin-top: 0.25rem !important; }
    .pj-subtitle {
        color: var(--pj-muted);
        font-size: 0.95rem;
        margin: 0 0 0.75rem 0;
    }
    .pj-header-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin: 0 0 0.85rem 0;
    }
    .pj-step-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin: 0.25rem 0 0.85rem 0;
    }
    .pj-step {
        background: var(--pj-input);
        color: var(--pj-muted);
        border: 1px solid var(--pj-border);
        border-radius: 999px;
        padding: 0.22rem 0.7rem;
        font-size: 0.8rem;
        font-weight: 600;
    }
    .pj-step-active {
        background: var(--pj-info-bg);
        color: var(--pj-info);
        border-color: #1F4B7A;
    }
    .pj-step-done {
        background: var(--pj-success-bg);
        color: var(--pj-success);
        border-color: #1A4D32;
    }
    .pj-badge {
        display: inline-block;
        border-radius: 999px;
        padding: 0.12rem 0.5rem;
        font-size: 0.75rem;
        font-weight: 700;
        margin-right: 0.2rem;
        border: 1px solid transparent;
    }
    .pj-badge-ok { background: var(--pj-success-bg); color: var(--pj-success); border-color: #1A4D32; }
    .pj-badge-warn { background: var(--pj-warn-bg); color: var(--pj-warn); border-color: #5C4510; }
    .pj-badge-fail { background: var(--pj-danger-bg); color: var(--pj-danger); border-color: #6E2226; }
    .pj-badge-info { background: var(--pj-info-bg); color: var(--pj-info); border-color: #1F4B7A; }
    .pj-badge-mute { background: var(--pj-input); color: var(--pj-muted); border-color: var(--pj-border); }
    .pj-badge-drop { background: var(--pj-danger-bg); color: var(--pj-danger); border-color: #6E2226; }
    .pj-panel, .pj-preview {
        border: 1px solid var(--pj-border);
        background: var(--pj-panel);
        border-radius: 8px;
        padding: 0.75rem 0.9rem;
        margin: 0.45rem 0 0.85rem 0;
    }
    .pj-preview { border-left: 3px solid var(--pj-accent); }
    .pj-panel-title {
        font-weight: 700;
        color: var(--pj-text);
        margin-bottom: 0.4rem;
    }
    .pj-kv {
        font-size: 0.9rem;
        line-height: 1.5;
        color: var(--pj-text);
    }
    .pj-kv code {
        background: var(--pj-input);
        border: 1px solid var(--pj-border);
        padding: 0.05rem 0.3rem;
        border-radius: 4px;
    }
    .pj-section-label {
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--pj-muted);
        margin: 0.7rem 0 0.3rem 0;
    }
    .pj-danger {
        border: 1px solid #6E2226;
        background: var(--pj-danger-bg);
        color: #FFB4B0;
        border-radius: 8px;
        padding: 0.55rem 0.75rem;
        margin: 0.4rem 0 0.7rem 0;
        font-weight: 600;
        font-size: 0.9rem;
    }
</style>
""",
        unsafe_allow_html=True,
    )


def _badge(text: str, kind: str = "mute") -> str:
    return f'<span class="pj-badge pj-badge-{kind}">{text}</span>'


def _status_badge_kind(status: Any) -> str:
    value = str(status or "").upper()
    if value in {"SUCCESS", "MANUAL_SUCCESS"}:
        return "ok"
    if value in {"FAIL", "MANUAL_FAIL", "ERROR"}:
        return "fail"
    if value in {"RUNNING", "PENDING"}:
        return "info"
    return "mute"


def _shared_form_defaults(
    *, db_config: str, schedule: str, job_name: str
) -> dict[str, Any]:
    now = datetime.now().replace(microsecond=0)
    return {
        "job_name": job_name,
        "is_enabled": True,
        "table_schema": "",
        "table_name": "",
        "db_config": db_config,
        "job_schedule": schedule,
        "frequency_amount": 1,
        "frequency_unit": "day",
        "auto_next_run": True,
        "next_run_date": now.date(),
        "next_run_time": now.time(),
        "partition_unit": "day",
        "partition_period": 1,
        "operation": "CREATE",
        "create_drop_amount": 1,
        "create_drop_unit": "month",
    }


def _init_form_state(prefix: str, defaults: dict[str, Any]) -> None:
    for suffix, value in defaults.items():
        key = prefix + suffix
        if key not in st.session_state:
            st.session_state[key] = value


def _init_session_state() -> None:
    simple_defaults: dict[str, Any] = {
        "jobs_loaded": False,
        "jobs": [],
        "jobs_error": None,
        "pgagent_missing": False,
        "load_warnings": [],
        "load_error": None,
        "load_info": None,
        "step_choices": [],
        "loaded_job_id": None,
        "load_job_id": 1,
        "create_in_flight": False,
        "last_created_fingerprint": None,
        "manual_run_feedback": None,
        "inference_summary": None,
        "history_job_id_filter": "",
        "history_status_filter": "All",
        "config_op_filter": "All",
        "config_enabled_filter": "All",
        "config_status_filter": "All",
        "config_search": "",
        "main_nav": NAV_CONVERT,
    }
    for key, value in simple_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    for state_key in (
        "partition_jobs",
        "partition_job_logs",
        "database_readiness",
    ):
        for suffix, default in (("", None), ("_error", None), ("_loaded", False)):
            if state_key + suffix not in st.session_state:
                st.session_state[state_key + suffix] = default

    _init_form_state(
        CONVERT_PREFIX,
        _shared_form_defaults(
            db_config=EMPTY_DB_CONFIG,
            schedule="0 0 2 * * *",
            job_name="",
        ),
    )
    _init_form_state(
        NEW_PREFIX,
        _shared_form_defaults(
            db_config=EMPTY_DB_CONFIG,
            schedule="0 0 2 * * *",
            job_name="",
        ),
    )


def _load_into_state(
    state_key: str,
    loader: Callable[..., Any],
    *args: Any,
    spinner_text: Optional[str] = None,
) -> None:
    """Run a read-only loader once and keep result/error in session state."""
    try:
        if spinner_text:
            with st.spinner(spinner_text):
                st.session_state[state_key] = loader(*args)
        else:
            st.session_state[state_key] = loader(*args)
        st.session_state[state_key + "_error"] = None
    except DatabaseError as exc:
        st.session_state[state_key] = None
        st.session_state[state_key + "_error"] = exc.message
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while loading %s", state_key)
        st.session_state[state_key] = None
        st.session_state[state_key + "_error"] = GENERIC_DB_ERROR
    finally:
        st.session_state[state_key + "_loaded"] = True


def _ensure_loaded(
    state_key: str,
    loader: Callable[..., Any],
    *args: Any,
    spinner_text: str,
) -> None:
    """Load operational data on first use of a view (not at app startup)."""
    if not st.session_state.get(state_key + "_loaded"):
        _load_into_state(state_key, loader, *args, spinner_text=spinner_text)


def _load_jobs() -> None:
    """Fetch pgAgent jobs and store them in session state."""
    try:
        with st.spinner("Loading pgAgent jobs..."):
            st.session_state.jobs = get_pgagent_jobs()
        st.session_state.jobs_error = None
        st.session_state.pgagent_missing = False
    except PgAgentNotInstalledError as exc:
        st.session_state.jobs = []
        st.session_state.jobs_error = exc.message
        st.session_state.pgagent_missing = True
    except DatabaseError as exc:
        st.session_state.jobs = []
        st.session_state.jobs_error = exc.message
        st.session_state.pgagent_missing = False
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while loading pgAgent jobs")
        st.session_state.jobs = []
        st.session_state.jobs_error = GENERIC_DB_ERROR
        st.session_state.pgagent_missing = False
    finally:
        st.session_state.jobs_loaded = True


def _combine_datetime(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def submission_fingerprint(validated: dict[str, Any]) -> str:
    """Stable signature of a validated payload, used to ignore duplicate submits."""
    return json.dumps(
        {key: str(value) for key, value in sorted(validated.items())},
        sort_keys=True,
    )


def _format_error_for_ui(message: str) -> str:
    """Keep errors actionable without inventing permission claims."""
    text = (message or "").strip()
    if not text:
        return GENERIC_DB_ERROR
    lower = text.lower()
    tips: list[str] = []
    if "sqlstate 42501" in lower or "does not have the required permission" in lower:
        tips.append(
            "Next step: ask a DBA to grant only the missing privilege shown above. "
            "This application never grants privileges itself."
        )
    elif "sqlstate 42883" in lower or "was not found" in lower:
        tips.append(
            "Next step: confirm the function name/signature in the database matches "
            "what the application calls."
        )
    elif "sqlstate 23505" in lower or "already exist" in lower:
        tips.append(
            "Next step: search Configured Jobs for an existing row with the same key."
        )
    elif "unable to connect" in lower or "authentication failed" in lower:
        tips.append(
            "Next step: verify host, port, database name, and credentials in the "
            "application environment — never paste passwords into the UI."
        )
    if tips:
        return text + "\n\n" + " ".join(tips)
    return text


def _render_db_error(message: str) -> None:
    st.error(_format_error_for_ui(message))


def _shorten(text: Any, limit: int = 120) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _normalize_db_config_text(raw: Any) -> str:
    text = ("" if raw is None else str(raw)).strip() or EMPTY_DB_CONFIG
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return json.dumps(parsed, indent=2, sort_keys=True)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return text


def _db_config_is_empty(raw: Any) -> bool:
    try:
        parsed = json.loads(str(raw or "").strip() or "{}")
        return isinstance(parsed, dict) and not parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


# ---------------------------------------------------------------------------
# Shared configuration form
# ---------------------------------------------------------------------------


def _render_cron_helper(job_schedule: str) -> tuple[Optional[datetime], Optional[str]]:
    """Show field hint, meaning, next run, or the exact invalid field."""
    st.caption("Fields: `second minute hour day-of-month month day-of-week`")
    cron_error = validate_six_field_cron(job_schedule or "")
    if cron_error:
        st.error(cron_error)
        return None, cron_error

    calculated = calculate_next_run(job_schedule)
    meaning = describe_schedule(job_schedule)
    bits: list[str] = []
    if meaning:
        bits.append(f"**Meaning:** {meaning}")
    if calculated:
        bits.append(f"**Next run:** `{calculated:%Y-%m-%d %H:%M:%S}`")
    else:
        bits.append(
            "The next occurrence could not be calculated from this schedule. "
            "Set the next run manually."
        )
    st.markdown("  \n".join(bits))
    return calculated, None


def _render_db_config_editor(prefix: str, *, from_inference: bool) -> str:
    db_config = st.text_area(
        "Database configuration (JSON object)",
        key=prefix + "db_config",
        height=90,
        help="Session/database parameters applied when this job executes. Stored as-is.",
    )
    if _db_config_is_empty(db_config):
        note = "No routine-level DB configuration was detected." if from_inference else (
            "Empty configuration (`{}`). Add session settings only when needed."
        )
        st.caption(note)
    elif from_inference:
        st.caption(
            "Extracted from SET / set_config statements in the routine definition."
        )
    else:
        try:
            json.loads(str(db_config))
            st.caption("JSON looks valid.")
        except (TypeError, ValueError, json.JSONDecodeError):
            st.caption("JSON is not valid yet — fix it before submitting.")
    return db_config


def _render_configuration_preview(raw: dict[str, Any]) -> None:
    """Compact pre-submit review so the DBA need not scroll back up."""
    is_create = bool(raw.get("is_create"))
    operation = "CREATE" if is_create else "DROP"
    interval_label = "Create ahead interval" if is_create else "Retention interval"
    schema = raw.get("table_schema") or "—"
    table = raw.get("table_name") or "—"
    freq = f"{raw.get('frequency_amount')} {raw.get('frequency_unit')}"
    part = f"{raw.get('partition_period')} {raw.get('partition_unit')}"
    interval = f"{raw.get('create_drop_amount')} {raw.get('create_drop_unit')}"
    next_run = raw.get("next_run_time")
    next_run_text = (
        next_run.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(next_run, datetime)
        else str(next_run or "—")
    )
    db_config = _normalize_db_config_text(raw.get("db_config"))

    st.markdown('<div class="pj-preview">', unsafe_allow_html=True)
    st.markdown("##### Configuration preview")
    st.caption(
        "This is what will be sent to "
        "`mubasher_oms.insert_data_to_partition_job_table(...)`. "
        "No pgAgent job is created."
    )
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            f"**Job name:** `{raw.get('job_name') or '—'}`  \n"
            f"**Operation:** `{operation}`  \n"
            f"**Target table:** `{schema}.{table}`  \n"
            f"**Enabled:** `{bool(raw.get('is_enabled'))}`  \n"
            f"**Schedule:** `{raw.get('job_schedule') or '—'}`"
        )
    with col2:
        st.markdown(
            f"**Next run:** `{next_run_text}`  \n"
            f"**Job frequency:** `{freq}`  \n"
            f"**Partition size:** `{part}`  \n"
            f"**{interval_label}:** `{interval}`"
        )
    st.markdown("**db_config_para:**")
    st.code(db_config, language="json")
    st.markdown("</div>", unsafe_allow_html=True)


def _render_job_fields(prefix: str) -> tuple[dict[str, Any], Optional[str]]:
    """
    Render the shared configuration fields.

    Returns (raw values for validate_form_data, blocking_error_or_None).
    Both tabs use this so the two paths always produce the same structure.
    """
    from_inference = (
        prefix == CONVERT_PREFIX and st.session_state.inference_summary is not None
    )

    st.markdown('<div class="pj-section-label">Basic information</div>', unsafe_allow_html=True)
    operation = st.radio(
        "Operation",
        options=OPERATIONS,
        key=prefix + "operation",
        horizontal=True,
        help=(
            "CREATE adds future partitions. DROP removes partitions older than "
            "the retention interval."
        ),
    )
    is_create = operation == "CREATE"
    if not is_create:
        st.markdown(
            '<div class="pj-danger">DROP operation: review the retention '
            "interval carefully. Wrong values can remove production data.</div>",
            unsafe_allow_html=True,
        )

    col_a, col_b = st.columns(2)
    with col_a:
        job_name = st.text_input("Job name", key=prefix + "job_name")
        table_schema = st.text_input("Table schema", key=prefix + "table_schema")
    with col_b:
        is_enabled = st.checkbox("Enabled", key=prefix + "is_enabled")
        table_name = st.text_input("Table name", key=prefix + "table_name")

    st.markdown('<div class="pj-section-label">Schedule</div>', unsafe_allow_html=True)
    job_schedule = st.text_input(
        "Job schedule (six-field cron)",
        key=prefix + "job_schedule",
    )
    calculated_next_run, blocking_error = _render_cron_helper(job_schedule or "")

    st.markdown(
        "**Job frequency** — how often this configuration is scheduled"
    )
    freq_col1, freq_col2 = st.columns(2)
    with freq_col1:
        frequency_amount = st.number_input(
            "Frequency amount",
            min_value=1,
            step=1,
            key=prefix + "frequency_amount",
        )
    with freq_col2:
        frequency_unit = st.selectbox(
            "Frequency unit",
            options=FREQUENCY_UNITS,
            key=prefix + "frequency_unit",
        )

    auto_next_run = st.checkbox(
        "Use the next run calculated from the schedule",
        key=prefix + "auto_next_run",
    )
    next_run_time: Optional[datetime]
    if auto_next_run and calculated_next_run is not None:
        next_run_time = calculated_next_run
        st.caption(
            f"Next run time will be stored as `{calculated_next_run:%Y-%m-%d %H:%M:%S}`."
        )
    else:
        if auto_next_run:
            st.caption("Automatic calculation unavailable — using the manual value.")
        else:
            st.caption("Manual next run selected — it overrides the schedule preview.")
        date_col, time_col = st.columns(2)
        with date_col:
            next_run_date = st.date_input("Next run date", key=prefix + "next_run_date")
        with time_col:
            next_run_value = st.time_input("Next run time", key=prefix + "next_run_time")
        next_run_time = _combine_datetime(next_run_date, next_run_value)

    st.markdown('<div class="pj-section-label">Partition configuration</div>', unsafe_allow_html=True)
    st.markdown("**Partition size** — how much data each partition covers")
    part_col1, part_col2 = st.columns(2)
    with part_col1:
        partition_unit = st.selectbox(
            "Partition unit",
            options=PARTITION_UNITS,
            key=prefix + "partition_unit",
        )
    with part_col2:
        partition_period = st.number_input(
            "Partition period",
            min_value=1,
            step=1,
            key=prefix + "partition_period",
        )

    if is_create:
        interval_heading = (
            "**Create ahead interval** — how far into the future partitions "
            "should exist"
        )
        amount_label = "Create ahead amount"
        unit_label = "Create ahead unit"
        interval_help = (
            "How far beyond the current maximum partition boundary should future "
            "partitions be created?"
        )
    else:
        interval_heading = (
            "**Retention interval** — partitions older than this may be dropped"
        )
        amount_label = "Retention amount"
        unit_label = "Retention unit"
        interval_help = "Partitions older than this interval may be dropped."

    st.markdown(interval_heading)
    cd_col1, cd_col2 = st.columns(2)
    with cd_col1:
        create_drop_amount = st.number_input(
            amount_label,
            min_value=1,
            step=1,
            key=prefix + "create_drop_amount",
            help=interval_help,
        )
    with cd_col2:
        create_drop_unit = st.selectbox(
            unit_label,
            options=INTERVAL_UNITS,
            key=prefix + "create_drop_unit",
        )

    st.markdown('<div class="pj-section-label">Database configuration</div>', unsafe_allow_html=True)
    db_config = _render_db_config_editor(prefix, from_inference=from_inference)

    raw = {
        "job_name": job_name,
        "is_enabled": is_enabled,
        "table_schema": table_schema,
        "table_name": table_name,
        "db_config": db_config,
        "job_schedule": job_schedule,
        "frequency_amount": frequency_amount,
        "frequency_unit": frequency_unit,
        "next_run_time": next_run_time,
        "partition_unit": partition_unit,
        "partition_period": partition_period,
        "is_create": is_create,
        "create_drop_amount": create_drop_amount,
        "create_drop_unit": create_drop_unit,
    }
    return raw, blocking_error


def submit_partition_configuration(raw: dict[str, Any]) -> list[tuple[str, str]]:
    """
    The single creation pathway shared by both tabs.

    Validates, then calls the approved database function through the existing
    parameterised layer. Never inserts into the configuration table directly and
    never creates a pgAgent job. Returns (kind, message) feedback items.
    """
    try:
        validated = validate_form_data(raw)
    except ValidationError as exc:
        return [("error", exc.message)]

    fingerprint = submission_fingerprint(validated)
    if fingerprint == st.session_state.last_created_fingerprint:
        return [
            (
                "info",
                "This exact configuration was already created in this session. "
                "Change a field before submitting again.",
            )
        ]

    try:
        with st.spinner("Creating partition configuration..."):
            result = create_partition_job(validated)
    except DatabaseError as exc:
        return [("error", _format_error_for_ui(exc.message))]
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while creating partition job")
        return [("error", GENERIC_DB_ERROR)]

    st.session_state.last_created_fingerprint = fingerprint

    feedback: list[tuple[str, str]] = [
        (
            "success",
            "Partition configuration created successfully via "
            "mubasher_oms.insert_data_to_partition_job_table().",
        ),
        (
            "info",
            "No pgAgent job was created. The generic scanners "
            "(run_partition_create_jobs / run_partition_drop_jobs) pick this "
            "configuration up when its next run time is due.",
        ),
    ]
    if result is not None:
        feedback.append(("info", f"Function result: `{result}`"))

    _load_into_state("partition_jobs", get_partition_jobs)
    return feedback


def _render_feedback(items: list[tuple[str, str]]) -> None:
    for kind, text in items:
        if kind == "success":
            st.success(text)
        elif kind == "error":
            st.error(text)
        else:
            st.info(text)


def _render_submit_button(
    prefix: str, raw: dict[str, Any], blocking_error: Optional[str], label: str
) -> None:
    _render_configuration_preview(raw)
    write_note = (
        "This button **writes** one configuration row through the approved "
        "database function. Loading and refresh are read-only."
    )
    if not raw.get("is_create"):
        write_note += " Operation is **DROP** — double-check retention before saving."
    st.caption(write_note)

    if st.button(
        label,
        type="primary",
        key=prefix + "submit",
        disabled=blocking_error is not None,
    ):
        if st.session_state.create_in_flight:
            return
        st.session_state.create_in_flight = True
        try:
            _render_feedback(submit_partition_configuration(raw))
        finally:
            st.session_state.create_in_flight = False
    elif blocking_error:
        st.caption("Fix the schedule above to enable submission.")


# ---------------------------------------------------------------------------
# Tab 1 — Convert Existing Job
# ---------------------------------------------------------------------------


def _build_inference_summary(details: dict[str, Any]) -> dict[str, Any]:
    autofill = details.get("autofill") or {}
    routine = details.get("called_routine") or {}
    warnings = list(details.get("warnings") or [])
    db_config = autofill.get("db_config", EMPTY_DB_CONFIG)
    db_empty = _db_config_is_empty(db_config)

    if "is_create" in autofill:
        operation = "CREATE" if autofill["is_create"] else "DROP"
    else:
        operation = "Unresolved — review manually"

    schema = autofill.get("table_schema")
    table = autofill.get("table_name")
    target = f"{schema}.{table}" if schema and table else "Unresolved"

    invocation = routine.get("invocation")
    kind = routine.get("kind")
    if invocation and kind:
        invocation_label = f"{invocation} {kind.lower()}"
    elif invocation:
        invocation_label = str(invocation)
    else:
        invocation_label = "Not resolved"

    confidence = "High"
    if warnings:
        confidence = "Needs review"
    if not routine.get("signature") or "is_create" not in autofill or target == "Unresolved":
        confidence = "Low — manual review required"

    return {
        "job_id": details.get("job_id"),
        "job_name": details.get("job_name"),
        "routine_signature": routine.get("signature") or "Not resolved",
        "invocation_label": invocation_label,
        "operation": operation,
        "target_table": target,
        "schedule": autofill.get("job_schedule") or "Not inferred",
        "next_run": autofill.get("next_run_time"),
        "db_config_extracted": not db_empty,
        "db_config": db_config,
        "confidence": confidence,
        "warnings": warnings,
        "database_name": details.get("database_name"),
    }


def _render_inference_summary(summary: dict[str, Any]) -> None:
    next_run = summary.get("next_run")
    if isinstance(next_run, datetime):
        next_run_text = next_run.strftime("%Y-%m-%d %H:%M:%S")
    else:
        next_run_text = str(next_run or "Not inferred")

    op = str(summary.get("operation") or "")
    op_kind = "ok" if op == "CREATE" else ("drop" if op == "DROP" else "warn")
    conf = str(summary.get("confidence") or "")
    conf_kind = "ok" if conf.startswith("High") else ("warn" if "review" in conf.lower() else "fail")
    db_kind = "ok" if summary.get("db_config_extracted") else "mute"

    st.markdown('<div class="pj-panel">', unsafe_allow_html=True)
    st.markdown(
        '<div class="pj-panel-title">Inference summary '
        "(read-only analysis)</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f'{_badge(op, op_kind)}{_badge(conf, conf_kind)}'
        f'{_badge("DB config: yes" if summary.get("db_config_extracted") else "DB config: no", db_kind)}',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="pj-kv">'
        f"<b>pgAgent job:</b> {summary.get('job_id')} — "
        f"<code>{summary.get('job_name') or '—'}</code><br>"
        f"<b>Routine resolved:</b> <code>{summary.get('routine_signature')}</code><br>"
        f"<b>Invocation type:</b> {summary.get('invocation_label')}<br>"
        f"<b>Target table:</b> <code>{summary.get('target_table')}</code><br>"
        f"<b>Schedule:</b> <code>{summary.get('schedule')}</code><br>"
        f"<b>Next run:</b> <code>{next_run_text}</code><br>"
        f"<b>Source database (job step):</b> "
        f"<code>{summary.get('database_name') or '—'}</code>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if summary.get("db_config_extracted"):
        st.caption("Extracted from SET / set_config statements in the routine definition.")
        st.code(
            _normalize_db_config_text(summary.get("db_config")),
            language="json",
        )
    else:
        st.caption("No routine-level DB configuration was detected. Field shows `{}`.")
    st.markdown("</div>", unsafe_allow_html=True)

    for warning in summary.get("warnings") or []:
        st.warning(warning)


def _apply_autofill(details: dict[str, Any]) -> None:
    """Copy safe auto-fill values into the convert-tab widgets. Read-only step."""
    autofill = details.get("autofill") or {}
    for source_key, suffix in _AUTOFILL_TO_FIELD.items():
        if source_key not in autofill:
            continue
        value = autofill[source_key]
        if source_key in _INTEGER_FIELDS:
            value = int(value)
        if source_key == "db_config":
            value = _normalize_db_config_text(value)
        st.session_state[CONVERT_PREFIX + suffix] = value

    if "is_create" in autofill:
        st.session_state[CONVERT_PREFIX + "operation"] = (
            "CREATE" if autofill["is_create"] else "DROP"
        )

    next_run = autofill.get("next_run_time")
    if isinstance(next_run, datetime):
        st.session_state[CONVERT_PREFIX + "next_run_date"] = next_run.date()
        st.session_state[CONVERT_PREFIX + "next_run_time"] = next_run.time().replace(
            microsecond=0
        )
    elif isinstance(next_run, date):
        st.session_state[CONVERT_PREFIX + "next_run_date"] = next_run

    summary = _build_inference_summary(details)
    st.session_state.inference_summary = summary
    st.session_state.load_warnings = list(summary.get("warnings") or [])
    st.session_state.step_choices = list(details.get("step_choices") or [])
    st.session_state.loaded_job_id = details.get("job_id")
    st.session_state.load_error = None
    job_label = details.get("job_name") or details.get("job_id")
    st.session_state.load_info = (
        f"Loaded pgAgent job {details.get('job_id')}: {job_label}. "
        "Nothing has been written yet. Review the inference summary, edit if "
        "needed, then create the configuration."
    )


def _load_job_details(job_id: int, step_id: Any = None) -> None:
    try:
        with st.spinner("Loading pgAgent job and reading routine definition..."):
            details = get_pgagent_job_details(job_id, step_id=step_id)
    except (PgAgentNotInstalledError, DatabaseError) as exc:
        st.session_state.load_error = _format_error_for_ui(exc.message)
        st.session_state.load_warnings = []
        st.session_state.load_info = None
        st.session_state.step_choices = []
        st.session_state.inference_summary = None
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while loading pgAgent job details")
        st.session_state.load_error = GENERIC_DB_ERROR
        st.session_state.load_warnings = []
        st.session_state.load_info = None
        st.session_state.step_choices = []
        st.session_state.inference_summary = None
        return
    _apply_autofill(details)


def _render_step_bar(active: int) -> None:
    labels = [
        "1. Enter Job ID",
        "2. Load details",
        "3. Review inference",
        "4. Create configuration",
    ]
    chips = []
    for index, label in enumerate(labels, start=1):
        css = "pj-step"
        if index < active:
            css += " pj-step-done"
        elif index == active:
            css += " pj-step-active"
        chips.append(f'<span class="{css}">{label}</span>')
    st.markdown(
        f'<div class="pj-step-bar">{"".join(chips)}</div>',
        unsafe_allow_html=True,
    )


def _render_pgagent_jobs() -> None:
    col_info, col_btn = st.columns([3, 1])
    with col_info:
        if (
            st.session_state.jobs_loaded
            and not st.session_state.jobs_error
            and not st.session_state.pgagent_missing
        ):
            st.markdown(f"**Total pgAgent jobs:** {len(st.session_state.jobs)}")
        elif not st.session_state.jobs_loaded:
            st.caption("Optional browse list — click Refresh to load (read-only).")
    with col_btn:
        if st.button("Refresh Jobs", width="stretch", help="Read-only refresh"):
            _load_jobs()

    if not st.session_state.jobs_loaded:
        return
    if st.session_state.pgagent_missing:
        st.warning(st.session_state.jobs_error)
        return
    if st.session_state.jobs_error:
        _render_db_error(st.session_state.jobs_error)
        return
    if not st.session_state.jobs:
        st.info("No pgAgent jobs were found.")
        return

    st.dataframe(
        [
            {JOB_COLUMN_LABELS[col]: job.get(col) for col in JOB_COLUMNS}
            for job in st.session_state.jobs
        ],
        width="stretch",
        hide_index=True,
    )


def _render_convert_tab() -> None:
    st.subheader("Convert an existing pgAgent partition job")
    st.caption(
        "Guided migration: read an old table-specific pgAgent job (read-only) and "
        "store one parameterised configuration row. The original pgAgent job is "
        "never modified."
    )

    if st.session_state.inference_summary is not None:
        active_step = 3
    elif st.session_state.load_error:
        active_step = 2
    else:
        active_step = 1
    _render_step_bar(active_step)

    with st.expander("Browse existing pgAgent jobs (optional)", expanded=False):
        _render_pgagent_jobs()

    st.markdown("##### Step 1 — Enter pgAgent Job ID")
    col_id, col_btn = st.columns([1.4, 1])
    with col_id:
        st.number_input("pgAgent Job ID", min_value=1, step=1, key="load_job_id")
    with col_btn:
        st.write("")
        if st.button(
            "Load Job Details",
            width="stretch",
            help="Read-only: inspects pgAgent and catalog metadata only",
        ):
            _load_job_details(int(st.session_state.load_job_id))

    if st.session_state.step_choices:
        st.markdown("##### Multiple job steps found")
        st.caption("Select the step that calls the partition routine, then apply it.")
        choice_labels = {}
        for choice in st.session_state.step_choices:
            routine_label = choice.get("routine_label") or "routine not identified"
            if choice.get("invocation"):
                routine_label = f"{choice['invocation']} {routine_label}"
            step_name = choice.get("step_name") or f"Step {choice.get('step_id')}"
            choice_labels[choice["step_id"]] = f"{step_name} ({routine_label})"
        st.selectbox(
            "Job step",
            options=list(choice_labels.keys()),
            format_func=lambda step_id: choice_labels.get(step_id, str(step_id)),
            key="selected_step_id",
        )
        if st.button("Apply Selected Step"):
            job_id = st.session_state.loaded_job_id or st.session_state.load_job_id
            _load_job_details(int(job_id), step_id=st.session_state.selected_step_id)

    if st.session_state.load_error:
        _render_db_error(st.session_state.load_error)
    if st.session_state.load_info:
        st.info(st.session_state.load_info)

    if st.session_state.inference_summary:
        st.markdown("##### Step 2–3 — Review inferred values")
        _render_inference_summary(st.session_state.inference_summary)
        st.markdown("##### Step 4 — Edit if needed, then create configuration")
        st.caption(
            "Fields below are editable. Values came from pgAgent / routine analysis "
            "where possible; leave unresolved fields blank only if you intend to "
            "fix them yourself."
        )
        raw, blocking_error = _render_job_fields(CONVERT_PREFIX)
        st.divider()
        _render_submit_button(
            CONVERT_PREFIX, raw, blocking_error, "Create Partition Job"
        )
        st.caption(
            "The original pgAgent job is never modified, disabled, or deleted. "
            "Retiring it is a separate DBA decision."
        )
    else:
        st.info(
            "Enter a pgAgent Job ID and click **Load Job Details** to begin. "
            "Loading is read-only and does not write to the database."
        )


# ---------------------------------------------------------------------------
# Tab 2 — Create New Job
# ---------------------------------------------------------------------------


def _render_new_job_tab() -> None:
    st.subheader("Create a new parameterised partition job")
    st.caption(
        "No pgAgent job is needed. This stores one configuration row that the "
        "generic scanners execute when it becomes due."
    )
    raw, blocking_error = _render_job_fields(NEW_PREFIX)
    st.divider()
    _render_submit_button(NEW_PREFIX, raw, blocking_error, "Create Partition Job")


# ---------------------------------------------------------------------------
# Tab 3 — Configured Jobs
# ---------------------------------------------------------------------------


def _operation_label(job: dict[str, Any]) -> str:
    return "CREATE" if bool(job.get("is_create")) else "DROP"


def _target_table(job: dict[str, Any]) -> str:
    schema = job.get("table_schema") or ""
    table = job.get("table_name") or ""
    if schema and table:
        return f"{schema}.{table}"
    return table or schema or "—"


def _format_config_details(job: dict[str, Any]) -> None:
    is_create = bool(job.get("is_create"))
    operation = "CREATE" if is_create else "DROP"
    interval_label = "Create ahead interval" if is_create else "Retention interval"
    status = job.get("last_run_status") or "—"
    status_kind = _status_badge_kind(status)

    st.markdown(
        f'{_badge(operation, "ok" if is_create else "drop")}'
        f'{_badge("Enabled" if job.get("is_enabled") else "Disabled", "info" if job.get("is_enabled") else "mute")}'
        f'{_badge(str(status), status_kind)}',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="pj-section-label">Identity</div>', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Job ID:** `{job.get('job_id')}`")
        st.markdown(f"**Job name:** `{job.get('job_name')}`")
        st.markdown(f"**Target table:** `{_target_table(job)}`")
    with col2:
        st.markdown(f"**Operation:** `{operation}`")
        st.markdown(f"**Enabled:** `{bool(job.get('is_enabled'))}`")
        st.markdown(f"**Last status:** `{status}`")

    st.markdown('<div class="pj-section-label">Schedule</div>', unsafe_allow_html=True)
    col3, col4 = st.columns(2)
    with col3:
        st.markdown(f"**Schedule:** `{job.get('job_schedule')}`")
        st.markdown(f"**Job frequency:** `{job.get('frequency')}`")
    with col4:
        st.markdown(f"**Next run:** `{job.get('next_run_time')}`")
        st.markdown(f"**Last run:** `{job.get('last_run_time')}`")
    meaning = describe_schedule(str(job.get("job_schedule") or ""))
    if meaning:
        st.caption(f"Schedule meaning: {meaning}")

    st.markdown('<div class="pj-section-label">Partition rules</div>', unsafe_allow_html=True)
    st.markdown(
        f"**Partition size:** `{job.get('partition_period')} {job.get('partition_unit')}`  \n"
        f"**{interval_label}:** `{job.get('create_drop_interval')}`"
    )

    st.markdown('<div class="pj-section-label">Database configuration</div>', unsafe_allow_html=True)
    st.code(json.dumps(job.get("db_config_para"), indent=2, default=str), language="json")


def _manual_run_allowed() -> tuple[bool, str]:
    readiness = st.session_state.database_readiness
    if st.session_state.database_readiness_error:
        return False, (
            "Manual run is blocked until Database Readiness can be checked. "
            "Fix the readiness error first."
        )
    if not readiness:
        return False, "Manual run is blocked: readiness information is unavailable."
    if not readiness.get("config_table_select"):
        return False, (
            "Manual run is blocked: configuration table SELECT is missing. "
            "Ask a DBA to grant only what is needed — this UI never grants privileges."
        )
    return True, ""


def _render_manual_run(job: dict[str, Any]) -> None:
    job_id = job.get("job_id")
    is_create = bool(job.get("is_create"))
    allowed, reason = _manual_run_allowed()

    st.markdown("**Manual run**")
    st.caption(
        "Executes this configured job immediately through "
        "run_partition_job_manual(). The automatic next run time is not changed. "
        "This is a write operation."
    )
    if not allowed:
        st.warning(reason)
        return

    if is_create:
        if st.button("Run CREATE Job Now", key=f"manual_run_{job_id}"):
            _execute_manual_run(job_id)
    else:
        st.markdown(
            '<div class="pj-danger">DROP operation: this may permanently remove '
            "old table partitions and their data.</div>",
            unsafe_allow_html=True,
        )
        confirmed = st.checkbox(
            "I understand that this DROP may permanently remove partition data.",
            key=f"manual_confirm_{job_id}",
        )
        if st.button(
            "Run DROP Job Now",
            key=f"manual_run_{job_id}",
            disabled=not confirmed,
        ):
            _execute_manual_run(job_id)

    if st.session_state.manual_run_feedback:
        _render_feedback(st.session_state.manual_run_feedback)
        st.session_state.manual_run_feedback = None


def _execute_manual_run(job_id: Any) -> None:
    try:
        result = run_partition_job_manual(job_id)
    except DatabaseError as exc:
        st.session_state.manual_run_feedback = [
            ("error", _format_error_for_ui(exc.message))
        ]
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error during manual partition job run")
        st.session_state.manual_run_feedback = [("error", GENERIC_DB_ERROR)]
        return

    feedback = [("success", f"Manual run of job {job_id} completed and committed.")]
    if result is not None:
        feedback.append(("info", f"Function result: `{result}`"))
    st.session_state.manual_run_feedback = feedback
    _load_into_state("partition_job_logs", get_partition_job_logs, 100)
    _load_into_state("partition_jobs", get_partition_jobs)


def _filter_configured_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    op_filter = st.session_state.config_op_filter
    enabled_filter = st.session_state.config_enabled_filter
    status_filter = st.session_state.config_status_filter
    search = (st.session_state.config_search or "").strip().lower()

    filtered: list[dict[str, Any]] = []
    for job in jobs:
        operation = _operation_label(job)
        if op_filter != "All" and operation != op_filter:
            continue
        if enabled_filter == "Enabled" and not job.get("is_enabled"):
            continue
        if enabled_filter == "Disabled" and job.get("is_enabled"):
            continue
        status = str(job.get("last_run_status") or "")
        if status_filter != "All" and status != status_filter:
            continue
        if search:
            haystack = " ".join(
                str(part or "")
                for part in (
                    job.get("job_id"),
                    job.get("job_name"),
                    job.get("table_schema"),
                    job.get("table_name"),
                    _target_table(job),
                )
            ).lower()
            if search not in haystack:
                continue
        filtered.append(job)
    return filtered


def _render_configured_jobs_tab() -> None:
    st.subheader("Configured partition jobs")
    st.caption(
        "Every row is one parameterised partition job in "
        "`mubasher_oms.partitioning_job_table`. These rows replaced the old "
        "per-table pgAgent jobs."
    )

    _ensure_loaded(
        "partition_jobs",
        get_partition_jobs,
        spinner_text="Loading configured jobs...",
    )

    if st.button("Refresh Configured Jobs", help="Read-only refresh"):
        _load_into_state(
            "partition_jobs",
            get_partition_jobs,
            spinner_text="Refreshing configured jobs...",
        )

    if st.session_state.partition_jobs_error:
        _render_db_error(st.session_state.partition_jobs_error)
        st.caption(
            "SELECT permission on the configuration table is required to list "
            "configured jobs. Ask a DBA to grant only what is needed."
        )
        return

    jobs = st.session_state.partition_jobs or []
    if not jobs:
        st.info("No parameterised partition jobs are configured yet.")
        return

    enabled_count = sum(1 for job in jobs if job.get("is_enabled"))
    create_count = sum(1 for job in jobs if job.get("is_create"))
    drop_count = len(jobs) - create_count
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total", len(jobs))
    m2.metric("Enabled", enabled_count)
    m3.metric("CREATE", create_count)
    m4.metric("DROP", drop_count)

    f1, f2, f3, f4 = st.columns([1, 1, 1, 2])
    with f1:
        st.selectbox(
            "Operation",
            options=["All", "CREATE", "DROP"],
            key="config_op_filter",
        )
    with f2:
        st.selectbox(
            "Enabled",
            options=["All", "Enabled", "Disabled"],
            key="config_enabled_filter",
        )
    with f3:
        status_options = ["All"] + sorted(
            {
                str(job.get("last_run_status"))
                for job in jobs
                if job.get("last_run_status")
            }
        )
        if st.session_state.config_status_filter not in status_options:
            st.session_state.config_status_filter = "All"
        st.selectbox("Status", options=status_options, key="config_status_filter")
    with f4:
        st.text_input("Search job / table", key="config_search")

    filtered = _filter_configured_jobs(jobs)
    st.markdown(
        f"**Showing {len(filtered)} of {len(jobs)} configured job(s).**"
    )

    table_rows = []
    for job in filtered:
        table_rows.append(
            {
                "job_id": job.get("job_id"),
                "job_name": job.get("job_name"),
                "enabled": bool(job.get("is_enabled")),
                "operation": _operation_label(job),
                "schema.table": _target_table(job),
                "schedule": job.get("job_schedule"),
                "next_run_time": job.get("next_run_time"),
                "last_run_status": job.get("last_run_status"),
            }
        )
    st.dataframe(table_rows, width="stretch", hide_index=True)

    job_ids = [job.get("job_id") for job in filtered if job.get("job_id") is not None]
    if not job_ids:
        st.info("No configured jobs match the current filters.")
        return

    st.divider()
    st.markdown("##### Job detail")
    if (
        "selected_config_job_id" in st.session_state
        and st.session_state.selected_config_job_id not in job_ids
    ):
        del st.session_state["selected_config_job_id"]
    selected = st.selectbox(
        "Select a configured job",
        options=job_ids,
        key="selected_config_job_id",
        format_func=lambda jid: f"Job {jid}",
    )
    chosen = next((job for job in filtered if job.get("job_id") == selected), None)
    if chosen is None:
        return

    _format_config_details(chosen)
    st.divider()
    _render_manual_run(chosen)


# ---------------------------------------------------------------------------
# Tab 4 — Execution History
# ---------------------------------------------------------------------------


def _filter_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_filter = st.session_state.history_status_filter
    job_id_text = (st.session_state.history_job_id_filter or "").strip()
    filtered: list[dict[str, Any]] = []
    for row in logs:
        status = str(row.get("last_run_status") or "")
        if status_filter != "All" and status != status_filter:
            continue
        if job_id_text:
            try:
                wanted = int(job_id_text)
            except ValueError:
                return []
            if row.get("job_id") != wanted:
                continue
        filtered.append(row)
    return filtered


def _render_history_tab() -> None:
    st.subheader("Execution history")
    st.caption(
        "Latest 100 executions recorded in "
        "`mubasher_oms.partitioning_job_table_log`."
    )

    _ensure_loaded(
        "partition_job_logs",
        get_partition_job_logs,
        100,
        spinner_text="Loading execution history...",
    )

    if st.button("Refresh History", help="Read-only refresh"):
        _load_into_state(
            "partition_job_logs",
            get_partition_job_logs,
            100,
            spinner_text="Refreshing execution history...",
        )

    if st.session_state.partition_job_logs_error:
        _render_db_error(st.session_state.partition_job_logs_error)
        st.caption(
            "SELECT permission on the log table is required to show execution "
            "history. Ask a DBA to grant only what is needed."
        )
        return

    logs = st.session_state.partition_job_logs or []
    if not logs:
        st.info("No execution history rows were found.")
        return

    f1, f2 = st.columns(2)
    with f1:
        st.text_input("Filter by job_id", key="history_job_id_filter")
    with f2:
        st.selectbox(
            "Status",
            options=STATUS_FILTER_OPTIONS,
            key="history_status_filter",
        )

    filtered = _filter_logs(logs)
    counts: dict[str, int] = {}
    for row in filtered:
        status = str(row.get("last_run_status") or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    if counts:
        metric_cols = st.columns(max(len(counts), 1))
        for column, (status, count) in zip(metric_cols, sorted(counts.items())):
            column.metric(status, count)
            column.markdown(
                _badge(status, _status_badge_kind(status)),
                unsafe_allow_html=True,
            )

    st.markdown(f"**Showing {len(filtered)} of {len(logs)} log row(s).**")
    table_rows = []
    for row in filtered:
        error = row.get("job_error")
        table_rows.append(
            {
                "job_log_id": row.get("job_log_id"),
                "job_id": row.get("job_id"),
                "job_name": row.get("job_name"),
                "last_run_status": row.get("last_run_status"),
                "job_runtime": row.get("job_runtime"),
                "job_error": _shorten(error, 120),
            }
        )
    st.dataframe(table_rows, width="stretch", hide_index=True)

    long_errors = [
        row
        for row in filtered
        if row.get("job_error") and len(str(row.get("job_error"))) > 120
    ]
    if long_errors:
        with st.expander("Expand full error messages", expanded=False):
            for row in long_errors:
                st.markdown(
                    f"**Log {row.get('job_log_id')} / Job {row.get('job_id')} "
                    f"({row.get('last_run_status')}):**"
                )
                st.code(str(row.get("job_error")))


# ---------------------------------------------------------------------------
# Status panels
# ---------------------------------------------------------------------------


def _readiness_badge(exists: Any, allowed: Any) -> str:
    if not exists:
        return _badge("Missing", "fail")
    if allowed is True:
        return _badge("Granted", "ok")
    if allowed is False:
        return _badge("Missing", "warn")
    return _badge("Not checked", "mute")


def _render_readiness_panel() -> None:
    st.markdown("#### Database readiness")
    st.caption("Read-only privilege report. This UI never grants privileges.")
    if st.button("Re-check readiness", help="Read-only privilege check"):
        _load_into_state(
            "database_readiness",
            get_database_readiness,
            spinner_text="Checking database readiness...",
        )

    if st.session_state.database_readiness_error:
        _render_db_error(st.session_state.database_readiness_error)
        return

    readiness = st.session_state.database_readiness
    if not readiness:
        st.info("Readiness information is not available.")
        return

    st.caption(
        f"Role `{readiness.get('db_user')}` on database "
        f"`{readiness.get('db_name')}`."
    )

    rows = [
        (
            "Schema USAGE",
            readiness.get("schema_exists"),
            readiness.get("schema_usage"),
        ),
        (
            "Insert function EXECUTE",
            readiness.get("insert_function_exists"),
            readiness.get("insert_function_execute"),
        ),
        (
            "Config table INSERT",
            readiness.get("config_table_exists"),
            readiness.get("config_table_insert"),
        ),
        (
            "Job ID sequence USAGE",
            readiness.get("sequence_exists"),
            readiness.get("sequence_usage"),
        ),
        (
            "Config table SELECT",
            readiness.get("config_table_exists"),
            readiness.get("config_table_select"),
        ),
        (
            "Log table SELECT",
            readiness.get("log_table_exists"),
            readiness.get("log_table_select"),
        ),
    ]
    for label, exists, allowed in rows:
        st.markdown(
            f"**{label}** {_readiness_badge(exists, allowed)}",
            unsafe_allow_html=True,
        )
    st.caption(
        "Config INSERT and sequence USAGE are only needed when the insert "
        "function is SECURITY INVOKER. Do not grant ALL."
    )


def _render_scheduler_panel() -> None:
    """Describe the externally managed Linux cron scanners (informational only)."""
    st.markdown("#### Linux cron scanners")
    st.caption(
        "Externally managed generic scanners — not one job per table, and not "
        "pgAgent jobs."
    )

    st.markdown("**CREATE scanner**")
    st.markdown(
        f'{_badge("External scheduler", "info")}',
        unsafe_allow_html=True,
    )
    st.markdown(
        "Function: `run_partition_create_jobs()`  \n"
        "Scheduler: Linux cron  \n"
        "Schedule: Every 6 minutes  \n"
        "`0,6,12,18,24,30,36,42,48,54 * * * *`"
    )

    st.markdown("**DROP scanner**")
    st.markdown(
        f'{_badge("External scheduler", "info")}',
        unsafe_allow_html=True,
    )
    st.markdown(
        "Function: `run_partition_drop_jobs()`  \n"
        "Scheduler: Linux cron  \n"
        "Schedule: Every 6 minutes (+3 minute offset)  \n"
        "`3,9,15,21,27,33,39,45,51,57 * * * *`"
    )

    st.info(
        "The generic scanners are executed by Linux cron outside this "
        "application. This UI manages partition-job configuration only."
    )


def _header_status_badges() -> None:
    readiness = st.session_state.database_readiness

    if st.session_state.database_readiness_error:
        db_badge = _badge("Database error", "fail")
    elif not st.session_state.database_readiness_loaded:
        db_badge = _badge("Database not checked", "mute")
    elif readiness and readiness.get("insert_function_execute"):
        db_badge = _badge("Database ready", "ok")
    elif readiness:
        db_badge = _badge("Database limited", "warn")
    else:
        db_badge = _badge("Database unknown", "mute")

    st.markdown(
        f'<div class="pj-header-meta">{db_badge}'
        f'{_badge("Cron scanners", "info")}'
        f'{_badge("Theme: dark", "info")}</div>',
        unsafe_allow_html=True,
    )


def _render_header() -> None:
    st.title(PAGE_TITLE)
    st.markdown(f'<p class="pj-subtitle">{PAGE_SUBTITLE}</p>', unsafe_allow_html=True)
    _header_status_badges()


def main() -> None:
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=None,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    _init_session_state()

    # Lightweight status only — heavy job/log lists load when their view opens.
    if not st.session_state.database_readiness_loaded:
        _load_into_state(
            "database_readiness",
            get_database_readiness,
            spinner_text="Checking database readiness...",
        )

    _render_header()

    with st.sidebar:
        st.caption(
            "Status panels are read-only. Default appearance is dark "
            "(.streamlit/config.toml). Use the Streamlit menu ▸ Settings to "
            "switch Light/Dark when available."
        )
        st.divider()
        _render_readiness_panel()
        st.divider()
        _render_scheduler_panel()

    # Single-view navigation: only the active section renders widgets/queries.
    view = st.radio(
        "Section",
        options=NAV_OPTIONS,
        horizontal=True,
        key="main_nav",
        label_visibility="collapsed",
    )
    st.divider()

    if view == NAV_CONVERT:
        _render_convert_tab()
    elif view == NAV_CREATE:
        _render_new_job_tab()
    elif view == NAV_CONFIGURED:
        _render_configured_jobs_tab()
    else:
        _render_history_tab()


if __name__ == "__main__":
    main()
