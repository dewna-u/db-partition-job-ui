"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import {
  Badge,
  readinessStatusBadgeTone,
  readinessStatusLabel,
  StatusDot,
} from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";
import type { SystemReadinessReport } from "@/lib/types";

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

  return (
    <div className="pj-enter space-y-6">
      <PageHeader
        title="System readiness"
        subtitle="Read-only diagnostics across database, scheduler, host services, and runtime configuration."
        showActions={false}
      />

      <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <StatusDot
              tone={report ? overallDot(report.overall_status) : "mute"}
              className="mt-1.5"
            />
            <div>
              <div className="text-lg font-extrabold tracking-tight text-pj-ink">
                {loading && !report
                  ? "Loading diagnostics…"
                  : report?.overall_label ?? "Unknown"}
              </div>
              <p className="mt-1 text-sm text-pj-muted">
                {report?.checked_at
                  ? `Last checked ${new Date(report.checked_at).toLocaleString()}`
                  : "Run checks to validate deployment health."}
                {report?.from_cache ? " (cached)" : null}
              </p>
            </div>
          </div>
          <Button
            type="button"
            onClick={() => void load(true)}
            disabled={loading || refreshing}
          >
            {refreshing ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
            Run checks
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
                <div className="mt-1 text-xl font-extrabold tabular-nums text-pj-ink">
                  {counts[key]}
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      {loading && !report ? (
        <div className="flex items-center gap-2 text-sm text-pj-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> Collecting checks…
        </div>
      ) : null}

      {report?.categories.map((category) => (
        <section
          key={category.key}
          className="rounded-card border border-pj-line bg-pj-card shadow-card"
        >
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-pj-line px-4 py-3">
            <div className="flex items-center gap-2">
              <StatusDot tone={overallDot(category.status)} />
              <h2 className="text-sm font-extrabold text-pj-ink">{category.label}</h2>
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
