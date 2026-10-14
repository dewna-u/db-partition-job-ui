"use client";

import { useEffect, useMemo, useState } from "react";
import { Pencil } from "lucide-react";
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
  displayStatus,
  enabledTone,
  formatDurationMs,
  nextRunLabel,
  operationLabel,
  runStatusLabel,
  runStatusTone,
  targetTable,
} from "@/lib/format";
import type { PartitionJob } from "@/lib/types";

export default function JobsPage() {
  const [jobs, setJobs] = useState<PartitionJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [op, setOp] = useState("All");
  const [enabled, setEnabled] = useState("All");
  const [status, setStatus] = useState("All");
  const [selected, setSelected] = useState<PartitionJob | null>(null);
  const [editMode, setEditMode] = useState(false);

  async function load(opts?: { refresh?: boolean }) {
    const isRefresh = Boolean(opts?.refresh);
    setError(null);
    if (isRefresh) setRefreshing(true);
    else setLoading(true);
    try {
      const res = await api.jobs();
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
  }, []);

  const statuses = useMemo(
    () =>
      ["All", ...Array.from(new Set(jobs.map((j) => j.last_run_status).filter(Boolean)))] as string[],
    [jobs],
  );

  const filtered = jobs.filter((job) => {
    if (op !== "All" && operationLabel(job) !== op) return false;
    if (enabled === "Enabled" && !job.is_enabled) return false;
    if (enabled === "Disabled" && job.is_enabled) return false;
    if (status !== "All" && String(job.last_run_status || "") !== status) return false;
    if (search.trim()) {
      const hay = `${job.job_id} ${job.job_name} ${targetTable(job)}`.toLowerCase();
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
                  <th className="px-3 py-2 font-medium">Target</th>
                  <th className="px-3 py-2 font-medium">Operation</th>
                  <th className="px-3 py-2 font-medium">Schedule</th>
                  <th className="px-3 py-2 font-medium">Next run</th>
                  <th className="px-3 py-2 font-medium">Last run</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Enabled</th>
                  <th className="px-3 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((job) => {
                  const st = displayStatus(job);
                  const type = operationLabel(job);
                  const overdue = nextRunLabel(job).startsWith("Overdue");
                  return (
                    <tr
                      key={job.job_id}
                      className="cursor-pointer border-t border-[var(--border-default)] hover:bg-[var(--hover-surface-1)]"
                      onClick={() => openJob(job, false)}
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
                        <span className="font-mono text-[11px] text-[var(--content-secondary)]">
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
            : "Configuration, duration, and manual run"
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
          />
        ) : null}
      </Sheet>
    </div>
  );
}
