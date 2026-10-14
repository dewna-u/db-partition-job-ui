import { cn } from "@/lib/utils";

export type BadgeTone =
  | "ok"
  | "warn"
  | "fail"
  | "info"
  | "mute"
  | "create"
  | "drop"
  | "success"
  | "error"
  | "warning"
  | "neutral";

const TONE: Record<BadgeTone, string> = {
  ok: "bg-[var(--success-bg)] text-[var(--success-fg)]",
  success: "bg-[var(--success-bg)] text-[var(--success-fg)]",
  warn: "bg-[var(--warning-bg)] text-[var(--warning-fg)]",
  warning: "bg-[var(--warning-bg)] text-[var(--warning-fg)]",
  fail: "bg-[var(--error-bg)] text-[var(--error-fg)]",
  error: "bg-[var(--error-bg)] text-[var(--error-fg)]",
  info: "bg-[var(--info-bg)] text-[var(--info-fg)]",
  mute: "bg-[var(--neutral-bg)] text-[var(--content-secondary)]",
  neutral: "bg-[var(--neutral-bg)] text-[var(--content-secondary)]",
  create: "bg-[var(--info-bg)] text-[var(--info-fg)]",
  drop: "bg-[var(--warning-bg)] text-[var(--warning-fg)]",
};

export function Badge({
  children,
  tone = "mute",
  className,
}: {
  children: React.ReactNode;
  tone?: BadgeTone;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-[4px] px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide",
        TONE[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function StatusDot({
  tone,
  className,
}: {
  tone: "green" | "amber" | "red" | "blue" | "mute";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-block h-1.5 w-1.5 shrink-0 rounded-full",
        tone === "green" && "bg-[var(--success-fg)]",
        tone === "amber" && "bg-[var(--warning-fg)]",
        tone === "red" && "bg-[var(--error-fg)]",
        tone === "blue" && "bg-[var(--info-fg)]",
        tone === "mute" && "bg-[var(--content-tertiary)]",
        className,
      )}
    />
  );
}

export function readinessStatusBadgeTone(
  status: string,
): BadgeTone {
  switch (status.toLowerCase()) {
    case "ready":
      return "success";
    case "warning":
      return "warning";
    case "failed":
      return "error";
    case "not_required":
      return "neutral";
    default:
      return "info";
  }
}

export function readinessStatusLabel(status: string): string {
  switch (status.toLowerCase()) {
    case "ready":
      return "READY";
    case "warning":
      return "WARNING";
    case "failed":
      return "FAILED";
    case "not_required":
      return "NOT REQUIRED";
    case "unknown":
      return "UNKNOWN";
    default:
      return status.toUpperCase();
  }
}
