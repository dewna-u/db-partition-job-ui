"""Partition Job Configuration — lightweight Streamlit UI."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from typing import Any

import streamlit as st

from database import (
    DatabaseError,
    PgAgentNotInstalledError,
    create_partition_job,
    get_pgagent_job_details,
    get_pgagent_jobs,
)
from validators import ValidationError, validate_form_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PAGE_TITLE = "Partition Job Configuration"

DEFAULT_DB_CONFIG = """{
  "work_mem": "512MB",
  "maintenance_work_mem": "1GB"
}"""

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

FREQUENCY_UNITS = ["minute", "hour", "day", "week", "month"]
PARTITION_UNITS = ["day", "week", "month", "year"]
CREATE_DROP_UNITS = ["day", "week", "month"]

_AUTOFILL_TO_WIDGET = {
    "job_name": "form_job_name",
    "is_enabled": "form_is_enabled",
    "table_schema": "form_table_schema",
    "table_name": "form_table_name",
    "db_config": "form_db_config",
    "job_schedule": "form_job_schedule",
    "frequency_amount": "form_frequency_amount",
    "frequency_unit": "form_frequency_unit",
    "partition_unit": "form_partition_unit",
    "partition_period": "form_partition_period",
    "is_create": "form_is_create",
    "create_drop_amount": "form_create_drop_amount",
    "create_drop_unit": "form_create_drop_unit",
}


def _inject_css() -> None:
    """Minimal CSS: centred layout, white containers, plain controls."""
    st.markdown(
        """
<style>
    .block-container {
        max-width: 900px;
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


def _init_session_state() -> None:
    if "jobs_loaded" not in st.session_state:
        st.session_state.jobs_loaded = False
    if "jobs" not in st.session_state:
        st.session_state.jobs = []
    if "jobs_error" not in st.session_state:
        st.session_state.jobs_error = None
    if "pgagent_missing" not in st.session_state:
        st.session_state.pgagent_missing = False
    if "create_feedback" not in st.session_state:
        st.session_state.create_feedback = None
    if "load_warnings" not in st.session_state:
        st.session_state.load_warnings = []
    if "load_error" not in st.session_state:
        st.session_state.load_error = None
    if "load_info" not in st.session_state:
        st.session_state.load_info = None
    if "step_choices" not in st.session_state:
        st.session_state.step_choices = []
    if "loaded_job_id" not in st.session_state:
        st.session_state.loaded_job_id = None
    if "load_job_id" not in st.session_state:
        st.session_state.load_job_id = 1
    if "create_in_flight" not in st.session_state:
        st.session_state.create_in_flight = False
    if "last_created_fingerprint" not in st.session_state:
        st.session_state.last_created_fingerprint = None

    now = datetime.now().replace(microsecond=0)
    form_defaults: dict[str, Any] = {
        "form_job_name": "JOB_TEST_PARTITIONING_2",
        "form_is_enabled": True,
        "form_table_schema": "mubasher_oms",
        "form_table_name": "test_partitions",
        "form_db_config": DEFAULT_DB_CONFIG,
        "form_job_schedule": "0 30 2 * * *",
        "form_frequency_amount": 1,
        "form_frequency_unit": "day",
        "form_next_run_date": now.date(),
        "form_next_run_time": now.time(),
        "form_partition_unit": "day",
        "form_partition_period": 1,
        "form_is_create": True,
        "form_create_drop_amount": 5,
        "form_create_drop_unit": "day",
    }
    for key, value in form_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _load_jobs() -> None:
    """Fetch pgAgent jobs once and store them in session state."""
    try:
        jobs = get_pgagent_jobs()
        st.session_state.jobs = jobs
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
        st.session_state.jobs_error = (
            "The database operation failed. Check the server logs for details."
        )
        st.session_state.pgagent_missing = False
    finally:
        st.session_state.jobs_loaded = True


def _job_id_set(jobs: list[dict]) -> set[Any]:
    return {job.get("job_id") for job in jobs if job.get("job_id") is not None}


def _render_jobs_section() -> None:
    st.header("Existing pgAgent Jobs")

    col_info, col_btn = st.columns([3, 1])
    with col_info:
        if (
            st.session_state.jobs_loaded
            and not st.session_state.jobs_error
            and not st.session_state.pgagent_missing
        ):
            st.markdown(f"**Total Jobs:** {len(st.session_state.jobs)}")
    with col_btn:
        if st.button("Refresh Jobs", use_container_width=True):
            _load_jobs()
            st.rerun()

    if st.session_state.pgagent_missing:
        st.warning(st.session_state.jobs_error)
        return

    if st.session_state.jobs_error:
        st.error(st.session_state.jobs_error)
        return

    jobs = st.session_state.jobs
    if not jobs:
        st.info("No pgAgent jobs were found.")
        return

    # Build display rows with Job ID first; keep read-only.
    display_rows = []
    for job in jobs:
        display_rows.append(
            {JOB_COLUMN_LABELS[col]: job.get(col) for col in JOB_COLUMNS}
        )

    st.dataframe(
        display_rows,
        use_container_width=True,
        hide_index=True,
    )


def _combine_datetime(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def submission_fingerprint(validated: dict[str, Any]) -> str:
    """Stable signature of a validated payload, used to ignore duplicate submits."""
    return json.dumps(
        {key: str(value) for key, value in sorted(validated.items())},
        sort_keys=True,
    )


def _apply_autofill(details: dict[str, Any]) -> None:
    """Copy safe auto-fill values into widget session state before the next render."""
    autofill = details.get("autofill") or {}
    for source_key, widget_key in _AUTOFILL_TO_WIDGET.items():
        if source_key not in autofill:
            continue
        value = autofill[source_key]
        if source_key in {"frequency_amount", "partition_period", "create_drop_amount"}:
            value = int(value)
        st.session_state[widget_key] = value

    next_run = autofill.get("next_run_time")
    if isinstance(next_run, datetime):
        st.session_state.form_next_run_date = next_run.date()
        st.session_state.form_next_run_time = next_run.time().replace(microsecond=0)
    elif isinstance(next_run, date) and not isinstance(next_run, datetime):
        st.session_state.form_next_run_date = next_run

    st.session_state.load_warnings = list(details.get("warnings") or [])
    st.session_state.step_choices = list(details.get("step_choices") or [])
    st.session_state.loaded_job_id = details.get("job_id")
    st.session_state.load_error = None
    job_label = details.get("job_name") or details.get("job_id")
    st.session_state.load_info = (
        f"Loaded pgAgent job {details.get('job_id')}: {job_label}. "
        "Review the form before creating a partition job configuration."
    )


def _load_job_details(job_id: int, step_id: Any = None) -> None:
    try:
        details = get_pgagent_job_details(job_id, step_id=step_id)
    except PgAgentNotInstalledError as exc:
        st.session_state.load_error = exc.message
        st.session_state.load_warnings = []
        st.session_state.load_info = None
        st.session_state.step_choices = []
        return
    except DatabaseError as exc:
        st.session_state.load_error = exc.message
        st.session_state.load_warnings = []
        st.session_state.load_info = None
        st.session_state.step_choices = []
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while loading pgAgent job details")
        st.session_state.load_error = (
            "The database operation failed. Check the server logs for details."
        )
        st.session_state.load_warnings = []
        st.session_state.load_info = None
        st.session_state.step_choices = []
        return
    _apply_autofill(details)


def _render_job_loader() -> None:
    st.subheader("Load from pgAgent Job")
    st.caption(
        "Enter an existing pgAgent Job ID to prepare the form. "
        "This does not create or update any database records."
    )

    col_id, col_btn = st.columns([2, 1])
    with col_id:
        st.number_input(
            "pgAgent Job ID",
            min_value=1,
            step=1,
            key="load_job_id",
        )
    with col_btn:
        st.write("")
        load_clicked = st.button("Load Job Details", use_container_width=True)

    if load_clicked:
        try:
            job_id = int(st.session_state.load_job_id)
        except (TypeError, ValueError):
            st.session_state.load_error = "pgAgent Job ID must be a whole number."
            st.session_state.load_warnings = []
            st.session_state.load_info = None
            st.rerun()
            return
        _load_job_details(job_id)
        st.rerun()

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
            st.rerun()

    if st.session_state.load_error:
        st.error(st.session_state.load_error)
    if st.session_state.load_info:
        st.info(st.session_state.load_info)
    for warning in st.session_state.load_warnings:
        st.warning(warning)


def _show_create_feedback() -> None:
    """Render one-shot success/info messages after a create + rerun."""
    feedback = st.session_state.create_feedback
    if not feedback:
        return
    for item in feedback:
        kind = item.get("kind", "info")
        text = item.get("text", "")
        if kind == "success":
            st.success(text)
        elif kind == "error":
            st.error(text)
        else:
            st.info(text)
    st.session_state.create_feedback = None


def _render_create_form() -> None:
    st.header("Create Partition Job")
    _show_create_feedback()

    with st.form("create_partition_job_form", clear_on_submit=False):
        job_name = st.text_input("Job Name", key="form_job_name")
        is_enabled = st.checkbox("Enabled", key="form_is_enabled")

        table_schema = st.text_input("Table Schema", key="form_table_schema")
        table_name = st.text_input("Table Name", key="form_table_name")

        db_config = st.text_area(
            "Database Configuration",
            key="form_db_config",
            height=120,
        )

        job_schedule = st.text_input("Job Schedule", key="form_job_schedule")
        st.caption(
            "Cron schedule used by the partition scheduler. "
            "Confirm whether your scheduler expects five or six fields."
        )

        freq_col1, freq_col2 = st.columns([1, 1])
        with freq_col1:
            frequency_amount = st.number_input(
                "Frequency",
                min_value=1,
                step=1,
                key="form_frequency_amount",
            )
        with freq_col2:
            frequency_unit = st.selectbox(
                "Frequency Unit",
                options=FREQUENCY_UNITS,
                key="form_frequency_unit",
            )

        date_col, time_col = st.columns([1, 1])
        with date_col:
            next_run_date = st.date_input("Next Run Date", key="form_next_run_date")
        with time_col:
            next_run_time_val = st.time_input(
                "Next Run Time",
                key="form_next_run_time",
            )

        partition_unit = st.selectbox(
            "Partition Unit",
            options=PARTITION_UNITS,
            key="form_partition_unit",
        )
        partition_period = st.number_input(
            "Partition Period",
            min_value=1,
            step=1,
            key="form_partition_period",
        )

        is_create = st.checkbox("Create Partitions", key="form_is_create")

        cd_col1, cd_col2 = st.columns([1, 1])
        with cd_col1:
            create_drop_amount = st.number_input(
                "Create/Drop Interval",
                min_value=1,
                step=1,
                key="form_create_drop_amount",
            )
        with cd_col2:
            create_drop_unit = st.selectbox(
                "Create/Drop Interval Unit",
                options=CREATE_DROP_UNITS,
                key="form_create_drop_unit",
            )

        submitted = st.form_submit_button(
            "Create Partition Job",
            type="primary",
            use_container_width=False,
        )

    if not submitted:
        return

    # Guard against re-entrant handling within one script run.
    if st.session_state.create_in_flight:
        return
    st.session_state.create_in_flight = True
    try:
        _handle_create_submission(
            job_name=job_name,
            is_enabled=is_enabled,
            table_schema=table_schema,
            table_name=table_name,
            db_config=db_config,
            job_schedule=job_schedule,
            frequency_amount=frequency_amount,
            frequency_unit=frequency_unit,
            next_run_date=next_run_date,
            next_run_time_val=next_run_time_val,
            partition_unit=partition_unit,
            partition_period=partition_period,
            is_create=is_create,
            create_drop_amount=create_drop_amount,
            create_drop_unit=create_drop_unit,
        )
    finally:
        st.session_state.create_in_flight = False


def _handle_create_submission(
    *,
    job_name: Any,
    is_enabled: Any,
    table_schema: Any,
    table_name: Any,
    db_config: Any,
    job_schedule: Any,
    frequency_amount: Any,
    frequency_unit: Any,
    next_run_date: Any,
    next_run_time_val: Any,
    partition_unit: Any,
    partition_period: Any,
    is_create: Any,
    create_drop_amount: Any,
    create_drop_unit: Any,
) -> None:
    try:
        next_run_time = _combine_datetime(next_run_date, next_run_time_val)
        validated = validate_form_data(
            {
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
        )
    except ValidationError as exc:
        st.error(exc.message)
        return

    fingerprint = submission_fingerprint(validated)
    if fingerprint == st.session_state.last_created_fingerprint:
        st.info(
            "This exact configuration was already created in this session. "
            "Change a field before submitting again."
        )
        return

    # Snapshot job IDs before the create call.
    before_ids = _job_id_set(st.session_state.jobs)

    try:
        result = create_partition_job(validated)
    except DatabaseError as exc:
        st.error(exc.message)
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while creating partition job")
        st.error(
            "The database operation failed. Check the server logs for details."
        )
        return

    # Reached only after the database transaction committed successfully.
    st.session_state.last_created_fingerprint = fingerprint

    feedback: list[dict[str, str]] = [
        {
            "kind": "success",
            "text": "Partition job configuration created successfully.",
        }
    ]
    if result is not None:
        feedback.append(
            {"kind": "info", "text": f"Function result: `{result}`"}
        )

    # Refresh pgAgent list once after success, then rerun so the table updates.
    _load_jobs()
    after_ids = _job_id_set(st.session_state.jobs)
    new_ids = sorted(after_ids - before_ids, key=lambda x: (str(type(x)), x))

    if new_ids:
        if len(new_ids) == 1:
            feedback.append(
                {
                    "kind": "success",
                    "text": f"New pgAgent Job ID detected: **{new_ids[0]}**",
                }
            )
        else:
            ids_text = ", ".join(str(i) for i in new_ids)
            feedback.append(
                {
                    "kind": "success",
                    "text": f"New pgAgent Job IDs detected: **{ids_text}**",
                }
            )
    else:
        feedback.append(
            {
                "kind": "info",
                "text": (
                    "The configuration was inserted successfully. "
                    "The pgAgent job may be created later by the partition-job processor. "
                    "Use Refresh Jobs to check again."
                ),
            }
        )

    st.session_state.create_feedback = feedback
    st.rerun()


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

    # Load jobs once when the session starts — no polling.
    if not st.session_state.jobs_loaded:
        _load_jobs()

    _render_jobs_section()
    st.divider()
    _render_job_loader()
    st.divider()
    _render_create_form()


if __name__ == "__main__":
    main()
