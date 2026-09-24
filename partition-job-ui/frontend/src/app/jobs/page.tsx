"use client";

import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Sheet } from "@/components/ui/sheet";
import { JobDetailPanel } from "@/components/jobs/job-detail-panel";
import { api, ApiError } from "@/lib/api";
import {
  displayStatus,
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

  async function load() {
    setError(null);
    try {
      const res = await api.jobs();
      setJobs(res.jobs || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load jobs");
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
          <thead className="bg-[#f3f0e9] text-[0.62rem] uppercase tracking-[0.08em] text-[#7e8982]">
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
            </tr>
          </thead>
          <tbody>
            {filtered.map((job) => {
              const st = displayStatus(job);
              const type = operationLabel(job);
              return (
                <tr
                  key={job.job_id}
                  className="cursor-pointer border-t border-[#ebe8e1] hover:bg-[#f7f5ef]"
                  onClick={() => setSelected(job)}
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
                </tr>
              );
            })}
            {!filtered.length ? (
              <tr>
                <td colSpan={9} className="px-3 py-10 text-center text-[#718078]">
                  No jobs match the current filters.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => !open && setSelected(null)}
        title={selected ? `Job #${selected.job_id}` : "Job"}
        description="Configuration details and manual run"
        wide
      >
        {selected ? (
          <JobDetailPanel
            job={selected}
            onRan={() => {
              void load();
            }}
          />
        ) : null}
      </Sheet>
    </div>
  );
}
