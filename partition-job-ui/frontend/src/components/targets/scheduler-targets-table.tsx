"use client";

import { StatusDot } from "@/components/ui/badge";
import { TargetBadge } from "@/components/targets/target-badges";
import { availabilityText } from "@/lib/targets";
import type { DatabaseTarget, SchedulerStatus } from "@/lib/types";

/** Global + per-target concurrency (read-only; limits are DBA-managed). */
export function SchedulerTargetsTable({
  scheduler,
  schedulerOk,
  targets,
}: {
  scheduler: SchedulerStatus | null;
  schedulerOk: boolean;
  targets: DatabaseTarget[] | null;
}) {
  if (!schedulerOk || !scheduler) {
    return (
      <p className="px-3 py-3 text-[12px] text-[var(--content-secondary)]">
        Scheduler is not reporting — per-target concurrency is unavailable. No data is guessed.
      </p>
    );
  }
  const per = scheduler.targets || {};
  const byKey = new Map((targets || []).map((t) => [t.target_key, t]));
  const keys = Object.keys(per).sort();
  const globalMax = scheduler.global_max ?? scheduler.max_concurrent_jobs;
  const globalActive = scheduler.global_active ?? scheduler.active_job_count;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[12px]">
        <caption className="sr-only">Scheduler concurrency per database target</caption>
        <thead className="bg-[var(--surface-tertiary)] text-[11px] text-[var(--content-secondary)]">
          <tr>
            <th className="px-3 py-1.5 font-medium">Scope</th>
            <th className="px-3 py-1.5 font-medium">Active</th>
            <th className="px-3 py-1.5 font-medium">Waiting</th>
            <th className="px-3 py-1.5 font-medium">Limit</th>
            <th className="px-3 py-1.5 font-medium">Availability</th>
          </tr>
        </thead>
        <tbody>
          <tr className="border-t border-[var(--border-default)]">
            <td className="px-3 py-1.5 font-medium">Global</td>
            <td className="px-3 py-1.5 tabular-nums">{globalActive ?? "—"}</td>
            <td className="px-3 py-1.5 tabular-nums text-[var(--content-tertiary)]">—</td>
            <td className="px-3 py-1.5 tabular-nums">{globalMax ?? "—"} max</td>
            <td className="px-3 py-1.5 text-[var(--content-secondary)]">
              {scheduler.scheduler_active ? "Scheduler online" : "Scheduler idle"}
            </td>
          </tr>
          {keys.map((key) => {
            const s = per[key];
            const t = byKey.get(key) || null;
            return (
              <tr key={key} className="border-t border-[var(--border-default)]">
                <td className="px-3 py-1.5">
                  <TargetBadge
                    target={t || { target_key: key, is_control_database: s.is_control_database, display_name: key }}
                  />
                </td>
                <td className="px-3 py-1.5 tabular-nums">{s.active_jobs}</td>
                <td className="px-3 py-1.5 tabular-nums">{s.waiting_jobs}</td>
                <td className="px-3 py-1.5 tabular-nums">
                  {s.max_concurrent_jobs != null ? `${s.max_concurrent_jobs} max` : "—"}
                </td>
                <td className="px-3 py-1.5">
                  <span className="inline-flex items-center gap-1.5">
                    <StatusDot tone={!s.is_enabled ? "mute" : s.available ? "green" : "amber"} />
                    <span className={s.available ? "text-[var(--content-secondary)]" : "text-[var(--warning-fg)]"}>
                      {availabilityText(s)}
                    </span>
                  </span>
                </td>
              </tr>
            );
          })}
          {!keys.length ? (
            <tr className="border-t border-[var(--border-default)]">
              <td colSpan={5} className="px-3 py-2 text-[var(--content-tertiary)]">
                No per-target data yet (scheduler starting or pre-5D build).
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
