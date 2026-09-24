"use client";

import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Sheet } from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api";
import { formatWhen, isFailStatus, isSuccessStatus } from "@/lib/format";
import type { JobLog } from "@/lib/types";

export default function HistoryPage() {
  const [logs, setLogs] = useState<JobLog[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [jobFilter, setJobFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("All");
  const [selected, setSelected] = useState<JobLog | null>(null);

  async function load() {
    setError(null);
    try {
      const res = await api.logs(100);
      setLogs(res.logs || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load history");
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const filtered = useMemo(() => {
    return logs.filter((row) => {
      if (statusFilter !== "All" && String(row.last_run_status || "") !== statusFilter)
        return false;
      if (jobFilter.trim()) {
        const wanted = Number(jobFilter);
        if (!Number.isNaN(wanted) && row.job_id !== wanted) return false;
        if (Number.isNaN(wanted)) {
          const hay = `${row.job_name || ""}`.toLowerCase();
          if (!hay.includes(jobFilter.trim().toLowerCase())) return false;
        }
      }
      return true;
    });
  }, [logs, jobFilter, statusFilter]);

  return (
    <div className="pj-enter space-y-5">
      <PageHeader
        title="Execution history"
        subtitle="Latest executions recorded in mubasher_oms.partitioning_job_table_log."
        showActions={false}
      />

      <div className="flex flex-wrap gap-2">
        <Input
          className="max-w-xs"
          placeholder="Filter by job id or name"
          value={jobFilter}
          onChange={(e) => setJobFilter(e.target.value)}
        />
        <Select
          className="w-48"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option>All</option>
          <option>SUCCESS</option>
          <option>FAIL</option>
          <option>MANUAL_SUCCESS</option>
          <option>MANUAL_FAIL</option>
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
              <th className="px-3 py-2.5">Time</th>
              <th className="px-3 py-2.5">Job</th>
              <th className="px-3 py-2.5">Status</th>
              <th className="px-3 py-2.5">Error</th>
              <th className="px-3 py-2.5">Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((row) => {
              const fail = isFailStatus(row.last_run_status);
              const ok = isSuccessStatus(row.last_run_status);
              return (
                <tr key={row.job_log_id} className="border-t border-[#ebe8e1]">
                  <td className="px-3 py-2.5 tabular-nums">{formatWhen(row.job_runtime)}</td>
                  <td className="px-3 py-2.5">
                    <div className="font-bold">{row.job_name || `Job ${row.job_id}`}</div>
                    <div className="text-[0.65rem] text-[#718078]">#{row.job_id}</div>
                  </td>
                  <td className="px-3 py-2.5">
                    <Badge tone={fail ? "fail" : ok ? "ok" : "mute"}>
                      {row.last_run_status || "—"}
                    </Badge>
                  </td>
                  <td className="max-w-md truncate px-3 py-2.5 text-[#718078]">
                    {row.job_error ? String(row.job_error).slice(0, 120) : "—"}
                  </td>
                  <td className="px-3 py-2.5">
                    {row.job_error ? (
                      <Button variant="secondary" size="sm" onClick={() => setSelected(row)}>
                        View details
                      </Button>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              );
            })}
            {!filtered.length ? (
              <tr>
                <td colSpan={5} className="px-3 py-10 text-center text-[#718078]">
                  No execution history rows match.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => !open && setSelected(null)}
        title={
          selected
            ? `${selected.job_name || `Job ${selected.job_id}`} · ${selected.last_run_status}`
            : "Error"
        }
        description="Database error from partitioning_job_table_log"
        wide
      >
        <pre className="whitespace-pre-wrap rounded-xl border border-[#cbd7ef] bg-[#eef3ff] p-3 font-mono text-xs text-[#40526f]">
          {selected?.job_error}
        </pre>
      </Sheet>
    </div>
  );
}
