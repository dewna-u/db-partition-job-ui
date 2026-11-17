"use client";

import { useEffect, useMemo, useState } from "react";
import { Info, RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { useHub } from "@/components/hub/hub-context";
import { Badge, StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader, QueryState } from "@/components/ui/panel";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { Sheet } from "@/components/ui/sheet";
import { SchedulerTargetsTable } from "@/components/targets/scheduler-targets-table";
import { TargetBadge, TargetHealthBadge } from "@/components/targets/target-badges";
import {
  availabilityText,
  errorClassLabel,
  healthInfo,
  safeText,
  targetRole,
} from "@/lib/targets";
import { formatWhen } from "@/lib/format";
import type { DatabaseTarget, PerTargetSchedulerStatus, TargetHealth } from "@/lib/types";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[140px_1fr] gap-2 border-t border-[var(--border-default)] py-1.5 first:border-t-0">
      <dt className="text-[11px] text-[var(--content-secondary)]">{label}</dt>
      <dd className="min-w-0 break-words text-[12px] text-[var(--content-primary)]">{children}</dd>
    </div>
  );
}

function concurrencyText(t: DatabaseTarget, s?: PerTargetSchedulerStatus) {
  const max = s?.max_concurrent_jobs ?? t.max_concurrent_jobs;
  return s ? `${s.active_jobs} active / ${max} max` : `${max} max`;
}

export default function TargetsPage() {
  const {
    targets,
    targetsAvailable,
    targetsError,
    scheduler,
    schedulerOk,
    health,
    healthLoading,
    loadHealth,
    refreshTargets,
    refreshScheduler,
  } = useHub();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // One health check per target on first visit (cached 60 s); no polling.
  useEffect(() => {
    for (const t of targets || []) {
      if (t.is_enabled) void loadHealth(t.target_id);
    }
  }, [targets, loadHealth]);

  async function refreshAll() {
    setRefreshing(true);
    try {
      await Promise.all([refreshTargets(), refreshScheduler()]);
      await Promise.all(
        (targets || []).filter((t) => t.is_enabled).map((t) => loadHealth(t.target_id, true)),
      );
    } finally {
      setRefreshing(false);
    }
  }

  const per = scheduler?.targets || {};
  const selected = useMemo(
    () => (targets || []).find((t) => t.target_id === selectedId) || null,
    [targets, selectedId],
  );
  const enabled = (targets || []).filter((t) => t.is_enabled);
  const healthyCount = enabled.filter((t) => healthInfo(health[t.target_id]?.health?.state).usable).length;

  return (
    <div className="pj-enter space-y-3">
      <PageHeader
        title="Database targets"
        count={targets ? `${healthyCount} / ${enabled.length} healthy` : undefined}
        subtitle="Databases managed by this hub's PartOps. Read-only operational view."
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void refreshAll()}
            disabled={refreshing}
            aria-busy={refreshing}
          >
            {refreshing ? <PartOpsLoader size="sm" /> : <RefreshCw className="h-3.5 w-3.5" aria-hidden />}
            {refreshing ? "Checking targets…" : "Refresh health"}
          </Button>
        }
      />

      <div
        className="flex items-start gap-2 rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-primary)] px-3 py-2 text-[11px] text-[var(--content-secondary)]"
        role="note"
      >
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--info-fg)]" aria-hidden />
        <span>
          Target registration, credentials, enable / disable and concurrency limits are managed by a
          DBA through the controlled deployment procedure (RUNBOOK §17), not through this UI. PartOps
          has no UI authentication, so registry changes are intentionally not available here.
        </span>
      </div>

      <Panel className="overflow-hidden">
        <QueryState
          loading={targets === null && !targetsError}
          error={targetsError && !targetsAvailable ? targetsError : null}
          empty={targets !== null && targets.length === 0}
          emptyMessage="No database targets registered."
          loadingLabel="Loading targets…"
        >
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <caption className="sr-only">Database targets of this hub</caption>
              <thead className="bg-[var(--surface-tertiary)] text-[11px] text-[var(--content-secondary)]">
                <tr>
                  <th className="px-3 py-2 font-medium">Target</th>
                  <th className="px-3 py-2 font-medium">Database</th>
                  <th className="px-3 py-2 font-medium">Environment</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Framework</th>
                  <th className="px-3 py-2 font-medium">Concurrency</th>
                  <th className="px-3 py-2 font-medium">Waiting</th>
                </tr>
              </thead>
              <tbody>
                {(targets || []).map((t) => {
                  const h = health[t.target_id]?.health;
                  const hErr = health[t.target_id]?.error;
                  const loading = healthLoading[t.target_id];
                  const s = per[t.target_key];
                  const fw = h?.framework;
                  return (
                    <tr
                      key={t.target_id}
                      className="cursor-pointer border-t border-[var(--border-default)] hover:bg-[var(--hover-surface-1)]"
                      onClick={() => setSelectedId(t.target_id)}
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelectedId(t.target_id);
                        }
                      }}
                      aria-label={`Open ${t.display_name}`}
                    >
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <TargetBadge target={t} />
                          <span className="font-medium text-[var(--content-primary)]">{t.display_name}</span>
                        </div>
                      </td>
                      <td className="max-w-[180px] truncate px-3 py-2 font-mono text-[11px]" title={t.database_name}>
                        {t.database_name}
                      </td>
                      <td className="px-3 py-2 text-[var(--content-secondary)]">{t.environment || "—"}</td>
                      <td className="px-3 py-2 text-[var(--content-secondary)]">{targetRole(t)}</td>
                      <td className="px-3 py-2">
                        {!t.is_enabled ? (
                          <TargetHealthBadge state="DISABLED" />
                        ) : loading && !h ? (
                          <PartOpsLoader size="sm" label="Checking" />
                        ) : h ? (
                          <TargetHealthBadge state={h.state} />
                        ) : hErr ? (
                          <Badge tone="neutral" className="normal-case tracking-normal">Check failed</Badge>
                        ) : (
                          <span className="text-[var(--content-tertiary)]">—</span>
                        )}
                        {s && !s.available && t.is_enabled ? (
                          <div className="mt-0.5 text-[10.5px] text-[var(--warning-fg)]">Backing off</div>
                        ) : null}
                      </td>
                      <td className="px-3 py-2 tabular-nums text-[var(--content-secondary)]">
                        {fw
                          ? `${fw.counts.FAIL ?? 0} fail · ${fw.counts.WARNING ?? 0} warn`
                          : "—"}
                      </td>
                      <td className="px-3 py-2 tabular-nums">{concurrencyText(t, s)}</td>
                      <td className="px-3 py-2 tabular-nums">{s ? s.waiting_jobs : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </QueryState>
      </Panel>

      <Panel>
        <PanelHeader
          title="Scheduler concurrency"
          description="Global and per-target slots. A target waits for its own slot before taking a global one, so a busy target never blocks the others."
        />
        <SchedulerTargetsTable scheduler={scheduler} schedulerOk={schedulerOk} targets={targets} />
      </Panel>

      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => !open && setSelectedId(null)}
        title={selected ? `${selected.display_name}` : "Target"}
        description={selected ? `${targetRole(selected)} · read-only` : undefined}
        wide
      >
        {selected ? (
          <TargetDetail
            target={selected}
            health={health[selected.target_id]?.health || null}
            healthError={health[selected.target_id]?.error || null}
            loading={Boolean(healthLoading[selected.target_id])}
            sched={per[selected.target_key]}
            schedulerOk={schedulerOk}
            onRefresh={() => void loadHealth(selected.target_id, true)}
          />
        ) : null}
      </Sheet>
    </div>
  );
}

function TargetDetail({
  target,
  health,
  healthError,
  loading,
  sched,
  schedulerOk,
  onRefresh,
}: {
  target: DatabaseTarget;
  health: TargetHealth | null;
  healthError: string | null;
  loading: boolean;
  sched?: PerTargetSchedulerStatus;
  schedulerOk: boolean;
  onRefresh: () => void;
}) {
  const state = target.is_enabled ? health?.state : "DISABLED";
  const info = healthInfo(state);
  const problems = health?.framework?.problems || [];
  const failures = problems.filter((p) => p.status === "FAIL");
  const warnings = problems.filter((p) => p.status === "WARNING");

  return (
    <div className="space-y-4" aria-busy={loading}>
      <section
        className={`rounded-[6px] border px-3 py-2.5 ${
          info.tone === "error"
            ? "border-[var(--error-fg)] bg-[var(--error-bg)]"
            : info.tone === "warning"
              ? "border-[var(--warning-fg)] bg-[var(--warning-bg)]"
              : "border-[var(--border-default)] bg-[var(--surface-tertiary)]"
        }`}
        aria-live="polite"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <TargetHealthBadge state={state} />
            <span className="text-[12px] text-[var(--content-secondary)]">
              {health?.checked_at ? `Checked ${formatWhen(health.checked_at, { naiveUtc: true })}` : "Not checked yet"}
            </span>
          </div>
          {target.is_enabled ? (
            <Button size="sm" variant="secondary" onClick={onRefresh} disabled={loading} aria-busy={loading}>
              {loading ? <PartOpsLoader size="sm" /> : <RefreshCw className="h-3.5 w-3.5" aria-hidden />}
              {loading ? "Checking…" : "Check health"}
            </Button>
          ) : null}
        </div>
        <p className="mt-1.5 text-[12px] text-[var(--content-primary)]">{info.meaning}</p>
        {health?.detail && info.tone !== "success" ? (
          <p className="mt-1 font-mono text-[11px] text-[var(--content-secondary)]">{safeText(health.detail)}</p>
        ) : null}
        {healthError ? (
          <p className="mt-1 text-[11px] text-[var(--error-fg)]">Health check failed: {safeText(healthError)}</p>
        ) : null}
      </section>

      <dl>
        <Row label="Target key"><span className="font-mono">{target.target_key}</span></Row>
        <Row label="Display name">{target.display_name}</Row>
        <Row label="Database"><span className="font-mono">{target.database_name}</span></Row>
        <Row label="Environment">{target.environment || "—"}</Row>
        <Row label="Role">{targetRole(target)}</Row>
        <Row label="Enabled">{target.is_enabled ? "Yes" : "No (DBA-disabled)"}</Row>
        <Row label="Credentials">
          {target.credential_provider === "inherit"
            ? "Same as PartOps control connection"
            : target.credential_provider === "aws"
              ? `Own AWS secret ${target.secret_configured ? "(configured)" : "(missing)"}`
              : "Environment (local development only)"}
        </Row>
        <Row label="Max concurrency">{target.max_concurrent_jobs} (DBA-managed)</Row>
        <Row label="Active / waiting">
          {sched ? `${sched.active_jobs} active · ${sched.waiting_jobs} waiting` : schedulerOk ? "—" : "Scheduler not reporting"}
        </Row>
        <Row label="Availability">
          <span className={sched && !sched.available ? "text-[var(--warning-fg)]" : undefined}>
            {availabilityText(sched)}
          </span>
        </Row>
        <Row label="Last error category">{errorClassLabel(sched?.last_error_class)}</Row>
        {health?.framework ? (
          <Row label="Framework version">
            {target.is_control_database
              ? `${health.framework.database_version || "unknown"} (expects ${health.framework.expected_version || "—"})`
              : "Not applicable (managed targets only need the partition workers)"}
          </Row>
        ) : null}
      </dl>

      {failures.length ? (
        <section aria-label="Framework failures">
          <h3 className="mb-1 text-[12px] font-semibold text-[var(--error-fg)]">
            Blocking framework problems ({failures.length})
          </h3>
          <ul className="space-y-1">
            {failures.map((p) => (
              <li key={`${p.object}-${p.check}`} className="rounded-[4px] border border-[var(--error-fg)] px-2 py-1 text-[11px]">
                <div className="truncate font-mono" title={p.object}>{p.object}</div>
                <div className="text-[var(--content-secondary)]">{p.check}: {safeText(p.detail)}</div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {warnings.length ? (
        <section aria-label="Framework warnings">
          <h3 className="mb-1 text-[12px] font-semibold text-[var(--warning-fg)]">
            Warnings — not blocking ({warnings.length})
          </h3>
          <ul className="space-y-1">
            {warnings.map((p) => (
              <li key={`${p.object}-${p.check}`} className="rounded-[4px] border border-[var(--border-default)] px-2 py-1 text-[11px]">
                <div className="truncate font-mono" title={p.object}>{p.object}</div>
                <div className="text-[var(--content-secondary)]">{p.check}: {safeText(p.detail)}</div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <p className="flex items-center gap-1.5 text-[11px] text-[var(--content-tertiary)]">
        <StatusDot tone="mute" /> Changes to this target are made by a DBA (RUNBOOK §17); this page is read-only.
      </p>
    </div>
  );
}
