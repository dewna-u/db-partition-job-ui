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
from scheduler_client import fetch_scheduler_status, notify_scheduler_refresh
from validators import ValidationError, validate_form_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

PAGE_TITLE = "Partition Job Management"
PAGE_SUBTITLE = (
    "Configure, monitor, and safely migrate PostgreSQL partition jobs."
)

NAV_CONVERT = "Convert Existing Job"
NAV_CREATE = "Create New Job"
NAV_CONFIGURED = "Configured Jobs"
NAV_HISTORY = "Execution History"
NAV_OPTIONS = [NAV_CONVERT, NAV_CREATE, NAV_CONFIGURED, NAV_HISTORY]
NAV_SIDEBAR_LABELS = {
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
    """Apply partition.ops visual system on top of Streamlit theme config."""
    st.markdown(
        """
<style>
    :root {
        --pj-bg: #f3f0e9;
        --pj-bg-soft: #eeece5;
        --pj-card: #fbfaf7;
        --pj-panel: #fbfaf7;
        --pj-input: #ffffff;
        --pj-border: #d9d5cc;
        --pj-line: #ddd9d0;
        --pj-text: #1c2730;
        --pj-ink: #1c2730;
        --pj-muted: #718078;
        --pj-faint: #8a958e;
        --pj-accent: #165dff;
        --pj-accent-soft: #e7edff;
        --pj-lime: #e5ff5c;
        --pj-sidebar: #202c34;
        --pj-sidebar-2: #293840;
        --pj-sidebar-border: #45535a;
        --pj-sidebar-text: #f3f1e9;
        --pj-sidebar-muted: #aab5b1;
        --pj-green: #1f8a64;
        --pj-green-bg: #e4f3ea;
        --pj-amber: #c76b2d;
        --pj-amber-bg: #fff0e5;
        --pj-success-bg: #e4f3ea;
        --pj-success: #1f8a64;
        --pj-warn-bg: #fff0e5;
        --pj-warn: #b8672d;
        --pj-danger-bg: #ffebe2;
        --pj-danger: #c76b2d;
        --pj-info-bg: #e7edff;
        --pj-info: #165dff;
        --pj-radius: 13px;
        --pj-radius-sm: 9px;
    }

    html, body, .stApp {
        background:
            radial-gradient(circle at 74% 0%, #dbe8ff 0, transparent 27rem),
            linear-gradient(135deg, #f3f0e9 0%, #eeece5 100%) !important;
        color: var(--pj-text);
        font-family: Arial, Helvetica, sans-serif;
    }
    .stApp > header { background: transparent !important; }
    [data-testid="stHeader"] { background: transparent !important; }
    [data-testid="stToolbar"] { display: none !important; }

    .block-container {
        max-width: 1500px !important;
        padding-top: 1.1rem !important;
        padding-bottom: 2rem !important;
        padding-left: 2rem !important;
        padding-right: 2rem !important;
    }

    /* ---- Sidebar (dark charcoal like template) ---- */
    section[data-testid="stSidebar"] {
        background: var(--pj-sidebar) !important;
        border-right: 1px solid #cfcac0 !important;
        min-width: 246px !important;
    }
    section[data-testid="stSidebar"] > div:first-child {
        background: var(--pj-sidebar) !important;
        padding: 1.4rem 0.85rem 1rem !important;
    }
    section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
    section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
        color: var(--pj-sidebar-muted) !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadio"] {
        gap: 0.25rem !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadio"] > label {
        display: none !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] {
        gap: 0.25rem !important;
        display: flex !important;
        flex-direction: column !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadio"] label {
        color: #b0bbb8 !important;
        font-size: 0.75rem !important;
        font-weight: 600 !important;
        padding: 0.7rem 0.65rem !important;
        border-radius: 9px !important;
        border: 1px solid transparent !important;
        background: transparent !important;
        margin: 0 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
        color: #fff !important;
        background: #2e3d45 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {
        color: #202c34 !important;
        background: var(--pj-lime) !important;
        border-color: var(--pj-lime) !important;
    }
    section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) p,
    section[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) span {
        color: #202c34 !important;
    }
    section[data-testid="stSidebar"] hr {
        border-color: var(--pj-sidebar-border) !important;
        margin: 0.85rem 0 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stExpander"] {
        background: #293840 !important;
        border: 1px solid #526067 !important;
        border-radius: 11px !important;
        color: #d5ddd9 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stExpander"] summary,
    section[data-testid="stSidebar"] [data-testid="stExpander"] p,
    section[data-testid="stSidebar"] [data-testid="stExpander"] span,
    section[data-testid="stSidebar"] [data-testid="stExpander"] label {
        color: #c5ceca !important;
    }
    section[data-testid="stSidebar"] .stButton > button {
        background: #293840 !important;
        color: #f3f1e9 !important;
        border: 1px solid #526067 !important;
        border-radius: 9px !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        box-shadow: none !important;
    }
    section[data-testid="stSidebar"] .stButton > button:hover {
        border-color: var(--pj-lime) !important;
        color: var(--pj-lime) !important;
        background: #2e3d45 !important;
    }
    section[data-testid="stSidebar"] .stAlert {
        background: #293840 !important;
        border: 1px solid #526067 !important;
        color: #d5ddd9 !important;
    }
    section[data-testid="stSidebar"] .stButton > button[kind="primary"],
    section[data-testid="stSidebar"] .stButton > button[data-testid="baseButton-primary"] {
        background: var(--pj-lime) !important;
        border-color: var(--pj-lime) !important;
        color: #202c34 !important;
        box-shadow: none !important;
    }

    /* ---- Typography ---- */
    h1 {
        font-size: 2.05rem !important;
        letter-spacing: -0.06em !important;
        line-height: 1.05 !important;
        font-weight: 800 !important;
        color: var(--pj-ink) !important;
        margin-bottom: 0.15rem !important;
    }
    h2, h3 {
        letter-spacing: -0.035em !important;
        color: var(--pj-ink) !important;
    }
    div[data-testid="stCaptionContainer"] {
        color: var(--pj-muted) !important;
        font-size: 0.82rem !important;
    }

    /* ---- Inputs / controls ---- */
    label[data-testid="stWidgetLabel"] p,
    .stMarkdown label {
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        color: #5f6d68 !important;
    }
    .stTextInput input, .stNumberInput input, .stTextArea textarea,
    .stSelectbox [data-baseweb="select"] > div,
    .stDateInput input, .stTimeInput input {
        background: #fff !important;
        border: 1px solid #d1cdc4 !important;
        border-radius: 8px !important;
        color: var(--pj-ink) !important;
        font-size: 0.72rem !important;
        min-height: 34px !important;
        box-shadow: none !important;
    }
    .stTextArea textarea {
        min-height: 90px !important;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace !important;
        font-size: 0.72rem !important;
    }
    .stTextInput input:focus, .stNumberInput input:focus, .stTextArea textarea:focus {
        border-color: var(--pj-accent) !important;
        box-shadow: 0 0 0 3px #165dff33 !important;
    }
    div[data-baseweb="radio"] > div {
        gap: 0.35rem !important;
        background: #fff !important;
        border: 1px solid #d1cdc4 !important;
        border-radius: 8px !important;
        padding: 0.2rem !important;
    }
    div[data-baseweb="radio"] label {
        font-weight: 700 !important;
        font-size: 0.72rem !important;
        border-radius: 6px !important;
        padding: 0.35rem 0.75rem !important;
    }
    .stCheckbox label p {
        font-size: 0.78rem !important;
        color: #5f6d68 !important;
    }

    /* ---- Buttons (secondary / primary / run / danger) ---- */
    .stButton > button {
        border-radius: 9px !important;
        border: 1px solid #c9c6bd !important;
        background: #fbfaf7 !important;
        color: #3f4e4a !important;
        font-size: 0.72rem !important;
        font-weight: 700 !important;
        padding: 0.62rem 0.95rem !important;
        min-height: 34px !important;
        line-height: 1.1 !important;
        transition: border-color 0.18s ease, background 0.18s ease, box-shadow 0.18s ease !important;
        box-shadow: none !important;
    }
    .stButton > button:hover {
        border-color: #7f8d87 !important;
        background: #fff !important;
        color: var(--pj-ink) !important;
    }
    .stButton > button:disabled,
    .stButton > button[disabled] {
        opacity: 0.45 !important;
        cursor: not-allowed !important;
        background: #efece4 !important;
        color: #8a958e !important;
        border-color: #d9d5cc !important;
        box-shadow: none !important;
    }
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="baseButton-primary"] {
        background: var(--pj-accent) !important;
        border-color: var(--pj-accent) !important;
        color: #fff !important;
        box-shadow: 0 8px 20px #165dff2b !important;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid="baseButton-primary"]:hover {
        background: #0f4fd6 !important;
        border-color: #0f4fd6 !important;
        color: #fff !important;
    }
    .stButton > button[kind="primary"]:disabled,
    .stButton > button[data-testid="baseButton-primary"]:disabled {
        background: #9db6f0 !important;
        border-color: #9db6f0 !important;
        color: #fff !important;
        box-shadow: none !important;
        opacity: 0.7 !important;
    }
    /* Green Run now (template .button.run) */
    div[data-testid="element-container"]:has(.pj-btn-run) + div[data-testid="element-container"] button,
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.pj-btn-run) button,
    .pj-run-zone button {
        background: #1f8a64 !important;
        border-color: #1f8a64 !important;
        color: #fff !important;
        box-shadow: 0 8px 20px #1f8a642b !important;
    }
    div[data-testid="element-container"]:has(.pj-btn-run) + div[data-testid="element-container"] button:hover,
    .pj-run-zone button:hover {
        background: #187553 !important;
        border-color: #187553 !important;
        color: #fff !important;
    }
    /* Amber DROP run */
    div[data-testid="element-container"]:has(.pj-btn-danger) + div[data-testid="element-container"] button,
    .pj-danger-zone button {
        background: #c76b2d !important;
        border-color: #c76b2d !important;
        color: #fff !important;
        box-shadow: 0 8px 20px #c76b2d2b !important;
    }
    div[data-testid="element-container"]:has(.pj-btn-danger) + div[data-testid="element-container"] button:hover,
    .pj-danger-zone button:hover {
        background: #a85720 !important;
        border-color: #a85720 !important;
        color: #fff !important;
    }
    .pj-btn-marker { display: none !important; }

    /* ---- Metrics as template metric cards ---- */
    [data-testid="stMetric"] {
        background: var(--pj-card);
        border: 1px solid var(--pj-line);
        border-radius: 12px;
        padding: 1.05rem 1.1rem 0.95rem;
        box-shadow: 0 4px 15px #4c554c08;
    }
    [data-testid="stMetricLabel"] {
        color: #7e8982 !important;
        font-size: 0.68rem !important;
        font-weight: 700 !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.65rem !important;
        letter-spacing: -0.06em !important;
        color: var(--pj-ink) !important;
        font-weight: 800 !important;
    }

    /* ---- Dataframes / tables ---- */
    [data-testid="stDataFrame"],
    [data-testid="stDataFrameResizable"] {
        border: 1px solid var(--pj-line) !important;
        border-radius: 12px !important;
        overflow: hidden !important;
        background: var(--pj-card) !important;
        box-shadow: 0 7px 22px #4c554c09;
    }
    [data-testid="stDataFrame"] thead tr th,
    [data-testid="stDataFrameResizable"] thead tr th {
        background: #f7f5f0 !important;
        color: #98a19b !important;
        text-transform: uppercase !important;
        letter-spacing: 0.1em !important;
        font-size: 0.58rem !important;
        font-weight: 800 !important;
        border-bottom: 1px solid var(--pj-line) !important;
    }
    [data-testid="stDataFrame"] tbody tr:hover td,
    [data-testid="stDataFrameResizable"] tbody tr:hover td {
        background: #f0f3f6 !important;
    }

    /* ---- Expanders / alerts ---- */
    [data-testid="stExpander"] {
        border: 1px solid var(--pj-line) !important;
        border-radius: 12px !important;
        background: var(--pj-card) !important;
        box-shadow: 0 4px 15px #4c554c08;
    }
    div[data-testid="stAlert"] {
        border-radius: 12px !important;
        padding: 0.75rem 0.95rem !important;
    }
    div[data-testid="stAlert"][kind="success"],
    .stSuccess, div:has(> [data-testid="stNotificationContentSuccess"]) {
        background: #e8f1e5 !important;
        border: 1px solid #c9d4c6 !important;
        color: #22543f !important;
    }
    div[data-testid="stAlert"][kind="info"] {
        background: #eef3ff !important;
        border: 1px solid #cbd7ef !important;
        color: #40526f !important;
    }
    div[data-testid="stAlert"][kind="warning"] {
        background: #fff0e5 !important;
        border: 1px solid #e5d2b8 !important;
        color: #8a4b16 !important;
    }
    div[data-testid="stAlert"][kind="error"] {
        background: #fff0ee !important;
        border: 1px solid #e5c4bc !important;
        color: #8a2e26 !important;
    }

    .pj-topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
        padding: 0.35rem 0 0.85rem;
        margin: 0 0 0.35rem;
        border-bottom: 1px solid var(--pj-line);
    }
    .pj-heading-row {
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        gap: 1.2rem;
        margin-bottom: 0.35rem;
    }
    .pj-heading-copy { flex: 1; min-width: 0; }
    .pj-detail-hero {
        display: flex;
        align-items: center;
        gap: 0.7rem;
        margin: 0.35rem 0 0.75rem;
        padding: 0.8rem 0.85rem;
        border: 1px solid #d9e1f5;
        border-radius: 10px;
        background: #f1f5ff;
    }
    .pj-detail-icon {
        display: grid;
        place-items: center;
        width: 35px;
        height: 35px;
        border-radius: 9px;
        background: #165dff;
        color: #fff;
        font-weight: 800;
        font-size: 0.75rem;
        flex-shrink: 0;
    }
    .pj-detail-hero strong {
        display: block;
        font-size: 0.8rem;
        color: var(--pj-ink);
    }
    .pj-detail-hero span {
        display: block;
        margin-top: 0.2rem;
        color: #718078;
        font-size: 0.65rem;
    }
    .pj-op-pill {
        display: inline-block;
        padding: 0.25rem 0.45rem;
        border-radius: 5px;
        font-size: 0.58rem;
        font-weight: 800;
    }
    .pj-op-pill.create { color: #165dff; background: #e7edff; }
    .pj-op-pill.drop { color: #c76b2d; background: #ffebe2; }
    [data-testid="stVerticalBlockBorderWrapper"] {
        border: 1px solid var(--pj-line) !important;
        border-radius: 13px !important;
        background: var(--pj-card) !important;
        box-shadow: 0 7px 22px #4c554c09 !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"] > div {
        background: transparent !important;
    }
    .pj-toolbar-note {
        color: #63716b;
        font-size: 0.68rem;
        margin: 0.15rem 0 0.55rem;
    }

    /* ---- Custom chrome ---- */
    .pj-brand {
        display: flex;
        align-items: center;
        gap: 0.7rem;
        padding: 0 0.35rem 1.1rem;
        border-bottom: 1px solid var(--pj-sidebar-border);
        margin-bottom: 0.85rem;
    }
    .pj-brand-icon {
        display: grid;
        place-items: center;
        width: 34px;
        height: 34px;
        border-radius: 10px;
        background: var(--pj-lime);
        color: #202c34;
        font-weight: 900;
        font-size: 0.85rem;
        transform: rotate(-6deg);
        flex-shrink: 0;
    }
    .pj-brand strong {
        display: block;
        font-size: 1rem;
        letter-spacing: -0.045em;
        color: #f3f1e9 !important;
        font-weight: 800;
    }
    .pj-brand strong span { color: var(--pj-lime); }
    .pj-brand small {
        display: block;
        margin-top: 0.2rem;
        color: #aab5b1 !important;
        font-size: 0.62rem;
        letter-spacing: 0.02em;
    }
    .pj-workspace {
        display: flex;
        align-items: center;
        gap: 0.55rem;
        margin: 0.2rem 0 1rem;
        padding: 0.65rem 0.7rem;
        border: 1px solid #526067;
        border-radius: 11px;
        background: #293840;
    }
    .pj-workspace-avatar {
        display: grid;
        place-items: center;
        width: 28px;
        height: 28px;
        border-radius: 8px;
        background: var(--pj-lime);
        color: #202c34;
        font-weight: 800;
        font-size: 0.58rem;
        flex-shrink: 0;
    }
    .pj-workspace span {
        display: block;
        font-size: 0.62rem;
        color: #a5b0ad !important;
    }
    .pj-workspace strong {
        display: block;
        margin-top: 0.1rem;
        font-size: 0.75rem;
        color: #fff !important;
    }
    .pj-nav-kicker {
        padding: 0 0.4rem;
        margin: 0.15rem 0 0.45rem;
        font-size: 0.62rem;
        letter-spacing: 0.13em;
        text-transform: uppercase;
        color: #8e9b98 !important;
        font-weight: 700;
    }
    .pj-connection {
        display: flex;
        align-items: center;
        gap: 0.55rem;
        padding: 0.85rem 0.35rem;
        margin-top: 0.5rem;
        border-top: 1px solid var(--pj-sidebar-border);
        border-bottom: 1px solid var(--pj-sidebar-border);
    }
    .pj-live-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: #7de0ad;
        box-shadow: 0 0 0 4px #7de0ad22;
        flex-shrink: 0;
    }
    .pj-connection strong {
        display: block;
        font-size: 0.7rem;
        color: #f3f1e9 !important;
    }
    .pj-connection span {
        display: block;
        margin-top: 0.15rem;
        font-size: 0.62rem;
        color: #a0aca8 !important;
    }

    .pj-eyebrow {
        display: flex;
        align-items: center;
        gap: 0.45rem;
        margin-bottom: 0.55rem;
        color: #718078;
        font-size: 0.62rem;
        letter-spacing: 0.15em;
        font-weight: 800;
        text-transform: uppercase;
    }
    .pj-eyebrow-dot {
        width: 7px;
        height: 7px;
        border-radius: 2px;
        background: var(--pj-accent);
        transform: rotate(45deg);
    }
    .pj-subtitle {
        color: var(--pj-muted);
        font-size: 0.88rem;
        margin: 0.35rem 0 0.85rem 0;
        max-width: 62ch;
    }
    .pj-header-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin: 0 0 1rem 0;
    }
    .pj-breadcrumb {
        display: flex;
        gap: 0.55rem;
        align-items: center;
        font-size: 0.72rem;
        color: #86908a;
        margin: 0 0 1.1rem 0;
    }
    .pj-breadcrumb strong {
        color: var(--pj-ink);
        font-weight: 700;
    }

    .pj-status-strip {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        flex-wrap: wrap;
        padding: 0.85rem 1.05rem;
        margin: 0 0 1.05rem 0;
        border: 1px solid #c9d4c6;
        border-radius: 12px;
        background: #e8f1e5;
    }
    .pj-status-strip.warn {
        border-color: #e5d2b8;
        background: #fff6ea;
    }
    .pj-status-strip.fail {
        border-color: #e5c4bc;
        background: #fff0ee;
    }
    .pj-status-message {
        display: flex;
        align-items: center;
        gap: 0.65rem;
    }
    .pj-status-icon {
        display: grid;
        place-items: center;
        width: 25px;
        height: 25px;
        border-radius: 50%;
        background: var(--pj-green);
        color: #fff;
        font-size: 0.75rem;
        font-weight: 800;
        flex-shrink: 0;
    }
    .pj-status-strip.warn .pj-status-icon { background: var(--pj-amber); }
    .pj-status-strip.fail .pj-status-icon { background: #c4473a; }
    .pj-status-message strong {
        display: block;
        font-size: 0.78rem;
        color: #22543f;
    }
    .pj-status-strip.warn .pj-status-message strong { color: #8a4b16; }
    .pj-status-strip.fail .pj-status-message strong { color: #8a2e26; }
    .pj-status-message span {
        display: block;
        margin-top: 0.15rem;
        font-size: 0.72rem;
        color: #5f7567;
    }
    .pj-strip-meta {
        display: flex;
        align-items: center;
        gap: 0.9rem;
        flex-wrap: wrap;
        color: #5c7266;
        font-size: 0.68rem;
    }
    .pj-mini-dot {
        display: inline-block;
        width: 6px;
        height: 6px;
        border-radius: 50%;
        margin-right: 0.35rem;
        vertical-align: middle;
    }
    .pj-mini-dot.blue { background: var(--pj-accent); }
    .pj-mini-dot.green { background: var(--pj-green); }
    .pj-mini-dot.amber { background: var(--pj-amber); }

    .pj-card {
        border: 1px solid var(--pj-line);
        border-radius: var(--pj-radius);
        background: var(--pj-card);
        box-shadow: 0 7px 22px #4c554c09;
        padding: 1.15rem 1.2rem;
        margin: 0 0 1rem 0;
    }
    .pj-card-eyebrow {
        color: #86928a;
        font-size: 0.58rem;
        letter-spacing: 0.15em;
        font-weight: 800;
        text-transform: uppercase;
        margin: 0 0 0.25rem 0;
    }
    .pj-card h2, .pj-card-title {
        margin: 0;
        font-size: 1.05rem;
        letter-spacing: -0.035em;
        font-weight: 800;
        color: var(--pj-ink);
    }

    .pj-step-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin: 0.25rem 0 0.95rem 0;
    }
    .pj-step {
        background: #fff;
        color: var(--pj-muted);
        border: 1px solid #d1cdc4;
        border-radius: 999px;
        padding: 0.28rem 0.75rem;
        font-size: 0.72rem;
        font-weight: 700;
    }
    .pj-step-active {
        background: var(--pj-accent-soft);
        color: var(--pj-accent);
        border-color: #b7c9f5;
    }
    .pj-step-done {
        background: var(--pj-green-bg);
        color: var(--pj-green);
        border-color: #b7d9c7;
    }

    .pj-badge {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        border-radius: 999px;
        padding: 0.28rem 0.55rem;
        font-size: 0.65rem;
        font-weight: 800;
        margin-right: 0.25rem;
        white-space: nowrap;
    }
    .pj-badge::before {
        content: "";
        width: 5px;
        height: 5px;
        border-radius: 50%;
        background: currentColor;
    }
    .pj-badge-ok { background: var(--pj-success-bg); color: var(--pj-success); }
    .pj-badge-warn { background: var(--pj-warn-bg); color: var(--pj-warn); }
    .pj-badge-fail { background: #ffe8e5; color: #c4473a; }
    .pj-badge-info { background: var(--pj-info-bg); color: var(--pj-info); }
    .pj-badge-mute { background: #efece4; color: #6d7973; }
    .pj-badge-drop { background: var(--pj-amber-bg); color: var(--pj-amber); }

    .pj-panel, .pj-preview {
        border: 1px solid var(--pj-line);
        background: var(--pj-card);
        border-radius: 12px;
        padding: 0.95rem 1.05rem;
        margin: 0.55rem 0 0.95rem 0;
        box-shadow: 0 4px 15px #4c554c08;
    }
    .pj-preview {
        border-color: #cbd7ef;
        background: #eef3ff;
    }
    .pj-panel-title {
        font-weight: 800;
        color: var(--pj-ink);
        margin-bottom: 0.45rem;
        letter-spacing: -0.02em;
    }
    .pj-kv {
        font-size: 0.86rem;
        line-height: 1.55;
        color: var(--pj-text);
    }
    .pj-kv code {
        background: #fff;
        border: 1px solid #d1cdc4;
        padding: 0.05rem 0.3rem;
        border-radius: 4px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.78rem;
    }
    .pj-section-label {
        font-size: 0.62rem;
        font-weight: 800;
        text-transform: uppercase;
        letter-spacing: 0.13em;
        color: #86928a;
        margin: 0.85rem 0 0.4rem 0;
    }
    .pj-danger {
        border: 1px solid #e5c4a8;
        background: var(--pj-amber-bg);
        color: #8a4b16;
        border-radius: 10px;
        padding: 0.65rem 0.85rem;
        margin: 0.45rem 0 0.75rem 0;
        font-weight: 700;
        font-size: 0.84rem;
    }
    .pj-insight {
        position: relative;
        overflow: hidden;
        border-radius: 13px;
        border: 1px solid #202c34;
        background: #202c34;
        color: #f5f3ea;
        padding: 1.25rem 1.2rem;
        margin: 0.75rem 0 1rem 0;
    }
    .pj-insight::after {
        content: "";
        position: absolute;
        right: -40px;
        top: -55px;
        width: 160px;
        height: 160px;
        border-radius: 50%;
        background: var(--pj-lime);
        opacity: 0.85;
    }
    .pj-insight > * { position: relative; z-index: 1; }
    .pj-insight .pj-card-eyebrow { color: #aebbb4; margin-top: 0.35rem; }
    .pj-insight h3 {
        margin: 0.35rem 0 0.45rem;
        font-size: 1.05rem;
        letter-spacing: -0.04em;
        color: #f5f3ea !important;
        max-width: 28ch;
    }
    .pj-insight p {
        margin: 0;
        color: #b5c0bb;
        font-size: 0.78rem;
        line-height: 1.55;
        max-width: 46ch;
    }
    .pj-footer {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        flex-wrap: wrap;
        padding: 1.1rem 0 0;
        color: #8b958f;
        font-size: 0.68rem;
    }
    .pj-footer strong { color: #5e6d66; font-weight: 800; }

    @media (max-width: 900px) {
        .block-container {
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }
    }
    @media (prefers-reduced-motion: reduce) {
        * { transition: none !important; animation: none !important; }
    }
    :focus-visible {
        outline: 3px solid #165dff !important;
        outline-offset: 2px !important;
    }
</style>
""",
        unsafe_allow_html=True,
    )


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
    st.markdown("#### Realtime scheduler backend")
    st.caption(
        "A dedicated long-running process preloads upcoming jobs from PostgreSQL, "
        "holds temporary timers in memory, and triggers each job at its "
        "next_run_time. Streamlit is configuration-only."
    )

    ok, status, message = _probe_scheduler()
    if ok and status:
        active = bool(status.get("scheduler_active"))
        st.markdown(
            f'{_badge("Backend active" if active else "Backend idle", "ok" if active else "warn")}',
            unsafe_allow_html=True,
        )
        st.markdown(
            "Upcoming jobs: `{count}`  \n"
            "Next job: `{job}` @ `{when}`  \n"
            "Last refresh: `{refresh}`  \n"
            "Last execution: job `{last_job}` → `{last_result}`".format(
                count=status.get("upcoming_job_count"),
                job=status.get("next_job_id"),
                when=status.get("next_expected_run_time"),
                refresh=status.get("last_refresh_result"),
                last_job=status.get("last_execution_job_id"),
                last_result=status.get("last_execution_result"),
            )
        )
    else:
        st.markdown(
            f'{_badge("Backend unreachable", "warn")}',
            unsafe_allow_html=True,
        )
        st.caption(message)

    st.info(
        "Legacy polling runners (`run_partition_create_jobs` / "
        "`run_partition_drop_jobs`) remain in the database for rollback only. "
        "On the NEW realtime database they must stay disabled while this "
        "backend is enabled."
    )


def _header_status_badges() -> None:
    readiness = st.session_state.database_readiness

    if st.session_state.database_readiness_error:
        db_badge = _badge("Database error", "fail")
        strip_class = "fail"
        strip_title = "Database readiness check failed"
        strip_detail = "Fix the readiness error before relying on write actions."
        strip_icon = "!"
    elif not st.session_state.database_readiness_loaded:
        db_badge = _badge("Database not checked", "mute")
        strip_class = "warn"
        strip_title = "Database readiness has not been checked yet"
        strip_detail = "The UI will check privileges on startup."
        strip_icon = "·"
    elif readiness and readiness.get("insert_function_execute"):
        db_badge = _badge("Database ready", "ok")
        strip_class = ""
        strip_title = "Everything is running smoothly"
        strip_detail = (
            "Configuration writes are available and the realtime scheduler "
            "panel is visible in the sidebar."
        )
        strip_icon = "✓"
    elif readiness:
        db_badge = _badge("Database limited", "warn")
        strip_class = "warn"
        strip_title = "Database access is limited"
        strip_detail = "Some privileges are missing. Review Database readiness in the sidebar."
        strip_icon = "!"
    else:
        db_badge = _badge("Database unknown", "mute")
        strip_class = "warn"
        strip_title = "Database status is unknown"
        strip_detail = "Readiness information is not available."
        strip_icon = "·"

    st.markdown(
        f'<div class="pj-header-meta">{db_badge}'
        f'{_badge("Realtime scheduler", "info")}</div>',
        unsafe_allow_html=True,
    )

    ok, status, _message = _probe_scheduler()
    meta_bits: list[str] = []
    if ok and status:
        active = bool(status.get("scheduler_active"))
        meta_bits.append(
            f'<span><span class="pj-mini-dot {"green" if active else "amber"}"></span>'
            f'{"Scheduler online" if active else "Scheduler idle"}</span>'
        )
        upcoming = status.get("upcoming_job_count")
        if upcoming is not None:
            meta_bits.append(
                f'<span><span class="pj-mini-dot blue"></span>{upcoming} upcoming</span>'
            )
        refresh = status.get("last_refresh_result")
        if refresh:
            meta_bits.append(f"<span class=\"mono\">{refresh}</span>")
    else:
        meta_bits.append(
            '<span><span class="pj-mini-dot amber"></span>Scheduler unreachable</span>'
        )

    strip_cls = f'pj-status-strip {strip_class}'.strip()
    st.markdown(
        f'<div class="{strip_cls}">'
        f'<div class="pj-status-message">'
        f'<div class="pj-status-icon">{strip_icon}</div>'
        f"<div><strong>{strip_title}</strong>"
        f"<span>{strip_detail}</span></div></div>"
        f'<div class="pj-strip-meta">{"".join(meta_bits)}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_header(active_view: str) -> None:
    st.markdown(
        f'<div class="pj-topbar"><div class="pj-breadcrumb"><span>Workspace</span>'
        f'<span>/</span><strong>{NAV_SIDEBAR_LABELS.get(active_view, active_view)}'
        f"</strong></div></div>",
        unsafe_allow_html=True,
    )

    title_col, action_col = st.columns([3.2, 1.35])
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
        btn_a, btn_b = st.columns(2)
        with btn_a:
            if st.button("↻ Refresh", key="hdr_refresh", width="stretch", help="Re-check readiness and scheduler"):
                st.session_state.pop("_scheduler_status_bundle", None)
                _load_into_state(
                    "database_readiness",
                    get_database_readiness,
                    spinner_text="Checking database readiness...",
                )
                st.session_state["_scheduler_status_bundle"] = fetch_scheduler_status()
                st.rerun()
        with btn_b:
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
        "<div><strong>partition<span>.ops</span></strong>"
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
        detail = str(status.get("last_refresh_result") or "Heartbeat ok")
    elif ok and status:
        title = "Scheduler idle"
        detail = str(status.get("last_refresh_result") or "Backend reachable")
    else:
        title = "Scheduler unreachable"
        detail = _shorten(message, 48)
    st.markdown(
        f'<div class="pj-connection"><div class="pj-live-dot"></div>'
        f"<div><strong>{title}</strong><span>{detail}</span></div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="pj-workspace" style="margin-top:0.85rem;margin-bottom:0">'
        '<div class="pj-workspace-avatar">DB</div>'
        "<div><span>Operator</span><strong>Streamlit UI</strong></div></div>",
        unsafe_allow_html=True,
    )


def _render_page_footer() -> None:
    ok, status, _message = _probe_scheduler()
    heartbeat = "offline"
    if ok and status:
        heartbeat = str(status.get("last_refresh_result") or "ok")
    st.markdown(
        '<div class="pj-footer">'
        "<strong>partition.ops</strong>"
        f"<span>Scheduler heartbeat · {heartbeat}</span>"
        "<span>All systems operational</span>"
        "</div>",
        unsafe_allow_html=True,
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
    # Fresh probe each run; shared by header strip, sidebar chip, and panel.
    st.session_state["_scheduler_status_bundle"] = fetch_scheduler_status()

    # Lightweight status only — heavy job/log lists load when their view opens.
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
        st.markdown('<div class="pj-nav-kicker second">System</div>', unsafe_allow_html=True)
        _render_sidebar_scheduler_chip()
        with st.expander("Database readiness", expanded=False):
            _render_readiness_panel()
        with st.expander("Realtime scheduler", expanded=False):
            _render_scheduler_panel()

    _render_header(view)

    # Single-view navigation: only the active section renders widgets/queries.
    if view == NAV_CONVERT:
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