"use client";

import { useEffect, useMemo, useState } from "react";
import { Pencil } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Sheet } from "@/components/ui/sheet";
import { JobDetailPanel } from "@/components/jobs/job-detail-panel";
import { api, ApiError } from "@/lib/api";
import {
  displayStatus,
  formatDurationMs,
  nextRunLabel,
  operationLabel,
  targetTable,
} from "@/lib/format";
import type { PartitionJob } from "@/lib/types";

export default function JobsPage() {
  const [jobs, setJobs] = useState<PartitionJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [op, setOp] = useState("All");
  const [enabled, setEnabled] = useState("All");
  const [status, setStatus] = useState("All");
  const [selected, setSelected] = useState<PartitionJob | null>(null);
  const [editMode, setEditMode] = useState(false);

  async function load() {
    setError(null);
    try {
      const res = await api.jobs();
      setJobs(res.jobs || []);
      if (selected) {
        const refreshed = (res.jobs || []).find((j) => j.job_id === selected.job_id);
        if (refreshed) setSelected(refreshed);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load jobs");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    <div className="pj-enter space-y-5">
      <PageHeader
        title="Configured jobs"
        subtitle="Every row is one parameterized partition job in mubasher_oms.partitioning_job_table."
      />

      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[200px] flex-1">
          <Input
            placeholder="Search job / table"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <Select value={op} onChange={(e) => setOp(e.target.value)} className="w-32">
          <option>All</option>
          <option>CREATE</option>
          <option>DROP</option>
        </Select>
        <Select value={enabled} onChange={(e) => setEnabled(e.target.value)} className="w-36">
          <option>All</option>
          <option>Enabled</option>
          <option>Disabled</option>
        </Select>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-40">
          {statuses.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </Select>
        <Button variant="secondary" onClick={() => void load()}>
          Refresh
        </Button>
      </div>

      {error ? <p className="text-sm font-semibold text-[#c4473a]">{error}</p> : null}

      <div className="overflow-hidden rounded-card border border-pj-line bg-pj-card shadow-card">
        <table className="w-full text-left text-xs">
          <thead className="bg-pj-table-head text-[0.62rem] uppercase tracking-[0.08em] text-pj-muted">
            <tr>
              <th className="px-3 py-2.5">Job ID</th>
              <th className="px-3 py-2.5">Job name</th>
              <th className="px-3 py-2.5">Target</th>
              <th className="px-3 py-2.5">Type</th>
              <th className="px-3 py-2.5">Schedule</th>
              <th className="px-3 py-2.5">Next run</th>
              <th className="px-3 py-2.5">Last run</th>
              <th className="px-3 py-2.5">Status</th>
              <th className="px-3 py-2.5">Enabled</th>
              <th className="px-3 py-2.5 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((job) => {
              const st = displayStatus(job);
              const type = operationLabel(job);
              return (
                <tr
                  key={job.job_id}
                  className="cursor-pointer border-t border-pj-line hover:bg-pj-surface"
                  onClick={() => openJob(job, false)}
                >
                  <td className="px-3 py-2.5 font-bold tabular-nums">{job.job_id}</td>
                  <td className="px-3 py-2.5 font-semibold">{job.job_name}</td>
                  <td className="px-3 py-2.5 text-[#53615b]">{targetTable(job)}</td>
                  <td className="px-3 py-2.5">
                    <Badge tone={type === "CREATE" ? "create" : "drop"}>{type}</Badge>
                  </td>
                  <td className="px-3 py-2.5 font-mono text-[0.7rem]">{job.job_schedule}</td>
                  <td className="px-3 py-2.5 tabular-nums">{nextRunLabel(job)}</td>
                  <td className="px-3 py-2.5">{String(job.last_run_time || "—")}</td>
                  <td className="px-3 py-2.5">{st.label}</td>
                  <td className="px-3 py-2.5">{job.is_enabled ? "Yes" : "No"}</td>
                  <td className="px-3 py-2.5 text-right">
                    <button
                      type="button"
                      title="Edit configuration"
                      className="inline-flex rounded-lg border border-[#d1cdc4] bg-white p-1.5 text-[#53615b] hover:border-[#165dff] hover:text-[#165dff]"
                      onClick={(e) => {
                        e.stopPropagation();
                        openJob(job, true);
                      }}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              );
            })}
            {!filtered.length ? (
              <tr>
                <td colSpan={10} className="px-3 py-10 text-center text-pj-muted">
                  No jobs match the current filters.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <p className="text-[0.65rem] text-pj-muted">
        Last duration is shown in the job details drawer to keep this table readable.
        Current last duration for the selected job:{" "}
        {selected ? formatDurationMs(selected.last_execution_duration_ms) : "—"}
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
              ? `Edit partition job · #${selected.job_id}`
              : `Job #${selected.job_id}`
            : "Job"
        }
        description={
          editMode
            ? "Load current values, edit configuration, then save"
            : "Configuration details, duration, and manual run"
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
