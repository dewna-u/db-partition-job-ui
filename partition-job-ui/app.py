"""Database Partition Job Management — lightweight Streamlit UI."""

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
    get_generic_partition_schedulers,
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

PAGE_TITLE = "Database Partition Job Management"

# Never seed example settings here: Database Configuration is either extracted
# from the called partition function or entered deliberately by the user.
EMPTY_DB_CONFIG = "{}"

GENERIC_DB_ERROR = "The database operation failed. Check the server logs for details."

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

CONFIGURED_JOB_COLUMNS = [
    "job_id",
    "job_name",
    "is_enabled",
    "table_schema",
    "table_name",
    "frequency",
    "last_run_time",
    "next_run_time",
    "last_run_status",
    "partition_unit",
    "partition_period",
    "is_create",
    "create_drop_interval",
    "job_schedule",
]

LOG_COLUMNS = [
    "job_log_id",
    "job_id",
    "job_name",
    "last_run_status",
    "job_runtime",
    "job_error",
]

FREQUENCY_UNITS = ["minute", "hour", "day", "week", "month", "year"]
PARTITION_UNITS = ["day", "week", "month", "year"]
INTERVAL_UNITS = ["day", "week", "month", "year"]
OPERATIONS = ["CREATE", "DROP"]

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
    """Minimal CSS: centred layout, plain controls."""
    st.markdown(
        """
<style>
    .block-container {
        max-width: 1100px;
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }
    [data-testid="stSidebar"],
    [data-testid="stSidebarCollapsedControl"] {
        display: none;
    }
    div[data-testid="stCaptionContainer"] {
        margin-top: -0.5rem;
        margin-bottom: 0.75rem;
        color: #6B7280;
        font-size: 0.85rem;
    }
    h2 {
        margin-top: 0.5rem;
    }
</style>
""",
        unsafe_allow_html=True,
    )


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
    }
    # selected_config_job_id is deliberately not pre-seeded: Streamlit rejects a
    # selectbox session value that is not present in its options list.
    for key, value in simple_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    for state_key in (
        "partition_jobs",
        "partition_job_logs",
        "database_readiness",
        "generic_schedulers",
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


def _load_into_state(state_key: str, loader: Callable[..., Any], *args: Any) -> None:
    """Run a read-only loader once and keep result/error in session state."""
    try:
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


def _load_jobs() -> None:
    """Fetch pgAgent jobs and store them in session state."""
    try:
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


# ---------------------------------------------------------------------------
# Shared configuration form
# ---------------------------------------------------------------------------


def _render_job_fields(prefix: str) -> tuple[dict[str, Any], Optional[str]]:
    """
    Render the shared configuration fields.

    Returns (raw values for validate_form_data, blocking_error_or_None).
    Both tabs use this so the two paths always produce the same structure.
    """
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

    col_a, col_b = st.columns(2)
    with col_a:
        job_name = st.text_input("Job Name", key=prefix + "job_name")
        table_schema = st.text_input("Table Schema", key=prefix + "table_schema")
    with col_b:
        is_enabled = st.checkbox("Enabled", key=prefix + "is_enabled")
        table_name = st.text_input("Table Name", key=prefix + "table_name")

    db_config = st.text_area(
        "Database Configuration (JSON object)",
        key=prefix + "db_config",
        height=110,
        help=(
            "Session settings applied while the partition operation runs. When a "
            "pgAgent job is loaded this is read from the called function's own "
            "SET / set_config statements, so an empty object means the function "
            "sets nothing. Whatever is shown here is stored as-is; no settings "
            "are added for you."
        ),
    )

    job_schedule = st.text_input("Job Schedule (six fields)", key=prefix + "job_schedule")
    st.caption("second minute hour day-of-month month day-of-week")

    blocking_error: Optional[str] = None
    cron_error = validate_six_field_cron(job_schedule or "")
    calculated_next_run: Optional[datetime] = None
    if cron_error:
        st.error(cron_error)
        blocking_error = cron_error
    else:
        calculated_next_run = calculate_next_run(job_schedule)
        meaning = describe_schedule(job_schedule)
        preview = f"Meaning: {meaning}. " if meaning else ""
        if calculated_next_run:
            st.success(f"{preview}Next run: {calculated_next_run:%Y-%m-%d %H:%M:%S}")
        else:
            st.warning(
                f"{preview}The next occurrence could not be calculated from this "
                "schedule. Set the next run manually."
            )

    st.markdown("**Job frequency** — how often this job executes.")
    freq_col1, freq_col2 = st.columns(2)
    with freq_col1:
        frequency_amount = st.number_input(
            "Frequency", min_value=1, step=1, key=prefix + "frequency_amount"
        )
    with freq_col2:
        frequency_unit = st.selectbox(
            "Frequency Unit", options=FREQUENCY_UNITS, key=prefix + "frequency_unit"
        )

    auto_next_run = st.checkbox(
        "Use the next run calculated from the schedule",
        key=prefix + "auto_next_run",
    )
    next_run_time: Optional[datetime]
    if auto_next_run and calculated_next_run is not None:
        next_run_time = calculated_next_run
        st.caption(
            f"Next Run Time will be stored as {calculated_next_run:%Y-%m-%d %H:%M:%S} "
            "(calculated from the schedule)."
        )
    else:
        if auto_next_run:
            st.caption("Automatic calculation unavailable — using the manual value.")
        else:
            st.caption("Manual next run selected — it overrides the schedule preview.")
        date_col, time_col = st.columns(2)
        with date_col:
            next_run_date = st.date_input("Next Run Date", key=prefix + "next_run_date")
        with time_col:
            next_run_value = st.time_input("Next Run Time", key=prefix + "next_run_time")
        next_run_time = _combine_datetime(next_run_date, next_run_value)

    st.markdown("**Partition size** — how much data each partition covers.")
    part_col1, part_col2 = st.columns(2)
    with part_col1:
        partition_unit = st.selectbox(
            "Partition Unit", options=PARTITION_UNITS, key=prefix + "partition_unit"
        )
    with part_col2:
        partition_period = st.number_input(
            "Partition Period", min_value=1, step=1, key=prefix + "partition_period"
        )

    if is_create:
        interval_label = "Create Ahead Interval"
        interval_help = (
            "How far beyond the current maximum partition boundary should future "
            "partitions be created?"
        )
    else:
        interval_label = "Retention Interval"
        interval_help = "Partitions older than this interval may be dropped."

    st.markdown(f"**{interval_label}**")
    cd_col1, cd_col2 = st.columns(2)
    with cd_col1:
        create_drop_amount = st.number_input(
            interval_label,
            min_value=1,
            step=1,
            key=prefix + "create_drop_amount",
            help=interval_help,
        )
    with cd_col2:
        create_drop_unit = st.selectbox(
            "Interval Unit", options=INTERVAL_UNITS, key=prefix + "create_drop_unit"
        )

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
        result = create_partition_job(validated)
    except DatabaseError as exc:
        # A failed write must stay retryable: no fingerprint is recorded.
        return [("error", exc.message)]
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while creating partition job")
        return [("error", GENERIC_DB_ERROR)]

    # Reached only after the database transaction committed successfully.
    st.session_state.last_created_fingerprint = fingerprint

    feedback: list[tuple[str, str]] = [
        (
            "success",
            "Partition job configuration stored by "
            "mubasher_oms.insert_data_to_partition_job_table().",
        ),
        (
            "info",
            "No pgAgent job was created. The two generic scanners pick this "
            "configuration up when its next run time is due.",
        ),
    ]
    if result is not None:
        feedback.append(("info", f"Function result: `{result}`"))

    # Refresh the configuration list so the new row is visible on the next render.
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
    if st.button(
        label,
        type="primary",
        key=prefix + "submit",
        disabled=blocking_error is not None,
    ):
        # Guard against re-entrant handling within one script run.
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


def _apply_autofill(details: dict[str, Any]) -> None:
    """Copy safe auto-fill values into the convert-tab widgets. Read-only step."""
    autofill = details.get("autofill") or {}
    for source_key, suffix in _AUTOFILL_TO_FIELD.items():
        if source_key not in autofill:
            continue
        value = autofill[source_key]
        if source_key in _INTEGER_FIELDS:
            value = int(value)
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

    st.session_state.load_warnings = list(details.get("warnings") or [])
    st.session_state.step_choices = list(details.get("step_choices") or [])
    st.session_state.loaded_job_id = details.get("job_id")
    st.session_state.load_error = None
    job_label = details.get("job_name") or details.get("job_id")
    st.session_state.load_info = (
        f"Loaded pgAgent job {details.get('job_id')}: {job_label}. "
        "Nothing was written. Review the values, then create the configuration."
    )


def _load_job_details(job_id: int, step_id: Any = None) -> None:
    try:
        details = get_pgagent_job_details(job_id, step_id=step_id)
    except (PgAgentNotInstalledError, DatabaseError) as exc:
        st.session_state.load_error = exc.message
        st.session_state.load_warnings = []
        st.session_state.load_info = None
        st.session_state.step_choices = []
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while loading pgAgent job details")
        st.session_state.load_error = GENERIC_DB_ERROR
        st.session_state.load_warnings = []
        st.session_state.load_info = None
        st.session_state.step_choices = []
        return
    _apply_autofill(details)


def _render_pgagent_jobs() -> None:
    col_info, col_btn = st.columns([3, 1])
    with col_info:
        if (
            st.session_state.jobs_loaded
            and not st.session_state.jobs_error
            and not st.session_state.pgagent_missing
        ):
            st.markdown(f"**Total pgAgent jobs:** {len(st.session_state.jobs)}")
    with col_btn:
        if st.button("Refresh Jobs", use_container_width=True):
            _load_jobs()

    if st.session_state.pgagent_missing:
        st.warning(st.session_state.jobs_error)
        return
    if st.session_state.jobs_error:
        st.error(st.session_state.jobs_error)
        return
    if not st.session_state.jobs:
        st.info("No pgAgent jobs were found.")
        return

    st.dataframe(
        [
            {JOB_COLUMN_LABELS[col]: job.get(col) for col in JOB_COLUMNS}
            for job in st.session_state.jobs
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_convert_tab() -> None:
    st.subheader("Convert an existing pgAgent partition job")
    st.caption(
        "Migration path: read an old table-specific pgAgent job and turn it into "
        "one parameterised configuration row. Loading is strictly read-only."
    )

    with st.expander("Existing pgAgent jobs", expanded=False):
        _render_pgagent_jobs()

    col_id, col_btn = st.columns([2, 1])
    with col_id:
        st.number_input("pgAgent Job ID", min_value=1, step=1, key="load_job_id")
    with col_btn:
        st.write("")
        if st.button("Load Job Details", use_container_width=True):
            _load_job_details(int(st.session_state.load_job_id))

    if st.session_state.step_choices:
        choice_labels = {}
        for choice in st.session_state.step_choices:
            function_label = "function not identified"
            if choice.get("function_schema") and choice.get("function_name"):
                function_label = (
                    f"{choice['function_schema']}.{choice['function_name']}"
                )
            step_name = choice.get("step_name") or f"Step {choice.get('step_id')}"
            choice_labels[choice["step_id"]] = f"{step_name} ({function_label})"
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
        st.error(st.session_state.load_error)
    if st.session_state.load_info:
        st.info(st.session_state.load_info)
    for warning in st.session_state.load_warnings:
        st.warning(warning)

    st.divider()
    raw, blocking_error = _render_job_fields(CONVERT_PREFIX)
    st.divider()
    _render_submit_button(
        CONVERT_PREFIX, raw, blocking_error, "Create Partition Job"
    )
    st.caption(
        "The original pgAgent job is never modified, disabled, or deleted. "
        "Retiring it is a separate DBA decision."
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


def _format_config_details(job: dict[str, Any]) -> None:
    is_create = bool(job.get("is_create"))
    operation = "CREATE" if is_create else "DROP"
    interval_label = "Create-ahead interval" if is_create else "Retention interval"

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Job ID:** {job.get('job_id')}")
        st.markdown(f"**Job Name:** {job.get('job_name')}")
        st.markdown(
            f"**Target table:** {job.get('table_schema')}.{job.get('table_name')}"
        )
        st.markdown(f"**Operation:** {operation}")
        st.markdown(f"**Enabled:** {bool(job.get('is_enabled'))}")
        st.markdown(f"**Schedule:** `{job.get('job_schedule')}`")
    with col2:
        st.markdown(f"**Frequency:** {job.get('frequency')}")
        st.markdown(f"**Next run:** {job.get('next_run_time')}")
        st.markdown(f"**Last run:** {job.get('last_run_time')}")
        st.markdown(f"**Last status:** {job.get('last_run_status')}")
        st.markdown(
            f"**Partition size:** {job.get('partition_period')} "
            f"{job.get('partition_unit')}"
        )
        st.markdown(f"**{interval_label}:** {job.get('create_drop_interval')}")

    meaning = describe_schedule(str(job.get("job_schedule") or ""))
    if meaning:
        st.caption(f"Schedule meaning: {meaning}")
    st.markdown("**Database configuration:**")
    st.code(json.dumps(job.get("db_config_para"), indent=2, default=str), language="json")


def _render_manual_run(job: dict[str, Any]) -> None:
    job_id = job.get("job_id")
    is_create = bool(job.get("is_create"))

    st.markdown("**Manual run**")
    st.caption(
        "Executes this configured job immediately through "
        "run_partition_job_manual(). The automatic next run time is not changed."
    )

    if is_create:
        if st.button("Run CREATE Job Now", key=f"manual_run_{job_id}"):
            _execute_manual_run(job_id)
    else:
        confirmed = st.checkbox(
            "I understand that this operation may permanently drop old table "
            "partitions and their data.",
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
        st.session_state.manual_run_feedback = [("error", exc.message)]
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error during manual partition job run")
        st.session_state.manual_run_feedback = [("error", GENERIC_DB_ERROR)]
        return

    feedback = [("success", f"Manual run of job {job_id} completed and committed.")]
    if result is not None:
        feedback.append(("info", f"Function result: `{result}`"))
    st.session_state.manual_run_feedback = feedback
    # Execution history and job state changed — reload both.
    _load_into_state("partition_job_logs", get_partition_job_logs, 100)
    _load_into_state("partition_jobs", get_partition_jobs)


def _render_configured_jobs_tab() -> None:
    st.subheader("Configured partition jobs")
    st.caption(
        "Every row is one parameterised partition job in "
        "mubasher_oms.partitioning_job_table. These rows replaced the old "
        "per-table pgAgent jobs."
    )

    if st.button("Refresh Configured Jobs"):
        _load_into_state("partition_jobs", get_partition_jobs)

    if st.session_state.partition_jobs_error:
        st.error(st.session_state.partition_jobs_error)
        st.caption(
            "SELECT permission on the configuration table is required to list "
            "configured jobs. Ask a DBA to grant only what is needed."
        )
        return

    jobs = st.session_state.partition_jobs or []
    if not jobs:
        st.info("No parameterised partition jobs are configured yet.")
        return

    st.markdown(f"**Total configured jobs:** {len(jobs)}")
    st.dataframe(
        [{col: job.get(col) for col in CONFIGURED_JOB_COLUMNS} for job in jobs],
        use_container_width=True,
        hide_index=True,
    )

    job_ids = [job.get("job_id") for job in jobs if job.get("job_id") is not None]
    if not job_ids:
        return

    st.divider()
    # Drop a stale selection so the widget never receives an out-of-range value.
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
    chosen = next((job for job in jobs if job.get("job_id") == selected), None)
    if chosen is None:
        return

    _format_config_details(chosen)
    st.divider()
    _render_manual_run(chosen)


# ---------------------------------------------------------------------------
# Tab 4 — Execution History
# ---------------------------------------------------------------------------


def _render_history_tab() -> None:
    st.subheader("Execution history")
    st.caption(
        "Latest 100 executions recorded in mubasher_oms.partitioning_job_table_log."
    )

    if st.button("Refresh History"):
        _load_into_state("partition_job_logs", get_partition_job_logs, 100)

    if st.session_state.partition_job_logs_error:
        st.error(st.session_state.partition_job_logs_error)
        st.caption(
            "SELECT permission on the log table is required to show execution "
            "history. Ask a DBA to grant only what is needed."
        )
        return

    logs = st.session_state.partition_job_logs or []
    if not logs:
        st.info("No execution history rows were found.")
        return

    counts: dict[str, int] = {}
    for row in logs:
        status = str(row.get("last_run_status") or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    if counts:
        metric_cols = st.columns(len(counts))
        for column, (status, count) in zip(metric_cols, sorted(counts.items())):
            column.metric(status, count)

    st.dataframe(
        [{col: row.get(col) for col in LOG_COLUMNS} for row in logs],
        use_container_width=True,
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Status panels
# ---------------------------------------------------------------------------


def _readiness_line(label: str, exists: Any, allowed: Any) -> str:
    if not exists:
        return f"- {label}: **missing**"
    if allowed is None:
        return f"- {label}: unknown"
    return f"- {label}: {'**granted**' if allowed else '**not granted**'}"


def _render_readiness_panel() -> None:
    with st.expander("Database readiness (read-only)", expanded=False):
        if st.button("Re-check readiness"):
            _load_into_state("database_readiness", get_database_readiness)

        if st.session_state.database_readiness_error:
            st.error(st.session_state.database_readiness_error)
            return

        readiness = st.session_state.database_readiness
        if not readiness:
            st.info("Readiness information is not available.")
            return

        st.caption(
            f"Role `{readiness.get('db_user')}` on database "
            f"`{readiness.get('db_name')}`. This panel only reports privileges; "
            "it never grants them."
        )
        lines = [
            _readiness_line(
                "Schema USAGE",
                readiness.get("schema_exists"),
                readiness.get("schema_usage"),
            ),
            _readiness_line(
                "Insert function EXECUTE",
                readiness.get("insert_function_exists"),
                readiness.get("insert_function_execute"),
            ),
            _readiness_line(
                "Configuration table INSERT",
                readiness.get("config_table_exists"),
                readiness.get("config_table_insert"),
            ),
            _readiness_line(
                "Configuration table SELECT",
                readiness.get("config_table_exists"),
                readiness.get("config_table_select"),
            ),
            _readiness_line(
                "Log table SELECT",
                readiness.get("log_table_exists"),
                readiness.get("log_table_select"),
            ),
            _readiness_line(
                "Job ID sequence USAGE",
                readiness.get("sequence_exists"),
                readiness.get("sequence_usage"),
            ),
        ]
        st.markdown("\n".join(lines))
        st.caption(
            "Configuration table INSERT and sequence USAGE are only required when "
            "the insert function is SECURITY INVOKER."
        )


def _render_scheduler_panel() -> None:
    with st.expander("Generic scheduler status (read-only)", expanded=False):
        st.caption(
            "The new architecture needs only these two pgAgent jobs. They poll the "
            "configuration table; they are not created per table."
        )
        if st.button("Re-check schedulers"):
            _load_into_state("generic_schedulers", get_generic_partition_schedulers)

        if st.session_state.generic_schedulers_error:
            st.error(st.session_state.generic_schedulers_error)
            return

        schedulers = st.session_state.generic_schedulers
        if schedulers is None:
            st.info("Scheduler information is not available.")
            return

        for key, label in (
            ("create_scheduler", "Generic CREATE Scheduler"),
            ("drop_scheduler", "Generic DROP Scheduler"),
        ):
            found = schedulers.get(key)
            if not found:
                st.warning(f"{label}: Missing")
                continue
            st.markdown(
                f"**{label}: Found** — pgAgent Job ID {found.get('job_id')}, "
                f"`{found.get('job_name')}`, enabled: {found.get('enabled')}, "
                f"schedule: `{found.get('schedule') or 'not shown'}`"
            )

        if not schedulers.get("create_scheduler") or not schedulers.get(
            "drop_scheduler"
        ):
            st.info(
                "A generic scheduler is missing. Create it through the approved "
                "pgAgent/DBA deployment process. This application never writes to "
                "the pgAgent catalog."
            )


# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=None,
        layout="centered",
        initial_sidebar_state="collapsed",
    )
    _inject_css()
    _init_session_state()

    st.title(PAGE_TITLE)

    # One-time loads per session — no polling, no timed refresh.
    if not st.session_state.jobs_loaded:
        _load_jobs()
    if not st.session_state.database_readiness_loaded:
        _load_into_state("database_readiness", get_database_readiness)
    if not st.session_state.generic_schedulers_loaded:
        _load_into_state("generic_schedulers", get_generic_partition_schedulers)
    if not st.session_state.partition_jobs_loaded:
        _load_into_state("partition_jobs", get_partition_jobs)
    if not st.session_state.partition_job_logs_loaded:
        _load_into_state("partition_job_logs", get_partition_job_logs, 100)

    _render_readiness_panel()
    _render_scheduler_panel()

    convert_tab, new_tab, configured_tab, history_tab = st.tabs(
        [
            "Convert Existing Job",
            "Create New Job",
            "Configured Jobs",
            "Execution History",
        ]
    )
    with convert_tab:
        _render_convert_tab()
    with new_tab:
        _render_new_job_tab()
    with configured_tab:
        _render_configured_jobs_tab()
    with history_tab:
        _render_history_tab()


if __name__ == "__main__":
    main()
