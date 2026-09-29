"use client";

import { useMemo, useState } from "react";
import { Badge, StatusDot } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { JobForm, type JobFormValues } from "@/components/jobs/job-form";
import { api, ApiError } from "@/lib/api";
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
import type { PartitionJob } from "@/lib/types";

export function JobDetailPanel({
  job,
  onRan,
  onUpdated,
  startInEdit = false,
}: {
  job: PartitionJob;
  onRan?: () => void;
  onUpdated?: () => void;
  startInEdit?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
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
    };
  }, [job]);

  async function runNow() {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const res = await api.runJob(job.job_id, op === "DROP" ? confirmDrop : false);
      setMessage(res.message);
      onRan?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Manual run failed");
    } finally {
      setBusy(false);
    }
  }

  if (editing) {
    return (
      <div className="space-y-4">
        <div>
          <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
            Edit partition job
          </div>
          <div className="text-base font-extrabold tracking-tight">Job #{job.job_id}</div>
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
        <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
          Selected job · #{job.job_id}
        </div>
        <div className="text-base font-extrabold tracking-tight">Job details</div>
      </div>

      <div className="flex items-center gap-3 rounded-[10px] border border-[#d9e1f5] bg-[#f1f5ff] px-3 py-3">
        <div className="grid h-9 w-9 place-items-center rounded-lg bg-[#165dff] text-[0.7rem] font-extrabold text-white">
          SQL
        </div>
        <div>
          <div className="text-sm font-extrabold">{job.job_name || `Job ${job.job_id}`}</div>
          <div className="text-[0.65rem] text-pj-muted">
            Parameterized partition routine
          </div>
        </div>
      </div>

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
          ["Target table", targetTable(job)],
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
          ["Last run", String(job.last_run_time || "—")],
          ["Last status", String(job.last_run_status || "—")],
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
            <dt className="text-[0.58rem] font-bold uppercase tracking-[0.1em] text-[#8a958e]">
              {k}
            </dt>
            <dd className="mt-1 break-all font-semibold text-pj-ink">{v}</dd>
          </div>
        ))}
      </dl>

      <pre className="whitespace-pre-wrap rounded-xl border border-[#cbd7ef] bg-[#eef3ff] p-3 font-mono text-[0.72rem] leading-relaxed text-[#40526f]">
        {`${op} partition job\n${targetTable(job)}\npartition unit: ${job.partition_unit}\nperiod: ${job.partition_period}\ninterval: ${job.create_drop_interval}\nschedule: ${job.job_schedule}`}
      </pre>

      <div className="space-y-2 border-t border-pj-line pt-3">
        <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
          Actions
        </div>
        {op === "DROP" ? (
          <label className="flex items-start gap-2 text-xs text-[#8a4b16]">
            <input
              type="checkbox"
              checked={confirmDrop}
              onChange={(e) => setConfirmDrop(e.target.checked)}
              className="mt-0.5"
            />
            I understand this DROP may permanently remove partition data.
          </label>
        ) : null}
        <Button
          variant={op === "DROP" ? "danger" : "success"}
          disabled={busy || (op === "DROP" && !confirmDrop)}
          onClick={() => void runNow()}
          className="w-full"
        >
          {busy ? "Running…" : "Run now"}
        </Button>
        <Button
          variant="secondary"
          className="w-full"
          onClick={() => setEditing(true)}
        >
          Edit configuration
        </Button>
        {message ? <p className="text-xs font-semibold text-[#1f8a64]">{message}</p> : null}
        {error ? <p className="text-xs font-semibold text-[#c4473a]">{error}</p> : null}
        <p className="text-[0.65rem] text-pj-muted">
          Uses existing run_partition_job_manual(). Does not change next_run_time.
        </p>
      </div>
    </div>
  );
}
