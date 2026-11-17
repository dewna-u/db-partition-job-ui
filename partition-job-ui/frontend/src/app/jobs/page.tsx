"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, OctagonAlert, Pencil } from "lucide-react";
import { useHub } from "@/components/hub/hub-context";
import { TargetBadge } from "@/components/targets/target-badges";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { Input, Select } from "@/components/ui/input";
import { Panel, QueryState } from "@/components/ui/panel";
import { Sheet } from "@/components/ui/sheet";
import { JobDetailPanel } from "@/components/jobs/job-detail-panel";
import { api, ApiError } from "@/lib/api";
import {
  attentionByJob,
  attentionTarget,
  displayStatus,
  enabledTone,
  formatWhen,
  formatDurationMs,
  nextRunLabel,
  operationLabel,
  runStatusLabel,
  runStatusTone,
  targetTable,
} from "@/lib/format";
import { isUnknownOutcomeReason, resolveTarget, reviewReason } from "@/lib/targets";
import type { AttentionItem, PartitionJob } from "@/lib/types";

export default function JobsPage() {
  const [jobs, setJobs] = useState<PartitionJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [op, setOp] = useState("All");
  const [enabled, setEnabled] = useState("All");
  const [status, setStatus] = useState("All");
  const [targetFilter, setTargetFilter] = useState("All");
  const { targets } = useHub();
  const [selected, setSelected] = useState<PartitionJob | null>(null);
  const [editMode, setEditMode] = useState(false);
  // Phase 5A: pending missed-run review items (never block the jobs list).
  const [attention, setAttention] = useState<AttentionItem[]>([]);
  const [attentionNote, setAttentionNote] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function loadAttention() {
    try {
      const res = await api.attention("pending");
      setAttention(res.items || []);
      setAttentionNote(res.available ? null : res.message);
    } catch (err) {
      setAttention([]);
      setAttentionNote(
        err instanceof ApiError ? `Missed-run review unavailable: ${err.message}` : null,
      );
    }
  }

  async function load(opts?: { refresh?: boolean }) {
    const isRefresh = Boolean(opts?.refresh);
    setError(null);
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const [res] = await Promise.all([api.jobs(), loadAttention()]);
      setJobs(res.jobs || []);
      setSelected((prev) => {
        if (!prev) return prev;
        const refreshed = (res.jobs || []).find((j) => j.job_id === prev.job_id);
        return refreshed || prev;
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load jobs");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    void load();
    // Mount-only initial load (same pattern as the Overview page).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const statuses = useMemo(
    () =>
      ["All", ...Array.from(new Set(jobs.map((j) => j.last_run_status).filter(Boolean)))] as string[],
    [jobs],
  );

  const pendingByJob = useMemo(() => attentionByJob(attention), [attention]);
  const unknownCount = attention.filter((a) => isUnknownOutcomeReason(a.reason)).length;

  const filtered = jobs.filter((job) => {
    if (status === "Needs review") {
      if (!pendingByJob.has(job.job_id)) return false;
    } else if (status !== "All" && String(job.last_run_status || "") !== status) {
      return false;
    }
    if (op !== "All" && operationLabel(job) !== op) return false;
    const jobTarget = resolveTarget(job.database_target_id, targets);
    if (targetFilter !== "All" && (jobTarget?.target_key || "") !== targetFilter) return false;
    if (enabled === "Enabled" && !job.is_enabled) return false;
    if (enabled === "Disabled" && job.is_enabled) return false;
    if (search.trim()) {
      const hay = `${job.job_id} ${job.job_name} ${targetTable(job)} ${jobTarget?.target_key || ""}`.toLowerCase();
      if (!hay.includes(search.trim().toLowerCase())) return false;
    }
    return true;
  });

  function openJob(job: PartitionJob, edit = false) {
    setSelected(job);
    setEditMode(edit);
  }

  return (
    <div className="pj-enter space-y-3">
      <PageHeader
        title="Partition jobs"
        count={loading ? undefined : filtered.length}
        subtitle="Configure and manage scheduled partition operations."
      />

      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-[200px] flex-1">
          <Input
            placeholder="Search jobs"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search jobs"
          />
        </div>
        {(targets || []).length > 1 ? (
          <Select
            value={targetFilter}
            onChange={(e) => setTargetFilter(e.target.value)}
            className="w-32"
            aria-label="Database target filter"
          >
            <option value="All">All targets</option>
            {(targets || []).map((t) => (
              <option key={t.target_id} value={t.target_key}>
                {t.target_key.toUpperCase()}
              </option>
            ))}
          </Select>
        ) : null}
        <Select value={op} onChange={(e) => setOp(e.target.value)} className="w-28" aria-label="Operation filter">
          <option>All</option>
          <option>CREATE</option>
          <option>DROP</option>
        </Select>
        <Select value={enabled} onChange={(e) => setEnabled(e.target.value)} className="w-32" aria-label="Enabled filter">
          <option>All</option>
          <option>Enabled</option>
          <option>Disabled</option>
        </Select>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-36" aria-label="Status filter">
          {statuses.map((s) => (
            <option key={s}>{s}</option>
          ))}
          <option>Needs review</option>
        </Select>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => void load({ refresh: true })}
          disabled={loading || refreshing}
          aria-busy={refreshing}
        >
          {refreshing ? (
            <>
              <PartOpsLoader size="sm" />
              Refreshing…
            </>
          ) : (
            "Refresh"
          )}
        </Button>
      </div>

      {notice ? (
        <p className="text-[12px] font-semibold text-[var(--success-fg)]" role="status">
          {notice}
        </p>
      ) : null}
      {attentionNote ? (
        <p className="text-[11px] text-[var(--content-tertiary)]">{attentionNote}</p>
      ) : null}

      {unknownCount ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-[6px] border-2 border-[var(--error-fg)] bg-[var(--error-bg)] px-3 py-2.5"
        >
          <OctagonAlert className="mt-0.5 h-4 w-4 shrink-0 text-[var(--error-fg)]" aria-hidden />
          <div className="text-[12px]">
            <div className="font-semibold text-[var(--error-fg)]">
              {unknownCount} remote outcome{unknownCount === 1 ? "" : "s"} unknown
            </div>
            <div className="text-[var(--content-primary)]">
              PartOps cannot confirm whether the target database committed these operations. Do not
              run those jobs again until the target state has been inspected. Open the job to record
              the inspection result.
            </div>
          </div>
        </div>
      ) : null}

      {attention.length ? (
        <Panel className="border-[var(--warning-fg)] bg-[var(--warning-bg)]">
          <div className="flex items-center gap-2 px-3 pt-2.5">
            <AlertTriangle className="h-4 w-4 text-[var(--warning-fg)]" aria-hidden />
            <h2 className="text-[13px] font-semibold text-[var(--content-primary)]">
              {attention.length} item{attention.length === 1 ? "" : "s"} need review
            </h2>
          </div>
          <p className="px-3 pt-0.5 text-[11px] text-[var(--content-secondary)]">
            Missed or retry-exhausted occurrences were not executed automatically (the job already
            moved to its next normal schedule). Remote outcomes that are unknown must be inspected
            and resolved before anything else is done with that job.
          </p>
          <ul className="mt-1.5 divide-y divide-[var(--border-default)] border-t border-[var(--border-default)]">
            {attention.map((item) => {
              const job = jobs.find((j) => j.job_id === item.job_id);
              const isDrop = item.is_create !== true;
              const reason = reviewReason(item.reason);
              const unknown = isUnknownOutcomeReason(item.reason);
              return (
                <li
                  key={item.attention_id}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 text-[12px]"
                >
                  <Badge tone={reason.tone} className="normal-case tracking-normal">{reason.label}</Badge>
                  <Badge tone={isDrop ? "drop" : "create"}>{isDrop ? "DROP" : "CREATE"}</Badge>
                  {item.database_target_id != null ? (
                    <TargetBadge
                      target={resolveTarget(item.database_target_id, targets)}
                      fallbackId={item.database_target_id}
                    />
                  ) : null}
                  <span className="font-medium text-[var(--content-primary)]">
                    {item.job_name || `Job ${item.job_id}`}
                  </span>
                  <span className="font-mono text-[11px] text-[var(--content-secondary)]">
                    {attentionTarget(item)}
                  </span>
                  <span className="font-mono text-[11px] text-[var(--content-secondary)]">
                    {unknown ? "dispatched/expected" : "missed"} {formatWhen(item.missed_run_time)}
                  </span>
                  <span className="ml-auto">
                    <Button
                      size="sm"
                      variant={unknown ? "danger" : "secondary"}
                      disabled={!job}
                      onClick={() => job && openJob(job, false)}
                    >
                      Review
                    </Button>
                  </span>
                </li>
              );
            })}
          </ul>
        </Panel>
      ) : null}

      <Panel className="overflow-hidden">
        <QueryState
          loading={loading && jobs.length === 0}
          error={error}
          empty={!loading && !error && filtered.length === 0}
          emptyMessage="No jobs match the current filters."
        >
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead className="sticky top-0 bg-[var(--surface-tertiary)] text-[11px] font-medium text-[var(--content-secondary)]">
                <tr>
                  <th className="px-3 py-2 font-medium">Job</th>
                  <th className="px-3 py-2 font-medium">Database</th>
                  <th className="px-3 py-2 font-medium">Table</th>
                  <th className="px-3 py-2 font-medium">Operation</th>
                  <th className="px-3 py-2 font-medium">Schedule</th>
                  <th className="px-3 py-2 font-medium">Next run</th>
                  <th className="px-3 py-2 font-medium">Last scheduled run</th>
                  <th className="px-3 py-2 font-medium">Scheduled status</th>
                  <th className="px-3 py-2 font-medium">Enabled</th>
                  <th className="px-3 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((job) => {
                  const st = displayStatus(job);
                  const type = operationLabel(job);
                  const overdue = nextRunLabel(job).startsWith("Overdue");
                  const pending = pendingByJob.get(job.job_id) || [];
                  return (
                    <tr
                      key={job.job_id}
                      className={
                        pending.length
                          ? "cursor-pointer border-t border-[var(--border-default)] bg-[var(--warning-bg)] shadow-[inset_3px_0_0_var(--warning-fg)] hover:bg-[var(--hover-surface-1)]"
                          : "cursor-pointer border-t border-[var(--border-default)] hover:bg-[var(--hover-surface-1)]"
                      }
                      onClick={() => openJob(job, false)}
                      tabIndex={0}
                      aria-label={`Open ${job.job_name || `job ${job.job_id}`}`}
                      onKeyDown={(e) => {
                        if (e.target !== e.currentTarget) return;
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          openJob(job, false);
                        }
                      }}
                    >
                      <td className="px-3 py-2 align-top">
                        <div className="font-medium text-[var(--content-primary)]">
                          {job.job_name || `Job ${job.job_id}`}
                        </div>
                        <div className="mt-0.5 font-mono text-[11px] tabular-nums text-[var(--content-tertiary)]">
                          #{job.job_id}
                        </div>
                      </td>
                      <td className="px-3 py-2 align-top">
                        <TargetBadge
                          target={resolveTarget(job.database_target_id, targets)}
                          fallbackId={job.database_target_id ?? null}
                        />
                      </td>
                      <td className="max-w-[220px] px-3 py-2 align-top">
                        <span
                          className="block truncate font-mono text-[11px] text-[var(--content-secondary)]"
                          title={targetTable(job)}
                        >
                          {targetTable(job)}
                        </span>
                      </td>
                      <td className="px-3 py-2 align-top">
                        <Badge tone={type === "CREATE" ? "create" : "drop"}>{type}</Badge>
                      </td>
                      <td className="px-3 py-2 align-top">
                        <span className="font-mono text-[11px]">{job.job_schedule}</span>
                      </td>
                      <td className="px-3 py-2 align-top tabular-nums">
                        {overdue ? (
                          <Badge tone="warning">OVERDUE</Badge>
                        ) : (
                          nextRunLabel(job)
                        )}
                      </td>
                      <td className="px-3 py-2 align-top tabular-nums text-[var(--content-secondary)]">
                        {String(job.last_run_time || "—")}
                      </td>
                      <td className="px-3 py-2 align-top">
                        {pending.length ? (
                          <div className="mb-1">
                            {pending.some((p) => isUnknownOutcomeReason(p.reason)) ? (
                              <Badge tone="error" className="whitespace-nowrap">
                                <OctagonAlert className="h-3 w-3" aria-hidden />
                                Outcome unknown
                              </Badge>
                            ) : (
                              <Badge tone="warning" className="whitespace-nowrap">
                                <AlertTriangle className="h-3 w-3" aria-hidden />
                                Needs review
                              </Badge>
                            )}
                            <div className="mt-0.5 font-mono text-[10px] text-[var(--content-secondary)]">
                              {isUnknownOutcomeReason(pending[0].reason) ? "since" : "missed"}{" "}
                              {formatWhen(pending[0].missed_run_time)}
                              {pending.length > 1 ? ` +${pending.length - 1}` : ""}
                            </div>
                          </div>
                        ) : null}
                        {job.last_run_status ? (
                          <Badge tone={runStatusTone(job.last_run_status)}>
                            {runStatusLabel(job.last_run_status)}
                          </Badge>
                        ) : (
                          <span className="text-[var(--content-tertiary)]">{st.label}</span>
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <Badge tone={enabledTone(job.is_enabled)}>
                          {job.is_enabled ? "ENABLED" : "DISABLED"}
                        </Badge>
                      </td>
                      <td className="px-3 py-2 text-right align-top">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          aria-label={`Edit ${job.job_name || `job ${job.job_id}`}`}
                          title="Edit job"
                          className="h-7 w-7"
                          onClick={(e) => {
                            e.stopPropagation();
                            openJob(job, true);
                          }}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </QueryState>
      </Panel>

      <p className="text-[11px] text-[var(--content-tertiary)]">
        Last duration is shown in the job details drawer.
        {selected
          ? ` Selected duration: ${formatDurationMs(selected.last_execution_duration_ms)}`
          : ""}
      </p>

      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => {
          if (!open) {
            setSelected(null);
            setEditMode(false);
          }
        }}
        title={
          selected
            ? editMode
              ? `Edit job · #${selected.job_id}`
              : `Job #${selected.job_id}`
            : "Job"
        }
        description={
          editMode
            ? "Load current values, edit configuration, then save changes"
            : "Database target, configuration, duration, and manual run"
        }
        wide
      >
        {selected ? (
          <JobDetailPanel
            key={`${selected.job_id}-${editMode ? "edit" : "view"}`}
            job={selected}
            startInEdit={editMode}
            onRan={() => {
              void load();
            }}
            onUpdated={() => {
              setEditMode(false);
              void load();
            }}
            attention={pendingByJob.get(selected.job_id) || []}
            onAttentionChanged={(message) => {
              setNotice(message || null);
              void load({ refresh: true });
            }}
          />
        ) : null}
      </Sheet>
    </div>
  );
}
