import { cn } from "@/lib/utils";

export function Badge({
  children,
  tone = "mute",
  className,
}: {
  children: React.ReactNode;
  tone?: "ok" | "warn" | "fail" | "info" | "mute" | "create" | "drop";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-2 py-0.5 text-[0.58rem] font-extrabold uppercase tracking-wide",
        tone === "ok" && "bg-pj-ok-bg text-pj-ok dark:bg-pj-ok-bg dark:text-pj-ok",
        tone === "warn" && "bg-pj-warn-bg text-pj-warn",
        tone === "fail" && "bg-pj-fail-bg text-pj-fail",
        tone === "info" && "bg-pj-create-bg text-pj-primary",
        tone === "mute" && "bg-pj-table-head text-pj-muted",
        tone === "create" && "bg-pj-create-bg text-pj-primary",
        tone === "drop" && "bg-pj-drop-bg text-pj-warn",
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
        "inline-block h-2 w-2 rotate-45 rounded-[2px]",
        tone === "green" && "bg-pj-ok",
        tone === "amber" && "bg-pj-warn",
        tone === "red" && "bg-pj-fail",
        tone === "blue" && "bg-pj-primary",
        tone === "mute" && "bg-pj-muted",
        className,
      )}
    />
  );
}

export function readinessStatusBadgeTone(
  status: string,
): "ok" | "warn" | "fail" | "mute" | "info" {
  switch (status.toLowerCase()) {
    case "ready":
      return "ok";
    case "warning":
      return "warn";
    case "failed":
      return "fail";
    case "not_required":
      return "mute";
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
