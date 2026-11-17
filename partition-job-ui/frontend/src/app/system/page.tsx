"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { PageHeader } from "@/components/shell/page-header";
import {
  Badge,
  readinessStatusBadgeTone,
  readinessStatusLabel,
  StatusDot,
} from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import { useHub } from "@/components/hub/hub-context";
import { SchedulerTargetsTable } from "@/components/targets/scheduler-targets-table";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { hubStatus, targetReadiness } from "@/lib/targets";
import type { SystemReadinessReport } from "@/lib/types";

/** Operator-facing section names (backend category keys unchanged). */
const SECTION_LABEL: Record<string, string> = {
  database: "Control database & framework",
  targets: "Managed database targets",
  scheduler: "Scheduler",
  host: "PartOps services",
  configuration: "Runtime configuration",
  legacy: "Legacy pgAgent",
  frontend: "Frontend",
};
const SECTION_ORDER = ["host", "database", "targets", "scheduler", "configuration", "legacy", "frontend"];

const SECRET_KEY = /password|passwd|secret|token|api[_-]?key|credential/i;

function safeDetails(details: unknown): Record<string, unknown> | null {
  if (!details || typeof details !== "object" || Array.isArray(details)) {
    return null;
  }
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(details as Record<string, unknown>)) {
    if (SECRET_KEY.test(key)) continue;
    if (typeof value === "string" && SECRET_KEY.test(value)) continue;
    out[key] = value;
  }
  return Object.keys(out).length ? out : null;
}

function overallDot(status: string): "green" | "amber" | "red" | "blue" | "mute" {
  switch (status.toLowerCase()) {
    case "ready":
      return "green";
    case "warning":
      return "amber";
    case "failed":
      return "red";
    case "not_required":
      return "mute";
    default:
      return "blue";
  }
}

export default function SystemPage() {
  const [report, setReport] = useState<SystemReadinessReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (refresh: boolean) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const data = await api.systemReadiness(refresh);
      setReport(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load system readiness");
      if (!refresh) setReport(null);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  const counts = report?.counts;
  const hub = hubStatus(report);
  const targetRows = targetReadiness(report);
  const { scheduler, schedulerOk, targets } = useHub();
  const categories = [...(report?.categories || [])].sort(
    (a, b) => SECTION_ORDER.indexOf(a.key) - SECTION_ORDER.indexOf(b.key),
  );

  return (
    <div className="pj-enter space-y-3">
      <PageHeader
        title="System"
        subtitle="Operational health of this hub: PartOps services, control database, managed targets, scheduler and framework."
        showActions={false}
      />

      <section className="rounded-card border border-pj-line bg-pj-card p-3">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <StatusDot
              tone={
                !report
                  ? "mute"
                  : hub.level === "down"
                    ? "red"
                    : hub.level === "degraded"
                      ? "amber"
                      : overallDot(report.overall_status)
              }
              className="mt-1.5"
            />
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[16px] font-semibold tracking-tight text-pj-ink">
                  {loading && !report ? "Loading diagnostics…" : report ? hub.title : "Unknown"}
                </span>
                {report ? (
                  <Badge
                    tone={hub.level === "down" ? "error" : hub.level === "degraded" ? "warning" : "success"}
                  >
                    {hub.level === "down" ? "DOWN" : hub.level === "degraded" ? "DEGRADED" : "OPERATIONAL"}
                  </Badge>
                ) : null}
              </div>
              {report && hub.detail ? (
                <p className="mt-0.5 text-[12px] text-[var(--content-primary)]">{hub.detail}</p>
              ) : null}
              <p className="mt-1 text-sm text-pj-muted">
                {report?.checked_at
                  ? `Last checked ${new Date(report.checked_at).toLocaleString()}`
                  : "Refresh checks to validate deployment health."}
                {report?.from_cache ? " (cached)" : null}
                {report ? ` · backend rollup: ${report.overall_label}` : null}
              </p>
            </div>
          </div>
          <Button
            type="button"
            onClick={() => void load(true)}
            disabled={loading || refreshing}
            aria-busy={refreshing || (loading && !report)}
          >
            {refreshing || (loading && !report) ? (
              <PartOpsLoader size="sm" />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden />
            )}
            {refreshing ? "Refreshing…" : "Refresh checks"}
          </Button>
        </div>

        {error ? (
          <div className="mt-4 rounded-lg border border-pj-fail/30 bg-pj-fail-bg px-3 py-2 text-sm text-pj-fail">
            {error}
          </div>
        ) : null}

        {counts ? (
          <div className="mt-5 grid gap-2 sm:grid-cols-5">
            {(
              [
                ["ready", "Ready"],
                ["warning", "Warning"],
                ["failed", "Failed"],
                ["not_required", "Not required"],
                ["unknown", "Unknown"],
              ] as const
            ).map(([key, label]) => (
              <div
                key={key}
                className="rounded-lg border border-pj-line bg-pj-surface px-3 py-2"
              >
                <div className="text-[0.62rem] font-bold uppercase tracking-[0.1em] text-pj-muted">
                  {label}
                </div>
                <div className="mt-1 text-[28px] font-semibold tabular-nums text-pj-ink">
                  {counts[key]}
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      {loading && !report ? (
        <div className="flex items-center gap-2 text-sm text-pj-muted" aria-busy="true" role="status">
          <PartOpsLoader size="sm" />
          Collecting checks…
        </div>
      ) : null}

      {report ? (
        <div className="grid gap-3 lg:grid-cols-2">
          <Panel>
            <PanelHeader
              title="Database targets"
              description={
                hub.total
                  ? `${hub.healthy} / ${hub.total} enabled targets healthy`
                  : "Target registry not installed (single-database mode)"
              }
            />
            <ul className="divide-y divide-[var(--border-default)]">
              {targetRows.map((t) => (
                <li key={t.key} className="flex items-center justify-between gap-2 px-3 py-1.5 text-[12px]">
                  <span className="flex min-w-0 items-center gap-2">
                    <StatusDot tone={overallDot(t.status)} />
                    <span className="truncate">{t.label}</span>
                  </span>
                  <Badge tone={readinessStatusBadgeTone(t.status)}>
                    {t.status === "not_required" ? "DISABLED" : readinessStatusLabel(t.status)}
                  </Badge>
                </li>
              ))}
            </ul>
            <p className="px-3 py-2 text-[11px] text-[var(--content-tertiary)]">
              An unhealthy managed target degrades only its own jobs. The control database being
              unavailable affects every target.
            </p>
          </Panel>
          <Panel>
            <PanelHeader
              title="Scheduler concurrency"
              description="Global and per-target slots (limits are DBA-managed)."
            />
            <SchedulerTargetsTable scheduler={scheduler} schedulerOk={schedulerOk} targets={targets} />
          </Panel>
        </div>
      ) : null}

      {categories.map((category) => (
        <section
          key={category.key}
          className="rounded-card border border-pj-line bg-pj-card"
        >
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-pj-line px-4 py-3">
            <div className="flex items-center gap-2">
              <StatusDot tone={overallDot(category.status)} />
              <h2 className="text-[14px] font-semibold text-[var(--content-primary)]">
                {SECTION_LABEL[category.key] || category.label}
              </h2>
            </div>
            <Badge tone={readinessStatusBadgeTone(category.status)}>
              {readinessStatusLabel(category.status)}
            </Badge>
          </div>
          <ul className="divide-y divide-pj-line">
            {category.checks.map((check) => {
              const details = safeDetails(check.details);
              return (
                <li key={check.key} className="px-4 py-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-bold text-pj-ink">{check.label}</div>
                      <p className="mt-0.5 text-xs text-pj-muted">{check.summary}</p>
                      {check.remediation_hint ? (
                        <p className="mt-2 text-xs text-pj-warn">{check.remediation_hint}</p>
                      ) : null}
                      {details ? (
                        <dl className="mt-2 grid gap-1 text-[0.68rem] text-pj-muted sm:grid-cols-2">
                          {Object.entries(details).map(([k, v]) => (
                            <div key={k} className="flex gap-2">
                              <dt className="font-semibold text-pj-ink">{k}:</dt>
                              <dd className="truncate font-mono">
                                {typeof v === "object" ? JSON.stringify(v) : String(v)}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      ) : null}
                    </div>
                    <Badge tone={readinessStatusBadgeTone(check.status)}>
                      {readinessStatusLabel(check.status)}
                    </Badge>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
