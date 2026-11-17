"use client";

import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { UnknownOutcomePanel } from "@/components/attempts/attempt-review";
import { TargetBadge } from "@/components/targets/target-badges";
import { useHub } from "@/components/hub/hub-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { api, ApiError } from "@/lib/api";
import {
  attentionReasonLabel,
  attentionTarget,
  formatDurationMs,
  formatWhen,
} from "@/lib/format";
import { friendlyError, isUnknownOutcomeReason, resolveTarget, reviewReason } from "@/lib/targets";
import type { AttentionItem, ExecutionAttempt } from "@/lib/types";
import { cn } from "@/lib/utils";

type Mode = "idle" | "recover" | "acknowledge";

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--content-tertiary)]">
        {label}
      </dt>
      <dd
        className={cn(
          "mt-0.5 break-all text-[12px] text-[var(--content-primary)]",
          mono && "font-mono text-[11px]",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

/**
 * Review card for one missed / not-executed scheduled occurrence.
 * Amber "needs review" styling — a missed run is NOT an execution failure.
 */
export function AttentionReviewCard({
  item,
  onChanged,
}: {
  item: AttentionItem;
  onChanged?: (message?: string) => void;
}) {
  if (isUnknownOutcomeReason(item.reason)) {
    return <UnknownOutcomeAttentionCard item={item} onChanged={onChanged} />;
  }
  return <MissedRunCard item={item} onChanged={onChanged} />;
}

/**
 * Phase 5D/5E: REMOTE_OUTCOME_UNKNOWN. Run / acknowledge are disabled until the
 * execution attempt is resolved (the backend enforces the same rule).
 */
function UnknownOutcomeAttentionCard({
  item,
  onChanged,
}: {
  item: AttentionItem;
  onChanged?: (message?: string) => void;
}) {
  const [attempt, setAttempt] = useState<ExecutionAttempt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const id = item.execution_attempt_id ?? null;

  useEffect(() => {
    if (id == null) return;
    let alive = true;
    api
      .attempt(id)
      .then((res) => alive && setAttempt(res.attempt))
      .catch((err) => {
        const f = friendlyError(err, "load");
        if (alive) setError(`${f.title}: ${f.detail}`);
      });
    return () => {
      alive = false;
    };
  }, [id]);

  if (attempt && String(attempt.state).toUpperCase() === "OUTCOME_UNKNOWN") {
    return <UnknownOutcomePanel attempt={attempt} onResolved={(m) => onChanged?.(m)} />;
  }
  return (
    <div
      role="alert"
      className="rounded-[6px] border-2 border-[var(--error-fg)] bg-[var(--error-bg)] px-3 py-2.5 text-[12px]"
    >
      <div className="font-semibold text-[var(--error-fg)]">Outcome unknown</div>
      <p className="mt-0.5">{reviewReason(item.reason).summary}</p>
      <p className="mt-1 font-mono text-[11px] text-[var(--content-secondary)]">
        {attentionTarget(item)} · missed {formatWhen(item.missed_run_time)}
        {id != null ? ` · attempt #${id}` : ""}
      </p>
      {attempt ? (
        <p className="mt-1 text-[11px] text-[var(--content-secondary)]">
          The attempt is now {attempt.state.toLowerCase().replace(/_/g, " ")}; refresh to update this item.
        </p>
      ) : error ? (
        <p className="mt-1 text-[11px] text-[var(--error-fg)]">{error}</p>
      ) : id != null ? (
        <p className="mt-1 text-[11px] text-[var(--content-secondary)]">Loading attempt…</p>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" disabled title="Not available until the attempt is resolved">
          Run missed occurrence
        </Button>
        <Button size="sm" variant="ghost" disabled title="Not available until the attempt is resolved">
          Acknowledge
        </Button>
      </div>
    </div>
  );
}

function MissedRunCard({
  item,
  onChanged,
}: {
  item: AttentionItem;
  onChanged?: (message?: string) => void;
}) {
  const { targets } = useHub();
  const dbTarget = resolveTarget(item.database_target_id, targets);
  const isDrop = item.is_create !== true;
  const target = attentionTarget(item);
  const [mode, setMode] = useState<Mode>("idle");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmRun, setConfirmRun] = useState(false);
  const [confirmDrop, setConfirmDrop] = useState(false);
  const [typedTarget, setTypedTarget] = useState("");
  const [confirmAck, setConfirmAck] = useState(false);
  const [note, setNote] = useState("");

  const canRecover =
    confirmRun && (!isDrop || (confirmDrop && typedTarget.trim() === target));

  function reset(next: Mode) {
    setMode(next);
    setError(null);
    setConfirmRun(false);
    setConfirmDrop(false);
    setTypedTarget("");
    setConfirmAck(false);
    setNote("");
  }

  async function recover() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const res = await api.recoverAttention(item.attention_id, {
        confirmDrop: isDrop ? confirmDrop : false,
        confirmTarget: isDrop ? typedTarget.trim() : undefined,
      });
      setMessage(res.message);
      reset("idle");
      onChanged?.(res.message);
    } catch (err) {
      const f = friendlyError(err, "run");
      setError(err instanceof ApiError ? `${f.title} ${f.detail}`.trim() : "Recovery failed");
      // A failed attempt is recorded on the item; reload to show it.
      onChanged?.();
    } finally {
      setBusy(false);
    }
  }

  async function acknowledge() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const res = await api.acknowledgeAttention(item.attention_id, note.trim() || undefined);
      setMessage(res.message);
      reset("idle");
      onChanged?.(res.message);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Acknowledge failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className={cn(
        "rounded-[6px] border border-l-[3px] bg-[var(--warning-bg)] px-3 py-2.5",
        isDrop
          ? "border-[var(--warning-fg)]"
          : "border-[var(--border-default)] border-l-[var(--warning-fg)]",
      )}
      role="region"
      aria-label={`Needs review: missed run for job ${item.job_id}`}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <AlertTriangle className="h-3.5 w-3.5 text-[var(--warning-fg)]" aria-hidden />
        <Badge tone="warning">Needs review</Badge>
        <Badge tone={isDrop ? "drop" : "create"}>{isDrop ? "DROP" : "CREATE"}</Badge>
        <span className="text-[12px] font-semibold text-[var(--content-primary)]">
          {attentionReasonLabel(item.reason)}
        </span>
        {item.database_target_id != null ? <TargetBadge target={dbTarget} fallbackId={item.database_target_id} /> : null}
      </div>

      <p className="mt-1 text-[11px] text-[var(--content-secondary)]">
        This occurrence was <strong>not executed</strong> automatically. The job has already
        been moved to its next normal schedule; running or acknowledging this item does not
        change that schedule.
      </p>

      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-2">
        <Field label="Missed scheduled time" value={formatWhen(item.missed_run_time)} mono />
        <Field label="Detected" value={formatWhen(item.detected_at)} mono />
        <Field
          label="Next scheduled run"
          value={
            item.current_next_run_time
              ? formatWhen(item.current_next_run_time)
              : "None — schedule needs fixing"
          }
          mono
        />
        <Field label="Target table" value={target} mono />
      </dl>

      {item.detail ? (
        <p className="mt-1.5 text-[11px] text-[var(--content-secondary)]">{item.detail}</p>
      ) : null}

      {item.manual_execution_result ? (
        <p className="mt-1.5 text-[11px] text-[var(--content-secondary)]">
          Last recovery attempt:{" "}
          <span className="font-mono">{item.manual_execution_result}</span>
          {item.manual_executed_at ? ` · ${formatWhen(item.manual_executed_at)}` : ""}
          {item.manual_execution_duration_ms != null
            ? ` · ${formatDurationMs(item.manual_execution_duration_ms)}`
            : ""}
          {item.manual_execution_message ? ` · ${item.manual_execution_message}` : ""}
        </p>
      ) : null}

      {isDrop ? (
        <p className="mt-2 rounded-[4px] border border-[var(--warning-fg)] px-2 py-1.5 text-[11px] font-semibold text-[var(--warning-fg)]">
          Destructive operation: running this missed DROP may permanently remove partition data
          from {target}. Review retention and current partitions before running it.
        </p>
      ) : null}

      {mode === "idle" ? (
        <div className="mt-2.5 flex flex-wrap gap-2">
          <Button size="sm" variant="secondary" onClick={() => reset("recover")} disabled={busy}>
            Run missed occurrence…
          </Button>
          <Button size="sm" variant="ghost" onClick={() => reset("acknowledge")} disabled={busy}>
            Acknowledge without running…
          </Button>
        </div>
      ) : null}

      {mode === "recover" ? (
        <div className="mt-2.5 space-y-2 border-t border-[var(--border-default)] pt-2.5">
          <label className="flex items-start gap-2 text-[11px] text-[var(--content-primary)]">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirmRun}
              onChange={(e) => setConfirmRun(e.target.checked)}
            />
            I have reviewed this missed run and want to execute it now. The next normal run
            stays as scheduled.
          </label>
          {isDrop ? (
            <>
              <label className="flex items-start gap-2 text-[11px] font-semibold text-[var(--warning-fg)]">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={confirmDrop}
                  onChange={(e) => setConfirmDrop(e.target.checked)}
                />
                I understand this DROP may permanently remove partition data.
              </label>
              <div>
                <Label htmlFor={`confirm-target-${item.attention_id}`}>
                  Type <span className="font-mono">{target}</span> to confirm
                </Label>
                <Input
                  id={`confirm-target-${item.attention_id}`}
                  value={typedTarget}
                  onChange={(e) => setTypedTarget(e.target.value)}
                  className="font-mono"
                  autoComplete="off"
                  spellCheck={false}
                />
              </div>
            </>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant={isDrop ? "danger" : "default"}
              disabled={busy || !canRecover}
              onClick={() => void recover()}
              aria-busy={busy}
            >
              {busy ? (
                <>
                  <PartOpsLoader size="sm" />
                  Running…
                </>
              ) : (
                "Run missed occurrence"
              )}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => reset("idle")} disabled={busy}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}

      {mode === "acknowledge" ? (
        <div className="mt-2.5 space-y-2 border-t border-[var(--border-default)] pt-2.5">
          <div>
            <Label htmlFor={`ack-note-${item.attention_id}`}>Note (optional)</Label>
            <Input
              id={`ack-note-${item.attention_id}`}
              value={note}
              maxLength={500}
              placeholder="Why this run is not needed"
              onChange={(e) => setNote(e.target.value)}
            />
          </div>
          <label className="flex items-start gap-2 text-[11px] text-[var(--content-primary)]">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirmAck}
              onChange={(e) => setConfirmAck(e.target.checked)}
            />
            Close this item without executing the missed {isDrop ? "DROP" : "CREATE"}.
          </label>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="secondary"
              disabled={busy || !confirmAck}
              onClick={() => void acknowledge()}
              aria-busy={busy}
            >
              {busy ? (
                <>
                  <PartOpsLoader size="sm" />
                  Saving…
                </>
              ) : (
                "Acknowledge without running"
              )}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => reset("idle")} disabled={busy}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}

      {message ? (
        <p className="mt-2 text-[11px] font-semibold text-[var(--success-fg)]">{message}</p>
      ) : null}
      {error ? (
        <p className="mt-2 text-[11px] font-semibold text-[var(--error-fg)]">{error}</p>
      ) : null}
    </div>
  );
}

export function AttentionReviewList({
  items,
  onChanged,
}: {
  items: AttentionItem[];
  onChanged?: (message?: string) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="space-y-2">
      {items.map((item) => (
        <AttentionReviewCard key={item.attention_id} item={item} onChanged={onChanged} />
      ))}
    </div>
  );
}
