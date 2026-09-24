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
from dashboard_metrics import format_age
from job_autofill import calculate_next_run, describe_schedule, validate_six_field_cron
from scheduler_client import fetch_scheduler_status, notify_scheduler_refresh
from ui_overview import export_button, render_health_banner, render_overview, render_top_nav
from ui_theme import theme_css
from validators import ValidationError, validate_form_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PAGE_TITLE = "Partition job management"
PAGE_SUBTITLE = (
    "Configure, monitor, and safely manage PostgreSQL partition jobs."
)

NAV_OVERVIEW = "Overview"
NAV_CONVERT = "Convert Existing Job"
NAV_CREATE = "Create New Job"
NAV_CONFIGURED = "Configured Jobs"
NAV_HISTORY = "Execution History"
NAV_OPTIONS = [NAV_OVERVIEW, NAV_CONVERT, NAV_CREATE, NAV_CONFIGURED, NAV_HISTORY]
NAV_SIDEBAR_LABELS = {
    NAV_OVERVIEW: "Overview",
    NAV_CONVERT: "Convert job",
    NAV_CREATE: "Create new",
    NAV_CONFIGURED: "Configured jobs",
    NAV_HISTORY: "Execution history",
}

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
    """Apply partition.ops visual system."""
    st.markdown(theme_css(), unsafe_allow_html=True)



def _badge(text: str, kind: str = "mute") -> str:
    return f'<span class="pj-badge pj-badge-{kind}">{text}</span>'


def _btn_marker(kind: str) -> None:
    """Invisible marker so CSS can style the next Streamlit button (run/danger)."""
    st.markdown(
        f'<span class="pj-btn-marker pj-btn-{kind}"></span>',
        unsafe_allow_html=True,
    )


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
        "main_nav": NAV_OVERVIEW,
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
    col_a, col_b = st.columns(2)
    with col_a:
        job_name = st.text_input(
            "Job name",
            key=prefix + "job_name",
            help="Stable name shown in the live queue and execution history.",
        )
    with col_b:
        is_enabled = st.checkbox(
            "Enabled",
            key=prefix + "is_enabled",
            help="Disabled jobs stay configured but are not scheduled.",
        )

    operation = st.radio(
        "CREATE / DROP",
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

    st.markdown('<div class="pj-section-label">Target</div>', unsafe_allow_html=True)
    col_schema, col_table = st.columns(2)
    with col_schema:
        table_schema = st.text_input("Schema", key=prefix + "table_schema")
    with col_table:
        table_name = st.text_input("Table", key=prefix + "table_name")

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

    st.markdown('<div class="pj-section-label">Partition settings</div>', unsafe_allow_html=True)
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

    st.markdown('<div class="pj-section-label">Advanced</div>', unsafe_allow_html=True)
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
            # DB open → insert → commit → close happens entirely inside
            # create_partition_job(). Refresh is signaled only after that returns.
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
            "No pgAgent job was created. The dedicated scheduler backend "
            "preloads upcoming jobs and triggers them at their scheduled "
            "next_run_time (schedule-driven near-real-time under normal "
            "operating conditions).",
        ),
    ]
    if result is not None:
        feedback.append(("info", f"Function result: `{result}`"))

    # DB connection is already closed. Signal is wake-up only — not authoritative.
    refresh_ok, refresh_message = notify_scheduler_refresh()
    if refresh_ok:
        feedback.append(("info", refresh_message))
    else:
        feedback.append(("info", refresh_message))

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

    if st.button("Reset", key=prefix + "reset_form", help="Clear this form to defaults"):
        defaults = _shared_form_defaults(
            db_config=EMPTY_DB_CONFIG,
            schedule="0 0 2 * * *",
            job_name="",
        )
        for key, value in defaults.items():
            st.session_state[prefix + key] = value
        st.rerun()


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
        "Select existing job",
        "Detected configuration",
        "Review",
        "Create partition job",
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
    with st.container(border=True):
        st.markdown(
            '<div class="pj-card-eyebrow">Workflow</div>'
            '<div class="pj-card-title">Convert an existing pgAgent partition job</div>',
            unsafe_allow_html=True,
        )
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
                "fill them yourself."
            )
            raw, blocking_error = _render_job_fields(CONVERT_PREFIX)
            st.divider()
            _render_submit_button(
                CONVERT_PREFIX, raw, blocking_error, "Create job"
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
    with st.container(border=True):
        st.markdown(
            '<div class="pj-card-eyebrow">New configuration</div>'
            '<div class="pj-card-title">Create a new parameterised partition job</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "No pgAgent job is needed. This stores one configuration row that the "
            "dedicated scheduler backend triggers at the scheduled next_run_time."
        )
        raw, blocking_error = _render_job_fields(NEW_PREFIX)
        st.divider()
        _render_submit_button(NEW_PREFIX, raw, blocking_error, "Create job")


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
    op_class = "create" if is_create else "drop"
    job_name = job.get("job_name") or f"Job {job.get('job_id')}"

    st.markdown(
        f'<div class="pj-detail-hero">'
        f'<div class="pj-detail-icon">SQL</div>'
        f"<div><strong>{job_name}</strong>"
        f"<span>Parameterized partition routine · #{job.get('job_id')}</span>"
        f"</div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f'{_badge(str(status), status_kind)}'
        f'{_badge("Enabled" if job.get("is_enabled") else "Disabled", "info" if job.get("is_enabled") else "mute")}'
        f'<span class="pj-op-pill {op_class}">{operation}</span>',
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

    st.markdown(
        '<div class="pj-card-eyebrow">Actions</div>',
        unsafe_allow_html=True,
    )
    st.markdown("##### Manual run")
    st.caption(
        "Executes this configured job immediately through "
        "run_partition_job_manual(). The automatic next run time is not changed. "
        "This is a write operation."
    )
    if not allowed:
        st.warning(reason)
        return

    if is_create:
        _btn_marker("run")
        if st.button("▶ Run now", key=f"manual_run_{job_id}", width="stretch"):
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
        _btn_marker("danger")
        if st.button(
            "▶ Run DROP now",
            key=f"manual_run_{job_id}",
            disabled=not confirmed,
            width="stretch",
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
    with st.container(border=True):
        st.markdown(
            '<div class="pj-card-eyebrow">Live queue</div>'
            '<div class="pj-card-title">Configured partition jobs</div>',
            unsafe_allow_html=True,
        )
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

        refresh_col, _spacer = st.columns([1, 3])
        with refresh_col:
            if st.button(
                "↻ Refresh",
                key="refresh_configured_jobs",
                width="stretch",
                help="Read-only refresh",
            ):
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

        st.markdown(
            '<p class="pj-toolbar-note">Filter the live queue</p>',
            unsafe_allow_html=True,
        )
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

        detail_col, run_col = st.columns([1.35, 0.9])
        with detail_col:
            with st.container(border=True):
                st.markdown(
                    f'<div class="pj-card-eyebrow">Selected job · #{chosen.get("job_id")}</div>'
                    '<div class="pj-card-title">Job details</div>',
                    unsafe_allow_html=True,
                )
                _format_config_details(chosen)
        with run_col:
            with st.container(border=True):
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
    with st.container(border=True):
        st.markdown(
            '<div class="pj-card-eyebrow">Recent activity</div>'
            '<div class="pj-card-title">Execution history</div>',
            unsafe_allow_html=True,
        )
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

        refresh_col, _spacer = st.columns([1, 3])
        with refresh_col:
            if st.button(
                "↻ Refresh",
                key="refresh_history",
                width="stretch",
                help="Read-only refresh",
            ):
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


def _probe_scheduler() -> tuple[bool, Any, str]:
    """Fetch scheduler status once per script run for all UI chrome."""
    bundle = st.session_state.get("_scheduler_status_bundle")
    if bundle is None:
        bundle = fetch_scheduler_status()
        st.session_state["_scheduler_status_bundle"] = bundle
    return bundle


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
    """Describe the realtime scheduler backend (management UI is not the scheduler)."""
    from dashboard_metrics import scheduler_uptime as _uptime

    st.markdown("#### Scheduler backend")
    st.caption(
        "Dedicated long-running process that preloads upcoming jobs and triggers "
        "each at next_run_time. Streamlit is configuration-only."
    )

    ok, status, message = _probe_scheduler()
    if ok and status:
        active = bool(status.get("scheduler_active"))
        uptime = _uptime(status)
        st.markdown(
            f'{_badge("Online" if active else "Idle", "ok" if active else "warn")}',
            unsafe_allow_html=True,
        )
        lookahead = status.get("lookahead_seconds")
        reconcile = status.get("reconcile_seconds")
        st.markdown(
            f"**Uptime:** `{uptime.get('label') or '—'}`  \n"
            f"**Last queue refresh:** `{format_age(status.get('last_refresh_at'))}`  \n"
            f"**Jobs in queue:** `{status.get('upcoming_job_count', '—')}`  \n"
            f"**Next job:** `#{status.get('next_job_id') or '—'}`  \n"
            f"**Next execution:** `{status.get('next_expected_run_time') or '—'}`  \n"
            f"**Lookahead:** `{lookahead if lookahead is not None else '—'}` seconds  \n"
            f"**Reconciliation:** `{reconcile if reconcile is not None else '—'}` seconds  \n"
            f"**Last execution:** job `{status.get('last_execution_job_id') or '—'}` → "
            f"`{status.get('last_execution_result') or '—'}`"
        )
    else:
        st.markdown(
            '<div class="pj-offline-panel">'
            "<strong>Scheduler backend unavailable</strong>"
            "<span>Configuration and history remain available, but realtime "
            "scheduler monitoring cannot currently be reached.</span>"
            f'<div class="pj-offline-dot">{_badge("Offline", "fail")}</div>'
            "</div>",
            unsafe_allow_html=True,
        )
        st.caption(message)

    st.caption(
        "Legacy polling runners remain in the database for rollback only and "
        "must stay disabled while this backend is enabled."
    )


def _header_status_badges() -> None:
    jobs = st.session_state.get("partition_jobs") or []
    logs = st.session_state.get("partition_job_logs") or []
    ok, status, _message = _probe_scheduler()
    render_health_banner(
        badge_fn=_badge,
        readiness=st.session_state.database_readiness,
        readiness_error=st.session_state.database_readiness_error,
        jobs=jobs,
        logs=logs,
        scheduler_ok=ok,
        status=status,
    )


def _render_header(active_view: str) -> None:
    st.markdown(
        f'<div class="pj-breadcrumb"><span>Workspace</span><span>/</span>'
        f"<strong>{NAV_SIDEBAR_LABELS.get(active_view, active_view)}</strong></div>",
        unsafe_allow_html=True,
    )
    title_col, action_col = st.columns([3.1, 1.5])
    with title_col:
        st.markdown(
            '<div class="pj-eyebrow"><span class="pj-eyebrow-dot"></span>'
            "Operations console</div>",
            unsafe_allow_html=True,
        )
        st.title(PAGE_TITLE)
        st.markdown(
            f'<p class="pj-subtitle">{PAGE_SUBTITLE}</p>',
            unsafe_allow_html=True,
        )
    with action_col:
        export_col, new_col = st.columns(2)
        with export_col:
            export_button(st.session_state.get("partition_jobs") or [])
        with new_col:
            if st.button(
                "+ New job",
                key="hdr_new_job",
                type="primary",
                width="stretch",
                help="Open Create New Job",
            ):
                st.session_state.main_nav = NAV_CREATE
                st.rerun()
    _header_status_badges()


def _render_sidebar_brand() -> None:
    st.markdown(
        '<div class="pj-brand">'
        '<div class="pj-brand-icon">P</div>'
        "<div><strong>Partition<span> Manager</span></strong>"
        "<small>PostgreSQL control plane</small></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="pj-workspace">'
        '<div class="pj-workspace-avatar">MO</div>'
        "<div><span>Workspace</span><strong>mubasher_oms</strong></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown('<div class="pj-nav-kicker">Manage</div>', unsafe_allow_html=True)


def _render_sidebar_scheduler_chip() -> None:
    ok, status, message = _probe_scheduler()
    if ok and status and bool(status.get("scheduler_active")):
        title = "Scheduler online"
        age = format_age(status.get("last_refresh_at"))
        if age == "—":
            detail = str(status.get("last_refresh_result") or "Heartbeat ok")
        else:
            detail = f"Last sync {age}"
        dot = ""
    elif ok and status:
        title = "Scheduler idle"
        detail = str(status.get("last_refresh_result") or "Backend reachable")
        dot = " off"
    else:
        title = "Scheduler offline"
        detail = _shorten(message, 56)
        dot = " off"
    st.markdown(
        f'<div class="pj-connection"><div class="pj-live-dot{dot}"></div>'
        f"<div><strong>{title}</strong><span>{detail}</span></div></div>",
        unsafe_allow_html=True,
    )


def _render_page_footer() -> None:
    ok, status, _message = _probe_scheduler()
    if ok and status and status.get("scheduler_active"):
        heartbeat = format_age(status.get("last_refresh_at"))
        if heartbeat == "—":
            heartbeat = str(status.get("last_refresh_result") or "ok")
        state = "All systems operational"
    elif ok and status:
        heartbeat = format_age(status.get("last_refresh_at"))
        state = "Scheduler idle"
    else:
        heartbeat = "unavailable"
        state = "Scheduler unavailable"
    st.markdown(
        '<div class="pj-footer">'
        "<strong>Partition Manager</strong>"
        f"<span>Scheduler heartbeat {heartbeat}</span>"
        f"<span>{state}</span>"
        "</div>",
        unsafe_allow_html=True,
    )


def _render_overview_tab() -> None:
    render_overview(
        badge_fn=_badge,
        ensure_loaded_fn=_ensure_loaded,
        load_jobs_fn=get_partition_jobs,
        load_logs_fn=get_partition_job_logs,
        probe_scheduler_fn=_probe_scheduler,
        render_db_error_fn=_render_db_error,
        render_manual_run_fn=_render_manual_run,
        nav_configured=NAV_CONFIGURED,
        nav_history=NAV_HISTORY,
        get_partition_jobs_error=st.session_state.partition_jobs_error,
        get_partition_logs_error=st.session_state.partition_job_logs_error,
    )


def main() -> None:
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=None,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    _init_session_state()
    st.session_state["_scheduler_status_bundle"] = fetch_scheduler_status()

    if not st.session_state.database_readiness_loaded:
        _load_into_state(
            "database_readiness",
            get_database_readiness,
            spinner_text="Checking database readiness...",
        )

    with st.sidebar:
        _render_sidebar_brand()
        view = st.radio(
            "Section",
            options=NAV_OPTIONS,
            key="main_nav",
            label_visibility="collapsed",
            format_func=lambda item: NAV_SIDEBAR_LABELS.get(item, item),
        )
        st.markdown(
            '<div class="pj-nav-kicker second">System</div>',
            unsafe_allow_html=True,
        )
        _render_sidebar_scheduler_chip()
        with st.expander("Database readiness", expanded=False):
            _render_readiness_panel()
        with st.expander("Realtime scheduler", expanded=False):
            _render_scheduler_panel()
        st.caption("Use the top-left control to reopen the sidebar if it is closed.")

    render_top_nav(
        nav_options=NAV_OPTIONS,
        nav_labels=NAV_SIDEBAR_LABELS,
        active_view=view,
    )
    _render_header(view)

    if view == NAV_OVERVIEW:
        _render_overview_tab()
    elif view == NAV_CONVERT:
        _render_convert_tab()
    elif view == NAV_CREATE:
        _render_new_job_tab()
    elif view == NAV_CONFIGURED:
        _render_configured_jobs_tab()
    else:
        _render_history_tab()

    _render_page_footer()


if __name__ == "__main__":
    main()