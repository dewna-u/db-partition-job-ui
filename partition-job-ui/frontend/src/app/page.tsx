"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { PageHeader } from "@/components/shell/page-header";
import { Badge, StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Sheet } from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api";
import {
  displayStatus,
  formatWhen,
  isFailStatus,
  isSuccessStatus,
  nextRunLabel,
  operationLabel,
  targetTable,
} from "@/lib/format";
import type { DashboardSummary, PartitionJob } from "@/lib/types";
import { JobDetailPanel } from "@/components/jobs/job-detail-panel";

export default function OverviewPage() {
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [opFilter, setOpFilter] = useState("All");
  const [stateFilter, setStateFilter] = useState("All");
  const [selected, setSelected] = useState<PartitionJob | null>(null);
  const [errorLog, setErrorLog] = useState<{ title: string; body: string } | null>(
    null,
  );

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const summary = await api.dashboard();
      setData(summary);
      if (!selected && summary.jobs.length) {
        setSelected(summary.jobs[0]);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load dashboard");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => {
    const jobs = data?.jobs || [];
    return jobs.filter((job) => {
      const op = operationLabel(job);
      const { label } = displayStatus(job);
      if (opFilter !== "All" && op !== opFilter) return false;
      if (stateFilter === "Enabled" && !job.is_enabled) return false;
      if (stateFilter === "Disabled" && job.is_enabled) return false;
      if (stateFilter === "Failed" && label !== "Failed") return false;
      if (search.trim()) {
        const hay = `${job.job_id} ${job.job_name} ${targetTable(job)}`.toLowerCase();
        if (!hay.includes(search.trim().toLowerCase())) return false;
      }
      return true;
    });
  }, [data, search, opFilter, stateFilter]);

  const schedulerOk = Boolean(data?.scheduler.ok);
  const active = Boolean(data?.scheduler.status?.scheduler_active);
  const banner = (() => {
    if (data?.readiness_error) {
      return {
        tone: "fail" as const,
        title: "Database readiness check failed",
        detail: data.readiness_error,
      };
    }
    if (!schedulerOk) {
      return {
        tone: "warn" as const,
        title: "Scheduler backend unavailable",
        detail:
          "Job configuration remains available, but realtime scheduler monitoring cannot currently be reached.",
      };
    }
    if ((data?.failed_last_24h || 0) > 0) {
      return {
        tone: "warn" as const,
        title: "Some jobs require attention",
        detail: `${data?.failed_last_24h} partition job(s) failed during the last 24 hours.`,
      };
    }
    return {
      tone: "ok" as const,
      title: "Everything is running smoothly",
      detail:
        active
          ? "All scheduled jobs are healthy and the realtime scheduler is responding."
          : "Configuration is ready. Monitor the scheduler heartbeat for live timing.",
    };
  })();

  return (
    <div className="pj-enter space-y-5">
      <PageHeader
        title="Partition job management"
        subtitle="Configure, monitor, and safely manage PostgreSQL partition jobs."
      />

      <section
        className={`flex flex-wrap items-center justify-between gap-4 rounded-card border px-4 py-3.5 shadow-card ${
          banner.tone === "ok"
            ? "border-[#cfe3d6] bg-[#f3faf6]"
            : banner.tone === "warn"
              ? "border-[#e5c4a8] bg-[#fff7f0]"
              : "border-[#e7b7b1] bg-[#fff5f3]"
        }`}
      >
        <div className="flex items-start gap-3">
          <StatusDot
            tone={banner.tone === "ok" ? "green" : banner.tone === "warn" ? "amber" : "red"}
            className="mt-1.5"
          />
          <div>
            <div className="text-sm font-extrabold text-[#1c2730]">{banner.title}</div>
            <div className="mt-0.5 text-xs text-[#5f6d68]">{banner.detail}</div>
          </div>
        </div>
        <div className="flex flex-wrap gap-4 text-xs font-semibold text-[#5f6d68]">
          <span className="inline-flex items-center gap-1.5">
            <StatusDot tone="blue" /> {data?.counts.enabled ?? "—"} scheduled
          </span>
          <span className="inline-flex items-center gap-1.5">
            <StatusDot tone="green" /> {data?.success_rate.label ?? "—"} success
          </span>
          <span className="inline-flex items-center gap-1.5">
            <StatusDot tone={active ? "green" : schedulerOk ? "amber" : "red"} />
            {active ? "Scheduler online" : schedulerOk ? "Scheduler idle" : "Scheduler offline"}
          </span>
        </div>
      </section>

      {error ? (
        <div className="rounded-card border border-[#e7b7b1] bg-[#fff5f3] px-4 py-3 text-sm text-[#c4473a]">
          {error}
        </div>
      ) : null}

      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        {[
          {
            label: "Configured jobs",
            value: data?.counts.total ?? (loading ? "…" : "0"),
            detail: data
              ? `${data.counts.enabled} enabled / ${data.counts.disabled} disabled`
              : "—",
          },
          {
            label: "Next execution",
            value: data?.next_execution.available
              ? data.next_execution.countdown
              : "—",
            detail: data?.next_execution.detail || "No upcoming jobs",
            warn: String(data?.next_execution.countdown || "").startsWith("Overdue"),
          },
          {
            label: "Success rate",
            value: data?.success_rate.label ?? "—",
            detail: data?.success_rate.detail || "Last 30 days",
          },
          {
            label: "Scheduler uptime",
            value: data?.scheduler_uptime.label ?? "—",
            detail: data?.scheduler_uptime.detail || "Backend offline",
          },
          {
            label: "Failed executions",
            value: data?.failed_executions ?? (loading ? "…" : "0"),
            detail: "In loaded history",
          },
        ].map((card) => (
          <div
            key={card.label}
            className={`rounded-card border border-pj-line bg-pj-card p-4 shadow-card ${
              card.warn ? "border-[#e5c4a8] bg-[#fff7f0]" : ""
            }`}
          >
            <div className="text-[0.68rem] font-bold uppercase tracking-[0.12em] text-[#7e8982]">
              {card.label}
            </div>
            <div className="mt-2 text-[1.65rem] font-extrabold tracking-[-0.04em] tabular-nums">
              {card.value}
            </div>
            <div className="mt-1 text-xs text-[#718078]">{card.detail}</div>
          </div>
        ))}
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.55fr_0.95fr]">
        <div className="rounded-card border border-pj-line bg-pj-card p-4 shadow-card">
          <div className="mb-3 flex items-end justify-between gap-3">
            <div>
              <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
                Live queue
              </div>
              <div className="text-base font-extrabold tracking-tight">Configured jobs</div>
            </div>
            <Button variant="secondary" asChild>
              <Link href="/jobs">View all</Link>
            </Button>
          </div>
          <div className="mb-3 grid gap-2 md:grid-cols-[1.4fr_0.8fr_1fr]">
            <Input
              placeholder="Filter jobs..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <Select value={opFilter} onChange={(e) => setOpFilter(e.target.value)}>
              <option>All</option>
              <option>CREATE</option>
              <option>DROP</option>
            </Select>
            <Select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
              <option>All</option>
              <option>Enabled</option>
              <option>Disabled</option>
              <option>Failed</option>
            </Select>
          </div>
          <div className="overflow-hidden rounded-xl border border-pj-line">
            <table className="w-full text-left text-xs">
              <thead className="bg-[#f3f0e9] text-[0.62rem] uppercase tracking-[0.08em] text-[#7e8982]">
                <tr>
                  <th className="px-3 py-2.5 font-bold">Job</th>
                  <th className="px-3 py-2.5 font-bold">Type</th>
                  <th className="px-3 py-2.5 font-bold">Status</th>
                  <th className="px-3 py-2.5 font-bold">Next run</th>
                </tr>
              </thead>
              <tbody>
                {filtered.slice(0, 12).map((job) => {
                  const status = displayStatus(job);
                  const op = operationLabel(job);
                  const isSel = selected?.job_id === job.job_id;
                  return (
                    <tr
                      key={job.job_id}
                      onClick={() => setSelected(job)}
                      className={`cursor-pointer border-t border-[#ebe8e1] hover:bg-[#f7f5ef] ${
                        isSel ? "bg-[#eef3ff]" : ""
                      }`}
                    >
                      <td className="px-3 py-2.5">
                        <div className="font-bold text-[#1c2730]">
                          {job.job_name || `Job ${job.job_id}`}
                        </div>
                        <div className="mt-0.5 text-[0.65rem] text-[#718078]">
                          {targetTable(job)}
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <Badge tone={op === "CREATE" ? "create" : "drop"}>{op}</Badge>
                      </td>
                      <td className="px-3 py-2.5">
                        <span className="inline-flex items-center gap-1.5 font-semibold">
                          <StatusDot
                            tone={
                              status.tone === "green"
                                ? "green"
                                : status.tone === "red"
                                  ? "red"
                                  : status.tone === "amber"
                                    ? "amber"
                                    : status.tone === "blue"
                                      ? "blue"
                                      : "mute"
                            }
                          />
                          {status.label}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 font-semibold tabular-nums text-[#53615b]">
                        {nextRunLabel(job)}
                      </td>
                    </tr>
                  );
                })}
                {!loading && filtered.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="px-3 py-8 text-center text-[#718078]">
                      No configured jobs match the current filters.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded-card border border-pj-line bg-pj-card p-4 shadow-card">
          {selected ? (
            <JobDetailPanel job={selected} onRan={() => void load()} />
          ) : (
            <div className="text-sm text-[#718078]">Select a job to inspect details.</div>
          )}
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-card border border-pj-line bg-pj-card p-4 shadow-card">
          <div className="mb-3 flex items-end justify-between">
            <div>
              <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
                Recent activity
              </div>
              <div className="text-base font-extrabold">Execution history</div>
            </div>
            <Button variant="secondary" asChild>
              <Link href="/history">Full history</Link>
            </Button>
          </div>
          <div className="divide-y divide-[#ebe8e1]">
            {(data?.logs || []).slice(0, 8).map((log) => {
              const fail = isFailStatus(log.last_run_status);
              const ok = isSuccessStatus(log.last_run_status);
              return (
                <button
                  key={log.job_log_id}
                  type="button"
                  className="flex w-full items-start gap-3 py-3 text-left hover:bg-[#f7f5ef]"
                  onClick={() => {
                    if (log.job_error) {
                      setErrorLog({
                        title: `${log.job_name || `Job ${log.job_id}`} · ${log.last_run_status}`,
                        body: String(log.job_error),
                      });
                    }
                  }}
                >
                  <StatusDot tone={fail ? "red" : ok ? "green" : "amber"} className="mt-1.5" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[0.78rem] font-bold">
                      {log.job_name || `Job ${log.job_id}`}
                    </div>
                    <div className="mt-0.5 text-[0.68rem] text-[#7f8a84]">
                      {fail
                        ? "Partition operation failed"
                        : ok
                          ? "Partition operation completed successfully"
                          : `Status: ${log.last_run_status || "—"}`}
                    </div>
                  </div>
                  <div className="text-right text-[0.62rem] text-[#8d9790]">
                    <div>{formatWhen(log.job_runtime)}</div>
                    <div className="mt-0.5 font-bold text-[#53615b]">
                      {log.last_run_status || "—"}
                    </div>
                  </div>
                </button>
              );
            })}
            {!loading && !(data?.logs || []).length ? (
              <div className="py-8 text-center text-sm text-[#718078]">No executions yet.</div>
            ) : null}
          </div>
        </div>

        <div className="relative overflow-hidden rounded-card border border-[#202c34] bg-[#202c34] p-5 text-[#f5f3ea] shadow-card">
          <div className="pointer-events-none absolute -right-10 -top-14 h-40 w-40 rounded-full bg-[#e5ff5c] opacity-85" />
          <div className="relative z-10">
            <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#aebbb4]">
              System insight
            </div>
            <h3 className="mt-2 text-[1.05rem] font-extrabold tracking-tight">
              Operational notes
            </h3>
            <ul className="mt-3 space-y-2 text-[0.78rem] leading-relaxed text-[#b5c0bb]">
              {(data?.insights || ["Loading insights…"]).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <Sheet
        open={Boolean(errorLog)}
        onOpenChange={(open) => !open && setErrorLog(null)}
        title={errorLog?.title || "Error details"}
        description="Database error from partitioning_job_table_log"
        wide
      >
        <pre className="whitespace-pre-wrap rounded-xl border border-[#cbd7ef] bg-[#eef3ff] p-3 font-mono text-xs text-[#40526f]">
          {errorLog?.body}
        </pre>
      </Sheet>
    </div>
  );
}
