/**
 * Phase 5E — single source of truth for target / attempt / review vocabulary.
 *
 * One PartOps instance per hub: the UI only ever represents THIS hub's targets
 * (control database + its managed databases). No hub selector, no fleet view.
 */
import { ApiError } from "./api";
import type {
  AttemptState,
  DatabaseTarget,
  PerTargetSchedulerStatus,
  SystemReadinessReport,
  TargetHealthState,
} from "./types";

export type Tone = "success" | "warning" | "error" | "info" | "neutral";

// ---------------------------------------------------------------------------
// Target health
// ---------------------------------------------------------------------------
export const TARGET_HEALTH: Record<
  TargetHealthState,
  { label: string; tone: Tone; meaning: string; usable: boolean }
> = {
  HEALTHY: {
    label: "Healthy",
    tone: "success",
    meaning: "Connected, identity verified, required framework present.",
    usable: true,
  },
  HEALTHY_WITH_WARNINGS: {
    label: "Healthy with warnings",
    tone: "warning",
    meaning:
      "Usable. Warnings only — e.g. legacy worker definitions are not yet captured in Git (Phase 5B.1 deferred). Not a failure.",
    usable: true,
  },
  DISABLED: {
    label: "Disabled",
    tone: "neutral",
    meaning: "Disabled in the registry by a DBA. Not contacted; no jobs run here.",
    usable: false,
  },
  UNREACHABLE: {
    label: "Unreachable",
    tone: "error",
    meaning: "PartOps could not connect. Jobs of this target wait; other targets are unaffected.",
    usable: false,
  },
  AUTH_FAILED: {
    label: "Authentication failed",
    tone: "error",
    meaning: "The credentials for this target were rejected. Check its secret (DBA).",
    usable: false,
  },
  WRONG_DATABASE: {
    label: "Wrong database",
    tone: "error",
    meaning:
      "The connection reached a different database than the registry names. PartOps never runs anything there.",
    usable: false,
  },
  FRAMEWORK_INCOMPATIBLE: {
    label: "Framework incompatible",
    tone: "error",
    meaning:
      "Required framework objects are missing or not executable (e.g. partition workers). Execution is blocked.",
    usable: false,
  },
  CONFIG_ERROR: {
    label: "Configuration error",
    tone: "error",
    meaning: "The registry row or credential configuration is invalid (DBA).",
    usable: false,
  },
};

export function healthInfo(state?: string | null) {
  const key = (state || "").toUpperCase() as TargetHealthState;
  return (
    TARGET_HEALTH[key] || {
      label: state ? state.replace(/_/g, " ").toLowerCase() : "Not checked",
      tone: "neutral" as Tone,
      meaning: state ? "Unrecognised health state." : "Health has not been checked yet.",
      usable: false,
    }
  );
}

// ---------------------------------------------------------------------------
// Execution attempts (control-DB ledger)
// ---------------------------------------------------------------------------
export const ATTEMPT_STATE: Record<AttemptState, { label: string; tone: Tone; meaning: string }> = {
  PREPARED: {
    label: "Prepared",
    tone: "info",
    meaning: "Claimed in the control database; nothing has been sent to the target yet.",
  },
  DISPATCHING: {
    label: "Dispatching",
    tone: "info",
    meaning: "The partition worker may be running on the target right now.",
  },
  TARGET_SUCCEEDED: {
    label: "Target succeeded",
    tone: "success",
    meaning:
      "The target committed. Only the control-database record is pending; the operation is never run again.",
  },
  TARGET_FAILED: {
    label: "Target failed",
    tone: "warning",
    meaning:
      "The target rolled back (proven). Nothing was applied; bounded automatic retry may follow.",
  },
  OUTCOME_UNKNOWN: {
    label: "Outcome unknown",
    tone: "error",
    meaning:
      "PartOps cannot confirm whether the target committed. Never retried automatically — inspect the target and resolve.",
  },
  FINALIZED: { label: "Finalized", tone: "neutral", meaning: "Closed; see the result." },
};

export function attemptInfo(state?: string | null) {
  const key = (state || "").toUpperCase() as AttemptState;
  return ATTEMPT_STATE[key] || { label: state || "—", tone: "neutral" as Tone, meaning: "" };
}

/** Human labels for FINALIZED results (exact value is shown alongside). */
export function attemptResultLabel(result?: string | null): string {
  switch ((result || "").toUpperCase()) {
    case "SUCCESS":
      return "Succeeded";
    case "MANUAL_SUCCESS":
      return "Manual run succeeded";
    case "MANUAL_FAIL":
      return "Manual run failed (rolled back)";
    case "MANUAL_NOT_RUN":
      return "Manual run did not start";
    case "RETRY_EXHAUSTED":
      return "Retries exhausted — needs review";
    case "RESOLVED_APPLIED":
      return "Resolved: operation was applied";
    case "RESOLVED_NOT_APPLIED":
      return "Resolved: operation was not applied";
    case "FAILED_INVALID_SCHEDULE":
      return "Invalid schedule (fail closed)";
    case "":
      return "—";
    default:
      if (result!.toUpperCase().startsWith("ABANDONED")) return "Closed without execution";
      return result!;
  }
}

// ---------------------------------------------------------------------------
// Needs review reasons (Phase 5A + 5D)
// ---------------------------------------------------------------------------
export const REVIEW_REASON: Record<string, { label: string; tone: Tone; summary: string }> = {
  MISSED_SCHEDULED_RUN: {
    label: "Missed scheduled occurrence",
    tone: "warning",
    summary: "Not executed automatically; the job already moved to its next normal schedule.",
  },
  RETRY_EXHAUSTED: {
    label: "Retries exhausted",
    tone: "warning",
    summary:
      "The scheduled run could not complete within its retry budget and was not executed again.",
  },
  REMOTE_OUTCOME_UNKNOWN: {
    label: "Remote outcome unknown",
    tone: "error",
    summary:
      "PartOps cannot confirm whether the target database committed this operation. Do not run it again until the target state has been inspected.",
  },
};

export function reviewReason(reason?: string | null) {
  return (
    REVIEW_REASON[(reason || "").toUpperCase()] || {
      label: reason || "Needs review",
      tone: "warning" as Tone,
      summary: "",
    }
  );
}

export function isUnknownOutcomeReason(reason?: string | null): boolean {
  return (reason || "").toUpperCase() === "REMOTE_OUTCOME_UNKNOWN";
}

// ---------------------------------------------------------------------------
// Targets helpers
// ---------------------------------------------------------------------------
/** Short operator label: registry key upper-cased (db02 → DB-02 style kept as key). */
export function targetLabel(t?: Pick<DatabaseTarget, "target_key"> | null): string {
  if (!t) return "—";
  return t.target_key.toUpperCase();
}

export function targetRole(t?: Pick<DatabaseTarget, "is_control_database"> | null): string {
  if (!t) return "—";
  return t.is_control_database ? "Control database" : "Managed database";
}

export function targetsById(targets: DatabaseTarget[] | null | undefined): Map<number, DatabaseTarget> {
  const map = new Map<number, DatabaseTarget>();
  for (const t of targets || []) map.set(t.target_id, t);
  return map;
}

export function controlTarget(targets: DatabaseTarget[] | null | undefined): DatabaseTarget | null {
  return (targets || []).find((t) => t.is_control_database) || null;
}

/** Resolve a job / log / attention target id (null → control database). */
export function resolveTarget(
  targetId: number | null | undefined,
  targets: DatabaseTarget[] | null | undefined,
): DatabaseTarget | null {
  if (targetId == null) return controlTarget(targets);
  return targetsById(targets).get(Number(targetId)) || null;
}

/** Scheduler availability phrased for operators (no scheduler jargon). */
export function availabilityText(s?: PerTargetSchedulerStatus | null): string {
  if (!s) return "No scheduler data";
  if (!s.is_enabled) return "Disabled";
  if (s.available) return "Available";
  const secs = Math.max(0, Number(s.cooldown_seconds_remaining || 0));
  const reason = s.last_error_class ? ` (${errorClassLabel(s.last_error_class)})` : "";
  return `Temporarily backing off after connection failure${reason} — retry in ~${secs}s`;
}

export function errorClassLabel(cls?: string | null): string {
  if (!cls) return "—";
  const h = TARGET_HEALTH[cls.toUpperCase() as TargetHealthState];
  if (h) return h.label.toLowerCase();
  switch (cls.toUpperCase()) {
    case "TARGET_LOCKED":
      return "table busy";
    case "WORKER_ERROR":
      return "worker error, rolled back";
    case "CONNECT_ERROR":
      return "connection error";
    case "OUTCOME_UNKNOWN":
      return "outcome unknown";
    case "CONTROL_UNAVAILABLE":
      return "control database unavailable";
    case "PRE_DISPATCH_ERROR":
      return "failed before dispatch";
    case "SLOT_TIMEOUT":
      return "no execution slot";
    default:
      return cls.replace(/_/g, " ").toLowerCase();
  }
}

// ---------------------------------------------------------------------------
// Hub status (System / Overview): a managed target failing = DEGRADED,
// the control database failing = DOWN.
// ---------------------------------------------------------------------------
export type HubLevel = "ok" | "degraded" | "down" | "unknown";

export type TargetReadiness = {
  key: string;
  label: string;
  isControl: boolean;
  status: string; // ready | warning | failed | not_required
  summary: string;
};

export function targetReadiness(report: SystemReadinessReport | null): TargetReadiness[] {
  const cat = report?.categories.find((c) => c.key === "targets");
  if (!cat) return [];
  return cat.checks
    .filter((c) => c.key.startsWith("target_") && c.key !== "target_registry")
    .map((c) => ({
      key: c.key.replace(/^target_(invalid_)?/, ""),
      label: c.label,
      isControl: c.label.startsWith("Control database"),
      status: String(c.status),
      summary: c.summary,
    }));
}

export function hubStatus(report: SystemReadinessReport | null): {
  level: HubLevel;
  title: string;
  detail: string;
  healthy: number;
  total: number;
} {
  if (!report) return { level: "unknown", title: "Status unknown", detail: "", healthy: 0, total: 0 };
  const targets = targetReadiness(report).filter((t) => t.status !== "not_required");
  const healthy = targets.filter((t) => t.status === "ready" || t.status === "warning").length;
  // DOWN only when the control database itself cannot be used (connection /
  // credentials / identity). Framework or privilege problems are reported as
  // such (the scheduler gate blocks dispatch) — not as an outage.
  const CONNECTIVITY = /^(UNREACHABLE|AUTH_FAILED|WRONG_DATABASE|CONFIG_ERROR)/;
  const dbCat = report.categories.find((c) => c.key === "database");
  const connectionFailed = Boolean(
    dbCat?.checks.some((c) => c.key === "database_connection" && c.status === "failed"),
  );
  const controlDown = targets.some(
    (t) => t.isControl && t.status === "failed" && CONNECTIVITY.test(t.summary),
  );
  const controlFrameworkFailed =
    targets.some((t) => t.isControl && t.status === "failed" && !CONNECTIVITY.test(t.summary)) ||
    Boolean(dbCat?.checks.some((c) => c.key !== "database_connection" && c.status === "failed"));
  const managedFailed = targets.filter((t) => !t.isControl && t.status === "failed");
  const others = report.categories.filter(
    (c) => c.key !== "targets" && c.key !== "database" && c.status === "failed",
  );
  if (connectionFailed || controlDown) {
    return {
      level: "down",
      title: "Control database unavailable",
      detail:
        "The control database holds jobs, schedules and history. Scheduling and manual runs are affected for every target.",
      healthy,
      total: targets.length,
    };
  }
  // DEGRADED: list every reason so none hides another.
  const reasons: string[] = [];
  const details: string[] = [];
  if (managedFailed.length) {
    reasons.push(managedFailed.map((t) => t.key.toUpperCase()).join(", ") + " unhealthy");
    details.push(
      "Jobs of the affected managed target(s) wait in isolation; control-database jobs and other targets continue normally.",
    );
  }
  if (controlFrameworkFailed) {
    reasons.push("control database framework / privilege checks failing");
    details.push(
      "The control database is reachable, but required framework objects or privileges are missing; the scheduler gate may block automatic execution.",
    );
  }
  if (others.length) {
    reasons.push(others.map((c) => c.label).join(", "));
    details.push("Some PartOps services report failures.");
  }
  if (reasons.length) {
    return {
      level: "degraded",
      title: "Degraded — " + reasons.join("; "),
      detail: details.join(" ") + " See the sections below.",
      healthy,
      total: targets.length,
    };
  }
  return {
    level: "ok",
    title: report.overall_status === "warning" ? "Operational with warnings" : "Operational",
    detail: "Control database and managed targets are reachable.",
    healthy,
    total: targets.length,
  };
}

// ---------------------------------------------------------------------------
// Operator-friendly error messages (never secrets / stack traces)
// ---------------------------------------------------------------------------
const SECRETISH = /(password|passwd|secret|token|aws_access|aws_secret|postgres(ql)?:\/\/\S+)/i;

export function safeText(text?: string | null): string {
  if (!text) return "";
  return String(text)
    .split("\n")[0]
    .replace(/postgres(ql)?:\/\/\S+/gi, "[connection string]")
    .replace(/(password|passwd|secret|token)\S*/gi, "[redacted]")
    .slice(0, 400);
}

/** Multi-line variant for detail views (still redacted, bounded). */
export function redactText(text?: string | null): string {
  if (!text) return "";
  return String(text)
    .replace(/postgres(ql)?:\/\/\S+/gi, "[connection string]")
    .replace(/(password|passwd|secret|token)\S*/gi, "[redacted]")
    .slice(0, 4000);
}

export function isUnknownOutcomeMessage(text?: string | null): boolean {
  return /could not be confirmed|outcome unknown|outcome could not/i.test(text || "");
}

export type FriendlyError = { title: string; detail: string; kind: "unknown" | "error" | "conflict" };

export function friendlyError(err: unknown, context: "run" | "save" | "resolve" | "load" | "review" = "load"): FriendlyError {
  if (!(err instanceof ApiError)) {
    return {
      title: "PartOps API unreachable",
      detail: "The request did not reach the PartOps API. Nothing was changed.",
      kind: "error",
    };
  }
  const detail = safeText(err.message);
  if (context === "run" && err.status === 503) {
    return {
      title: "Manual run could not start because the scheduler is unavailable.",
      detail: "No partition operation was executed. Runs on managed targets go through the scheduler.",
      kind: "error",
    };
  }
  if (context === "run" && err.status === 504) {
    return {
      title: "No answer from the scheduler in time",
      detail: detail || "The run may still be in progress or finished. Check History before doing anything else.",
      kind: "unknown",
    };
  }
  if (isUnknownOutcomeMessage(detail)) {
    return {
      title: "Outcome unknown — do not run again",
      detail:
        "PartOps cannot confirm whether the target database committed this operation. Inspect the target, then resolve it under Needs review.",
      kind: "unknown",
    };
  }
  if (err.status === 409) {
    if (/remote execution attempt|open execution attempt|in state/i.test(detail)) {
      return {
        title: "Blocked by an active or unresolved execution",
        detail,
        kind: "conflict",
      };
    }
    if (/already/i.test(detail) && context === "resolve") {
      return { title: "Attempt already resolved", detail, kind: "conflict" };
    }
    return { title: "Not executed", detail, kind: "conflict" };
  }
  if (err.status === 400 && /confirm_dangerous/i.test(detail)) {
    return {
      title: "Dangerous change not confirmed",
      detail: detail.replace(/confirm_dangerous=true/gi, "explicit confirmation"),
      kind: "conflict",
    };
  }
  for (const [state, info] of Object.entries(TARGET_HEALTH)) {
    if (detail.includes(state)) {
      return { title: `Target ${info.label.toLowerCase()}`, detail, kind: "error" };
    }
  }
  if (SECRETISH.test(err.message)) {
    return { title: "Request failed", detail: "The server reported an error (details withheld).", kind: "error" };
  }
  return { title: "Request failed", detail: detail || `HTTP ${err.status}`, kind: "error" };
}
