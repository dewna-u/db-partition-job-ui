"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { OctagonAlert } from "lucide-react";
import { useHub } from "@/components/hub/hub-context";
import { TargetBadge, TargetHealthBadge } from "@/components/targets/target-badges";
import { Badge, StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { JobForm, type JobFormValues } from "@/components/jobs/job-form";
import { AttentionReviewList } from "@/components/jobs/attention-review";
import { api } from "@/lib/api";
import {
  dbConfigToString,
  displayStatus,
  formatCountdown,
  formatDurationMs,
  nextRunLabel,
  operationLabel,
  parseIntervalParts,
  targetTable,
} from "@/lib/format";
import {
  availabilityText,
  friendlyError,
  healthInfo,
  isUnknownOutcomeReason,
  resolveTarget,
  targetRole,
  type FriendlyError,
} from "@/lib/targets";
import type { AttentionItem, JobLog, ManualRunResult, PartitionJob } from "@/lib/types";

export function JobDetailPanel({
  job,
  onRan,
  onUpdated,
  startInEdit = false,
  attention = [],
  onAttentionChanged,
}: {
  job: PartitionJob;
  onRan?: () => void;
  onUpdated?: () => void;
  startInEdit?: boolean;
  /** Pending missed-run items for this job (Phase 5A). */
  attention?: AttentionItem[];
  onAttentionChanged?: (message?: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<FriendlyError | null>(null);
  const { targets, scheduler, schedulerOk, health, loadHealth } = useHub();
  const target = resolveTarget(job.database_target_id, targets);
  const isRemote = Boolean(target && !target.is_control_database);
  const targetHealth = target ? health[target.target_id]?.health : null;
  const targetSched = target ? scheduler?.targets?.[target.target_key] : undefined;
  const [lastManual, setLastManual] = useState<JobLog | null>(null);
  // Phase 5E.1: open remote attempts of THIS job (null = not loaded / not installed).
  const [openAttemptState, setOpenAttemptState] = useState<string | null>(null);
  // After a 504 / unknown outcome the result of the click is not known:
  // never offer an immediate second run from this drawer.
  const [runLocked, setRunLocked] = useState(false);
  const targetId = target?.target_id;
  const targetEnabled = Boolean(target?.is_enabled);

  // Target health: cached 60 s in the hub context (no polling).
  useEffect(() => {
    if (targetId != null && targetEnabled) void loadHealth(targetId);
  }, [targetId, targetEnabled, loadHealth]);

  // Open / unresolved remote execution attempts of this job (one request per open).
  useEffect(() => {
    let alive = true;
    api
      .attempts({ jobId: job.job_id, open: true, limit: 20 })
      .then((res) => {
        const states = (res.attempts || []).map((a) => String(a.state).toUpperCase());
        const blocking =
          states.find((s) => s === "OUTCOME_UNKNOWN") ||
          states.find((s) => s === "DISPATCHING" || s === "TARGET_SUCCEEDED") ||
          null;
        if (alive) setOpenAttemptState(blocking);
      })
      .catch(() => {
        if (alive) setOpenAttemptState(null);
      });
    return () => {
      alive = false;
    };
  }, [job.job_id]);

  // Latest MANUAL_* result, shown separately from the scheduled status.
  useEffect(() => {
    let alive = true;
    api
      .logs(200)
      .then((res) => {
        const row = (res.logs || []).find(
          (l) =>
            l.job_id === job.job_id &&
            String(l.last_run_status || "").toUpperCase().startsWith("MANUAL_"),
        );
        if (alive) setLastManual(row || null);
      })
      .catch(() => {
        if (alive) setLastManual(null);
      });
    return () => {
      alive = false;
    };
  }, [job.job_id]);
  const [confirmDrop, setConfirmDrop] = useState(false);
  const [editing, setEditing] = useState(startInEdit);
  const status = displayStatus(job);
  const op = operationLabel(job);

  const formInitial = useMemo((): JobFormValues => {
    const freq = parseIntervalParts(job.frequency) || { amount: 1, unit: "day" };
    const interval =
      parseIntervalParts(job.create_drop_interval) || { amount: 1, unit: "month" };
    const next = job.next_run_time
      ? String(job.next_run_time).replace(" ", "T").slice(0, 19)
      : "";
    return {
      job_name: job.job_name || "",
      is_enabled: Boolean(job.is_enabled),
      is_create: Boolean(job.is_create),
      table_schema: job.table_schema || "",
      table_name: job.table_name || "",
      job_schedule: job.job_schedule || "0 0 2 * * *",
      frequency_amount: freq.amount,
      frequency_unit: freq.unit,
      auto_next_run: true,
      next_run_time: next,
      partition_unit: String(job.partition_unit || "day"),
      partition_period: Number(job.partition_period || 1),
      create_drop_amount: interval.amount,
      create_drop_unit: interval.unit,
      db_config: dbConfigToString(job.db_config_para),
      database_target_id: job.database_target_id ?? null,
    };
  }, [job]);

  async function runNow() {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const res = await api.runJob(job.job_id, op === "DROP" ? confirmDrop : false);
      const result = (res.result || {}) as ManualRunResult;
      setMessage(
        result.status === "MANUAL_FINALIZE_PENDING"
          ? "The target committed the operation; history is still being recorded (it will not run again)."
          : res.message,
      );
      setConfirmDrop(false);
      onRan?.();
    } catch (err) {
      const f = friendlyError(err, "run");
      setError(f);
      if (f.kind === "unknown") setRunLocked(true);
      onRan?.();
    } finally {
      setBusy(false);
    }
  }

  const unknownPending = attention.some((a) => isUnknownOutcomeReason(a.reason));
  const runBlockReason: string | null =
    runLocked
      ? "The result of the last run request is not known (no answer / outcome not confirmed). Check what happened before doing anything else."
      : openAttemptState === "OUTCOME_UNKNOWN" || unknownPending
        ? "A previous execution of this job has an unknown outcome. Inspect the target and record the inspection result first."
        : openAttemptState === "DISPATCHING"
          ? "An execution of this job may be running on the target right now."
          : openAttemptState === "TARGET_SUCCEEDED"
            ? "An execution of this job committed on the target and is still being recorded."
            : null;

  if (editing) {
    return (
      <div className="space-y-4">
        <div>
          <div className="text-[0.62rem] font-semibold uppercase tracking-[0.13em] text-[var(--content-tertiary)]">
            Edit partition job
          </div>
          <div className="text-base font-semibold tracking-tight">Job #{job.job_id}</div>
        </div>
        <JobForm
          mode="edit"
          jobId={job.job_id}
          initial={formInitial}
          baseline={formInitial}
          submitLabel="Save changes"
          onCancel={() => setEditing(false)}
          onSuccess={() => {
            setEditing(false);
            onUpdated?.();
          }}
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <div className="text-[0.62rem] font-semibold uppercase tracking-[0.13em] text-[var(--content-tertiary)]">
          Selected job · #{job.job_id}
        </div>
        <div className="text-base font-semibold tracking-tight">Job details</div>
      </div>

      {attention.length ? (
        <section className="space-y-2" aria-label="Missed runs needing review">
          <div className="text-[0.62rem] font-semibold uppercase tracking-[0.13em] text-[var(--warning-fg)]">
            Needs review · {attention.length} item{attention.length === 1 ? "" : "s"}
          </div>
          <AttentionReviewList items={attention} onChanged={onAttentionChanged} />
        </section>
      ) : null}

      <div className="flex items-center gap-3 rounded-[6px] border border-[var(--border-default)] bg-[var(--info-bg)] px-3 py-3">
        <div className="grid h-9 w-9 place-items-center rounded-lg bg-[var(--client-primary)] text-[0.7rem] font-semibold text-white">
          SQL
        </div>
        <div>
          <div className="text-sm font-semibold">{job.job_name || `Job ${job.job_id}`}</div>
          <div className="text-[0.65rem] text-pj-muted">
            Parameterized partition routine
          </div>
        </div>
      </div>

      <section
        aria-label="Database target"
        className="rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-primary)] px-3 py-2"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-[var(--content-secondary)]">Database target</span>
          <TargetBadge target={target} fallbackId={job.database_target_id ?? null} />
          <span className="text-[12px] font-medium">{target?.display_name || "Unknown target"}</span>
          <span className="text-[11px] text-[var(--content-tertiary)]">{targetRole(target)}</span>
          <span className="ml-auto">
            <TargetHealthBadge
              state={target && !target.is_enabled ? "DISABLED" : targetHealth?.state}
            />
          </span>
        </div>
        {target &&
        (!target.is_enabled ||
          (targetHealth && !healthInfo(targetHealth.state).usable) ||
          (targetSched && !targetSched.available)) ? (
          <p
            className="mt-1.5 rounded-[4px] border border-[var(--warning-fg)] bg-[var(--warning-bg)] px-2 py-1.5 text-[11px] text-[var(--content-primary)]"
            role="status"
          >
            <span className="font-semibold">
              {target.target_key.toUpperCase()} is currently{" "}
              {!target.is_enabled
                ? "disabled"
                : targetSched && !targetSched.available
                  ? "unreachable (backing off)"
                  : healthInfo(targetHealth?.state).label.toLowerCase()}
              .
            </span>{" "}
            Scheduled execution of this job waits in isolation on this target and has not been
            executed while waiting.{" "}
            {target.is_control_database
              ? "The control database also holds every job's schedule, so all targets are affected."
              : "Control-database jobs and other targets are unaffected."}
            {targetSched && !targetSched.available ? ` ${availabilityText(targetSched)}.` : ""}
          </p>
        ) : null}
      </section>

      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-bold">
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
        <Badge tone={op === "CREATE" ? "create" : "drop"}>{op}</Badge>
      </div>

      <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
        {[
          ["Schema.table", targetTable(job)],
          ["Type", op],
          ["Enabled", job.is_enabled ? "Yes" : "No"],
          ["Schedule", job.job_schedule || "—"],
          [
            "Next execution",
            `${job.next_run_time || "—"} (${formatCountdown(job.next_run_time)})`,
          ],
          ["Partition unit", String(job.partition_unit || "—")],
          ["Partition period", String(job.partition_period ?? "—")],
          ["Interval", String(job.create_drop_interval || "—")],
          ["Last scheduled run", String(job.last_run_time || "—")],
          ["Last scheduled status", String(job.last_run_status || "—")],
          [
            "Latest manual run",
            lastManual
              ? `${lastManual.last_run_status} · ${lastManual.job_runtime || "—"}`
              : "None in recent history",
          ],
          ["Last duration", formatDurationMs(job.last_execution_duration_ms)],
          [
            "Average duration",
            job.avg_execution_duration_ms != null
              ? formatDurationMs(job.avg_execution_duration_ms)
              : "—",
          ],
          ["Relative", nextRunLabel(job)],
        ].map(([k, v]) => (
          <div key={k} className="rounded-lg border border-pj-line bg-pj-surface px-3 py-2">
            <dt className="text-[0.58rem] font-bold uppercase tracking-[0.1em] text-[var(--content-tertiary)]">
              {k}
            </dt>
            <dd className="mt-1 break-all font-semibold text-pj-ink">{v}</dd>
          </div>
        ))}
      </dl>

      <pre className="whitespace-pre-wrap rounded-[6px] border border-[var(--border-default)] bg-[var(--info-bg)] p-3 font-mono text-[0.72rem] leading-relaxed text-[var(--info-fg)]">
        {`${op} partition job\n${targetTable(job)}\npartition unit: ${job.partition_unit}\nperiod: ${job.partition_period}\ninterval: ${job.create_drop_interval}\nschedule: ${job.job_schedule}`}
      </pre>

      <div className="space-y-2 border-t border-pj-line pt-3">
        <div className="text-[0.62rem] font-semibold uppercase tracking-[0.13em] text-[var(--content-tertiary)]">
          Actions
        </div>
        {op === "DROP" ? (
          <label className="flex items-start gap-2 text-xs text-[var(--warning-fg)]">
            <input
              type="checkbox"
              checked={confirmDrop}
              onChange={(e) => setConfirmDrop(e.target.checked)}
              className="mt-0.5"
            />
            I understand this DROP may permanently remove partition data.
          </label>
        ) : null}
        {runBlockReason ? (
          <p
            role="status"
            className="rounded-[6px] border border-[var(--error-fg)] bg-[var(--error-bg)] px-2.5 py-2 text-[11px] text-[var(--content-primary)]"
          >
            <span className="font-semibold text-[var(--error-fg)]">Run now is unavailable.</span>{" "}
            {runBlockReason}{" "}
            <Link href="/history" className="font-medium text-[var(--content-link)] underline">
              Open History → Remote execution attempts
            </Link>
          </p>
        ) : null}
        <Button
          variant={op === "DROP" ? "danger" : "success"}
          disabled={busy || Boolean(runBlockReason) || (op === "DROP" && !confirmDrop)}
          onClick={() => void runNow()}
          className="w-full"
          aria-busy={busy}
        >
          {busy ? (
            <>
              <PartOpsLoader size="sm" />
              Running…
            </>
          ) : (
            "Run now"
          )}
        </Button>
        <Button
          variant="secondary"
          className="w-full"
          onClick={() => setEditing(true)}
        >
          Edit configuration
        </Button>
        {message ? (
          <p className="text-xs font-semibold text-[var(--success-fg)]" role="status">
            {message}
          </p>
        ) : null}
        {error ? (
          <div
            role="alert"
            className={
              error.kind === "unknown"
                ? "rounded-[6px] border-2 border-[var(--error-fg)] bg-[var(--error-bg)] px-2.5 py-2 text-[12px]"
                : "text-xs text-[var(--error-fg)]"
            }
          >
            <div className="flex items-center gap-1.5 font-semibold text-[var(--error-fg)]">
              {error.kind === "unknown" ? <OctagonAlert className="h-3.5 w-3.5" aria-hidden /> : null}
              {error.title}
            </div>
            <div className="mt-0.5 text-[var(--content-primary)]">{error.detail}</div>
          </div>
        ) : null}
        <p className="text-[0.65rem] text-pj-muted">
          {isRemote
            ? `Runs on ${target?.target_key.toUpperCase()} through the scheduler (same locks, limits and safety checks as scheduled runs). Requires the scheduler to be running${schedulerOk ? "" : " — it is not reporting right now"}. Does not change next_run_time.`
            : "Runs on the control database via run_partition_job_manual(). Does not change next_run_time."}
        </p>
      </div>
    </div>
  );
}
