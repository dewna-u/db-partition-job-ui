"""Overview dashboard and application shell rendering (presentation only)."""

from __future__ import annotations

from typing import Any, Callable, Optional

import streamlit as st

from dashboard_metrics import (
    display_status,
    format_age,
    job_counts,
    jobs_to_csv,
    next_execution,
    next_run_label,
    operation_label,
    scheduler_uptime,
    success_rate,
    system_insights,
    target_table,
)


def tone_to_badge(tone: str) -> str:
    return {
        "green": "ok",
        "blue": "info",
        "amber": "warn",
        "red": "fail",
        "mute": "mute",
    }.get(tone, "mute")


def render_top_nav(
    *,
    nav_options: list[str],
    nav_labels: dict[str, str],
    active_view: str,
    nav_key: str = "main_nav",
) -> None:
    try:
        import streamlit.components.v1 as components

        components.html(
            """
            <div style="display:flex;align-items:center;gap:10px;font-family:Arial,sans-serif;">
              <button id="pj-reopen" style="appearance:none;border:1px solid #c9c6bd;background:#fff;
                color:#1c2730;border-radius:9px;padding:8px 12px;font-size:12px;font-weight:800;
                cursor:pointer;box-shadow:0 4px 12px #202c3412;">☰ Menu</button>
              <span style="color:#5f6d68;font-size:12px;font-weight:700;">
                Open the sidebar, or switch pages with the buttons below.
              </span>
            </div>
            <script>
            const btn = document.getElementById("pj-reopen");
            btn && btn.addEventListener("click", () => {
              const doc = window.parent.document;
              const selectors = [
                '[data-testid="stSidebarCollapsedControl"] button',
                '[data-testid="stSidebarCollapsedControl"]',
                '[data-testid="collapsedControl"] button',
                '[data-testid="collapsedControl"]',
                'button[kind="headerNoPadding"]',
                'button[kind="header"]'
              ];
              for (const sel of selectors) {
                const el = doc.querySelector(sel);
                if (el) { el.click(); return; }
              }
            });
            </script>
            """,
            height=46,
        )
    except Exception:  # noqa: BLE001
        st.caption("Use the top-left sidebar control to reopen navigation.")

    cols = st.columns(len(nav_options))
    for index, option in enumerate(nav_options):
        with cols[index]:
            label = nav_labels.get(option, option)
            is_active = option == active_view
            if st.button(
                label,
                key=f"topnav_{index}",
                type="primary" if is_active else "secondary",
                width="stretch",
            ):
                if option != active_view:
                    st.session_state[nav_key] = option
                    st.rerun()


def render_health_banner(
    *,
    badge_fn: Callable[..., str],
    readiness: Any,
    readiness_error: Any,
    jobs: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    scheduler_ok: bool,
    status: Optional[dict[str, Any]],
) -> None:
    counts = job_counts(jobs)
    rate = success_rate(logs)
    fails = sum(
        1
        for row in logs
        if str(row.get("last_run_status") or "").upper()
        in {"FAIL", "MANUAL_FAIL", "ERROR", "FAILED", "FAILED_CONNECTION"}
    )

    if readiness_error:
        strip_class, strip_title, strip_icon = (
            "fail",
            "Database readiness check failed",
            "!",
        )
        strip_detail = "Fix the readiness error before relying on write actions."
    elif not scheduler_ok:
        strip_class, strip_title, strip_icon = (
            "warn",
            "Scheduler backend unavailable",
            "!",
        )
        strip_detail = (
            "Job configuration is still available, but realtime scheduler status "
            "cannot currently be retrieved."
        )
    elif fails:
        strip_class, strip_title, strip_icon = ("warn", "Some jobs require attention", "!")
        strip_detail = (
            f"{fails} failed execution(s) appear in recent history. "
            "Review Execution History before the next scheduled run."
        )
    elif readiness and readiness.get("insert_function_execute"):
        strip_class, strip_title, strip_icon = ("", "Everything is running smoothly", "OK")
        strip_detail = (
            "All scheduled jobs are healthy and the realtime scheduler is responding."
            if scheduler_ok and status and status.get("scheduler_active")
            else "Configuration is ready. Monitor the scheduler heartbeat for live timing."
        )
    else:
        strip_class, strip_title, strip_icon = ("warn", "Database access is limited", "!")
        strip_detail = "Some privileges are missing. Review Database readiness in the sidebar."

    meta = [
        f'<span><span class="pj-mini-dot blue"></span>{counts["enabled"]} scheduled</span>'
    ]
    if rate["available"]:
        meta.append(
            f'<span><span class="pj-mini-dot green"></span>{rate["label"]} success</span>'
        )
    if scheduler_ok and status and status.get("scheduler_active"):
        meta.append('<span><span class="pj-mini-dot green"></span>Scheduler online</span>')
    elif scheduler_ok:
        meta.append('<span><span class="pj-mini-dot amber"></span>Scheduler idle</span>')
    else:
        meta.append('<span><span class="pj-mini-dot amber"></span>Scheduler offline</span>')

    strip_cls = f"pj-status-strip {strip_class}".strip()
    st.markdown(
        f'<div class="{strip_cls}">'
        f'<div class="pj-status-message"><div class="pj-status-icon">{strip_icon}</div>'
        f"<div><strong>{strip_title}</strong><span>{strip_detail}</span></div></div>"
        f'<div class="pj-strip-meta">{"".join(meta)}</div></div>',
        unsafe_allow_html=True,
    )


def render_metric_cards(
    jobs: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    *,
    scheduler_ok: bool,
    status: Optional[dict[str, Any]],
) -> None:
    counts = job_counts(jobs)
    nxt = next_execution(jobs)
    rate = success_rate(logs)
    uptime = scheduler_uptime(status if scheduler_ok else None)
    fails = sum(
        1
        for row in logs
        if str(row.get("last_run_status") or "").upper()
        in {"FAIL", "MANUAL_FAIL", "ERROR", "FAILED", "FAILED_CONNECTION"}
    )
    card4_label = "Scheduler uptime"
    card4_value = uptime["label"]
    card4_detail = uptime["detail"]
    if not uptime["available"] and fails:
        card4_label = "Failed executions"
        card4_value = str(fails)
        card4_detail = "In loaded history"

    nxt_value = nxt["countdown"] if nxt["available"] else "-"
    st.markdown(
        (
            '<div class="pj-kpi-grid">'
            '<div class="pj-kpi"><div class="pj-kpi-label">Configured jobs</div>'
            f'<strong class="pj-kpi-value">{counts["total"]}</strong>'
            f'<span class="pj-kpi-detail">{counts["enabled"]} enabled / '
            f'{counts["disabled"]} disabled</span></div>'
            '<div class="pj-kpi"><div class="pj-kpi-label">Next execution</div>'
            f'<strong class="pj-kpi-value">{nxt_value}</strong>'
            f'<span class="pj-kpi-detail">{nxt["detail"]}</span></div>'
            '<div class="pj-kpi"><div class="pj-kpi-label">Success rate</div>'
            f'<strong class="pj-kpi-value">{rate["label"]}</strong>'
            f'<span class="pj-kpi-detail">{rate["detail"]}</span></div>'
            f'<div class="pj-kpi"><div class="pj-kpi-label">{card4_label}</div>'
            f'<strong class="pj-kpi-value">{card4_value}</strong>'
            f'<span class="pj-kpi-detail">{card4_detail}</span></div>'
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_overview(
    *,
    badge_fn: Callable[..., str],
    ensure_loaded_fn: Callable[..., None],
    load_jobs_fn: Callable[[], list],
    load_logs_fn: Callable[..., list],
    probe_scheduler_fn: Callable[[], tuple[bool, Optional[dict], str]],
    render_db_error_fn: Callable[[str], None],
    render_manual_run_fn: Callable[[dict], None],
    nav_configured: str,
    nav_history: str,
    get_partition_jobs_error: Any,
    get_partition_logs_error: Any,
) -> None:
    if not st.session_state.get("partition_jobs_loaded"):
        ensure_loaded_fn(
            "partition_jobs",
            load_jobs_fn,
            spinner_text="Loading configured jobs...",
        )
    if not st.session_state.get("partition_job_logs_loaded"):
        ensure_loaded_fn(
            "partition_job_logs",
            load_logs_fn,
            200,
            spinner_text="Loading execution history...",
        )

    jobs = st.session_state.partition_jobs or []
    logs = st.session_state.partition_job_logs or []
    if get_partition_jobs_error:
        render_db_error_fn(get_partition_jobs_error)
    if get_partition_logs_error:
        st.caption("Execution history could not be loaded for success-rate metrics.")

    ok, status, message = probe_scheduler_fn()
    render_metric_cards(jobs, logs, scheduler_ok=ok, status=status)

    left, right = st.columns([1.55, 0.95])
    with left:
        with st.container(border=True):
            head_l, head_r = st.columns([3, 1])
            with head_l:
                st.markdown(
                    '<div class="pj-card-eyebrow">Live queue</div>'
                    '<div class="pj-card-title">Configured jobs</div>',
                    unsafe_allow_html=True,
                )
            with head_r:
                if st.button("View all", key="overview_view_all", width="stretch"):
                    st.session_state.main_nav = nav_configured
                    st.rerun()

            st.text_input("Filter jobs...", key="overview_search")
            f1, f2 = st.columns(2)
            with f1:
                st.selectbox(
                    "Type", options=["All", "CREATE", "DROP"], key="overview_op_filter"
                )
            with f2:
                st.selectbox(
                    "State",
                    options=["All", "Enabled", "Disabled", "Failed / Needs review"],
                    key="overview_state_filter",
                )

            search = (st.session_state.get("overview_search") or "").strip().lower()
            op_filter = st.session_state.get("overview_op_filter", "All")
            state_filter = st.session_state.get("overview_state_filter", "All")
            filtered: list[dict[str, Any]] = []
            for job in jobs:
                op = operation_label(job)
                label, _tone = display_status(job)
                if op_filter != "All" and op != op_filter:
                    continue
                if state_filter == "Enabled" and not job.get("is_enabled"):
                    continue
                if state_filter == "Disabled" and job.get("is_enabled"):
                    continue
                if state_filter == "Failed / Needs review" and label not in {
                    "Failed",
                    "Needs review",
                }:
                    continue
                if search:
                    hay = " ".join(
                        str(part or "")
                        for part in (
                            job.get("job_id"),
                            job.get("job_name"),
                            target_table(job),
                        )
                    ).lower()
                    if search not in hay:
                        continue
                filtered.append(job)

            rows = filtered[:12]
            if rows:
                st.dataframe(
                    [
                        {
                            "Job": job.get("job_name"),
                            "Target": target_table(job),
                            "Schedule": job.get("job_schedule"),
                            "Status": display_status(job)[0],
                            "Next run": next_run_label(job),
                        }
                        for job in rows
                    ],
                    width="stretch",
                    hide_index=True,
                )
                ids = [job.get("job_id") for job in rows if job.get("job_id") is not None]
                if ids:
                    if (
                        "overview_selected_job_id" in st.session_state
                        and st.session_state.overview_selected_job_id not in ids
                    ):
                        del st.session_state["overview_selected_job_id"]
                    selected = st.selectbox(
                        "Select job for details",
                        options=ids,
                        key="overview_selected_job_id",
                        format_func=lambda jid: f"Job {jid}",
                    )
                    st.session_state.selected_config_job_id = selected
            else:
                st.info("No configured jobs match the current filters.")

    with right:
        with st.container(border=True):
            selected_id = st.session_state.get(
                "overview_selected_job_id"
            ) or st.session_state.get("selected_config_job_id")
            chosen = next((j for j in jobs if j.get("job_id") == selected_id), None)
            if chosen is None and jobs:
                chosen = jobs[0]
                st.session_state.overview_selected_job_id = chosen.get("job_id")
            if chosen is None:
                st.markdown(
                    '<div class="pj-card-eyebrow">Selected job</div>'
                    '<div class="pj-card-title">Job details</div>',
                    unsafe_allow_html=True,
                )
                st.info("Select a configured job to inspect details.")
            else:
                label, tone = display_status(chosen)
                st.markdown(
                    f'<div class="pj-card-eyebrow">Selected job · #{chosen.get("job_id")}</div>'
                    '<div class="pj-card-title">Job details</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="pj-detail-hero"><div class="pj-detail-icon">SQL</div>'
                    f"<div><strong>{chosen.get('job_name') or '-'}</strong>"
                    f"<span>Parameterized partition routine</span></div></div>",
                    unsafe_allow_html=True,
                )
                op_class = "create" if chosen.get("is_create") else "drop"
                st.markdown(
                    f'{badge_fn(label, tone_to_badge(tone))}'
                    f'<span class="pj-op-pill {op_class}">{operation_label(chosen)}</span>',
                    unsafe_allow_html=True,
                )
                nxt = chosen.get("next_run_time")
                st.markdown(
                    f"**Target table:** `{target_table(chosen)}`  \n"
                    f"**Schedule:** `{chosen.get('job_schedule') or '-'}`  \n"
                    f"**Next execution:** `{nxt}` ({next_run_label(chosen)})  \n"
                    f"**Partition:** `{chosen.get('partition_period')} {chosen.get('partition_unit')}`  \n"
                    f"**Interval:** `{chosen.get('create_drop_interval')}`  \n"
                    f"**Enabled:** `{'Yes' if chosen.get('is_enabled') else 'No'}`  \n"
                    f"**Last run:** `{chosen.get('last_run_time') or '-'}`  \n"
                    f"**Last status:** `{chosen.get('last_run_status') or '-'}`"
                )
                st.markdown(
                    "<div class=\"pj-code\">"
                    f"{operation_label(chosen)} partition job\n"
                    f"{target_table(chosen)}\n"
                    f"partition unit: {chosen.get('partition_unit')}\n"
                    f"period: {chosen.get('partition_period')}\n"
                    f"interval: {chosen.get('create_drop_interval')}\n"
                    f"schedule: {chosen.get('job_schedule')}"
                    "</div>",
                    unsafe_allow_html=True,
                )
                render_manual_run_fn(chosen)

        with st.container(border=True):
            head_l, head_r = st.columns([3, 1])
            with head_l:
                st.markdown(
                    '<div class="pj-card-eyebrow">Recent activity</div>'
                    '<div class="pj-card-title">Execution history</div>',
                    unsafe_allow_html=True,
                )
            with head_r:
                if st.button("Full history", key="overview_full_history", width="stretch"):
                    st.session_state.main_nav = nav_history
                    st.rerun()
            recent = logs[:5]
            if not recent:
                st.info("No execution history rows were found.")
            else:
                for row in recent:
                    status_text = str(row.get("last_run_status") or "")
                    upper = status_text.upper()
                    if upper in {"SUCCESS", "MANUAL_SUCCESS"}:
                        tone = "green"
                        detail = "Partition operation completed successfully"
                    elif upper in {
                        "FAIL",
                        "MANUAL_FAIL",
                        "ERROR",
                        "FAILED",
                        "FAILED_CONNECTION",
                    }:
                        tone = "red"
                        detail = "Partition operation failed"
                    else:
                        tone = "amber"
                        detail = status_text or "See history for details"
                    st.markdown(
                        f'<div class="pj-activity"><span class="pj-activity-dot {tone}"></span>'
                        f"<div><strong>{row.get('job_name') or row.get('job_id')}</strong>"
                        f"<span>{detail}</span></div>"
                        f"<time>{row.get('job_runtime') or '-'}<b>{status_text or '-'}</b></time></div>",
                        unsafe_allow_html=True,
                    )

        insights = system_insights(jobs, logs, scheduler_ok=ok, status=status)
        st.markdown(
            '<div class="pj-insight"><div class="pj-card-eyebrow">System insight</div>'
            "<h3>Operational signal</h3>"
            + "".join(f"<p>{item}</p>" for item in insights)
            + "</div>",
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            st.markdown(
                '<div class="pj-card-eyebrow">System</div>'
                '<div class="pj-card-title">Scheduler status</div>',
                unsafe_allow_html=True,
            )
            if not ok:
                st.markdown(
                    '<div class="pj-status-strip fail" style="margin:0">'
                    '<div class="pj-status-message"><div class="pj-status-icon">!</div>'
                    "<div><strong>Scheduler backend unavailable</strong>"
                    "<span>Configuration and history remain available, but realtime "
                    "scheduler monitoring cannot currently be reached.</span></div></div>"
                    '<div class="pj-strip-meta"><span>Offline</span></div></div>',
                    unsafe_allow_html=True,
                )
                st.caption(message)
            else:
                assert status is not None
                active = bool(status.get("scheduler_active"))
                uptime = scheduler_uptime(status)
                st.markdown(
                    f'{badge_fn("Online" if active else "Idle", "ok" if active else "warn")}  \n'
                    f"**Uptime:** `{uptime['label']}`  \n"
                    f"**Last queue refresh:** `{format_age(status.get('last_refresh_at'))}`  \n"
                    f"**Jobs in queue:** `{status.get('upcoming_job_count', '-')}`  \n"
                    f"**Next job:** `#{status.get('next_job_id') or '-'}`  \n"
                    f"**Next execution:** `{status.get('next_expected_run_time') or '-'}`  \n"
                    f"**Lookahead:** `{status.get('lookahead_seconds', '-')}` seconds  \n"
                    f"**Reconciliation:** `{status.get('reconcile_seconds', '-')}` seconds"
                )


def export_button(jobs: list[dict[str, Any]], *, key: str = "hdr_export") -> None:
    if jobs:
        st.download_button(
            "Export",
            data=jobs_to_csv(jobs),
            file_name="partition_jobs.csv",
            mime="text/csv",
            key=key,
            width="stretch",
            help="Download configured jobs as CSV (read-only)",
        )
    else:
        st.button(
            "Export",
            key=f"{key}_disabled",
            width="stretch",
            disabled=True,
            help="Open Overview or Configured Jobs first to load data",
        )
