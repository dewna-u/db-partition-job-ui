"use client";

import { Badge } from "@/components/ui/badge";
import { attemptInfo, healthInfo, targetLabel } from "@/lib/targets";
import type { DatabaseTarget } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Quiet target chip (deliberately calmer than job status): mono key with a
 * hairline border; control database marked with a small "ctl" suffix.
 */
export function TargetBadge({
  target,
  fallbackId,
  className,
}: {
  target: Pick<DatabaseTarget, "target_key" | "is_control_database" | "display_name"> | null;
  fallbackId?: number | null;
  className?: string;
}) {
  const text = target ? targetLabel(target) : fallbackId != null ? `#${fallbackId}` : "—";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-[4px] border border-[var(--border-default)] px-1.5 py-0.5 font-mono text-[10.5px] text-[var(--content-secondary)]",
        className,
      )}
      title={
        target
          ? `${target.display_name} — ${target.is_control_database ? "control database" : "managed database"}`
          : "Unknown database target"
      }
    >
      {text}
      {target?.is_control_database ? (
        <span className="text-[9.5px] text-[var(--content-tertiary)]" aria-label="control database">
          ctl
        </span>
      ) : null}
    </span>
  );
}

export function TargetHealthBadge({ state, className }: { state?: string | null; className?: string }) {
  const info = healthInfo(state);
  return (
    <Badge tone={info.tone} className={cn("normal-case tracking-normal", className)}>
      {info.label}
    </Badge>
  );
}

export function AttemptStateBadge({ state, className }: { state?: string | null; className?: string }) {
  const info = attemptInfo(state);
  return (
    <Badge tone={info.tone} className={cn("normal-case tracking-normal", className)}>
      {info.label}
    </Badge>
  );
}
