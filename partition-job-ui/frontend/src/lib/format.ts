import type { PartitionJob } from "./types";

const FAIL = new Set([
  "FAIL",
  "MANUAL_FAIL",
  "ERROR",
  "FAILED",
  "FAILED_CONNECTION",
]);
const SUCCESS = new Set(["SUCCESS", "MANUAL_SUCCESS"]);

export function targetTable(job: PartitionJob): string {
  const schema = job.table_schema || "";
  const table = job.table_name || "";
  if (schema && table) return `${schema}.${table}`;
  return table || schema || "—";
}

export function operationLabel(job: PartitionJob): "CREATE" | "DROP" {
  return job.is_create ? "CREATE" : "DROP";
}

export function displayStatus(job: PartitionJob): { label: string; tone: string } {
  if (!job.is_enabled) return { label: "Disabled", tone: "mute" };
  const status = (job.last_run_status || "").toUpperCase();
  const next = job.next_run_time ? new Date(job.next_run_time) : null;
  const now = Date.now();
  if (FAIL.has(status)) return { label: "Failed", tone: "red" };
  if (next && next.getTime() < now - 5 * 60 * 1000)
    return { label: "Needs review", tone: "amber" };
  if (next && next.getTime() >= now) return { label: "Scheduled", tone: "blue" };
  if (SUCCESS.has(status)) return { label: "Completed", tone: "green" };
  if (!next) return { label: "Paused", tone: "amber" };
  return { label: status || "Unknown", tone: "mute" };
}

export function formatCountdown(target?: string | null): string {
  if (!target) return "—";
  const dt = new Date(target);
  if (Number.isNaN(dt.getTime())) return "—";
  const secs = Math.floor((dt.getTime() - Date.now()) / 1000);
  if (secs < 0) {
    const overdue = Math.abs(secs);
    if (overdue < 60) return `Overdue ${overdue}s`;
    if (overdue < 3600) return `Overdue ${Math.floor(overdue / 60)}m`;
    return `Overdue ${Math.floor(overdue / 3600)}h`;
  }
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) {
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return `${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m`;
  }
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  return `${d}d ${String(h).padStart(2, "0")}h`;
}

export function nextRunLabel(job: PartitionJob): string {
  if (!job.is_enabled) return "Paused";
  if (!job.next_run_time) return "—";
  const label = formatCountdown(job.next_run_time);
  if (label.startsWith("Overdue")) return label;
  return `in ${label}`;
}

export function formatAge(moment?: string | null): string {
  if (!moment) return "—";
  const dt = new Date(moment);
  if (Number.isNaN(dt.getTime())) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export function formatWhen(moment?: string | null): string {
  if (!moment) return "—";
  const dt = new Date(moment);
  if (Number.isNaN(dt.getTime())) return String(moment);
  const today = new Date();
  const sameDay =
    dt.getFullYear() === today.getFullYear() &&
    dt.getMonth() === today.getMonth() &&
    dt.getDate() === today.getDate();
  const time = dt.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  if (sameDay) return `Today, ${time}`;
  return `${dt.toLocaleDateString()} ${time}`;
}

export function isFailStatus(status?: string | null): boolean {
  return FAIL.has((status || "").toUpperCase());
}

export function isSuccessStatus(status?: string | null): boolean {
  return SUCCESS.has((status || "").toUpperCase());
}

/** Format stored execution_duration_ms for display. NULL → em dash. */
export function formatDurationMs(value?: number | null): string {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const ms = Math.trunc(Number(value));
  if (ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) {
    if (seconds < 10) {
      const text = seconds.toFixed(2).replace(/\.?0+$/, "");
      return `${text} s`;
    }
    const text = seconds.toFixed(1).replace(/\.0$/, "");
    return `${text} s`;
  }
  const totalSecs = Math.round(seconds);
  const minutes = Math.floor(totalSecs / 60);
  const rem = totalSecs % 60;
  return `${minutes}m ${String(rem).padStart(2, "0")}s`;
}

/** Parse PostgreSQL-ish interval like "2 months" → { amount, unit }. */
export function parseIntervalParts(
  value?: string | null,
): { amount: number; unit: string } | null {
  if (!value) return null;
  const match = String(value)
    .trim()
    .match(
      /^(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)$/i,
    );
  if (!match) return null;
  const amount = Number(match[1]);
  const unit = match[2].toLowerCase().replace(/s$/, "");
  if (!amount || amount < 1) return null;
  return { amount, unit };
}

export function dbConfigToString(value: unknown): string {
  if (value == null) return "{}";
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "{}";
  }
}

