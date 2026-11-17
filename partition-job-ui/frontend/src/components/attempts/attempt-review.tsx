"use client";

/**
 * Phase 5E — remote execution attempts and the OUTCOME_UNKNOWN workflow.
 *
 * Safety rules mirrored from the backend (never weakened here):
 *  - an unknown target outcome is NEVER re-run from the UI (no retry / force
 *    / ignore action exists);
 *  - resolution is the operator's finding after inspecting the target:
 *    APPLIED or NOT_APPLIED, with a note; PartOps never guesses;
 *  - DROP resolution requires typing the exact schema.table.
 */
import { useCallback, useEffect, useState } from "react";
import { OctagonAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Label, Textarea } from "@/components/ui/input";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { Sheet } from "@/components/ui/sheet";
import { AttemptStateBadge, TargetBadge } from "@/components/targets/target-badges";
import { useHub } from "@/components/hub/hub-context";
import { api } from "@/lib/api";
import { formatDurationMs, formatWhen } from "@/lib/format";
import {
  attemptInfo,
  attemptResultLabel,
  errorClassLabel,
  friendlyError,
  resolveTarget,
  safeText,
} from "@/lib/targets";
import type { AttemptResolution, ExecutionAttempt } from "@/lib/types";
import { cn } from "@/lib/utils";

function Field({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--content-tertiary)]">{label}</dt>
      <dd className={cn("mt-0.5 break-all text-[12px] text-[var(--content-primary)]", mono && "font-mono text-[11px]")}>
        {value}
      </dd>
    </div>
  );
}

export function attemptTable(a: Pick<ExecutionAttempt, "table_schema" | "table_name">): string {
  return `${a.table_schema || ""}.${a.table_name || ""}`;
}

const VERIFY_TEXT: Record<AttemptResolution, string> = {
  APPLIED: "I verified that the operation WAS applied on the target database.",
  NOT_APPLIED: "I verified that the operation WAS NOT applied on the target database.",
};

/** Strong, impossible-to-miss panel for one OUTCOME_UNKNOWN attempt. */
export function UnknownOutcomePanel({
  attempt,
  onResolved,
  compact = false,
}: {
  attempt: ExecutionAttempt;
  onResolved?: (message: string) => void;
  compact?: boolean;
}) {
  const { targets } = useHub();
  const target = resolveTarget(attempt.database_target_id, targets);
  const isDrop = String(attempt.operation).toUpperCase() === "DROP";
  const table = attemptTable(attempt);
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState<AttemptResolution | null>(null);
  const [verified, setVerified] = useState(false);
  const [note, setNote] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ title: string; detail: string } | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const noteOk = note.trim().length >= 3;
  const typedOk = !isDrop || typed.trim() === table;
  const canSubmit = Boolean(choice) && verified && noteOk && typedOk && !busy;

  function cancel() {
    setOpen(false);
    setChoice(null);
    setVerified(false);
    setNote("");
    setTyped("");
    setError(null);
  }

  async function submit() {
    if (!choice) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.resolveAttempt(attempt.attempt_id, {
        confirm: true,
        resolution: choice,
        note: note.trim(),
        confirm_target: isDrop ? typed.trim() : null,
      });
      setDone(res.message);
      cancel();
      onResolved?.(res.message);
    } catch (err) {
      setError(friendlyError(err, "resolve"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      role="alert"
      aria-labelledby={`unknown-${attempt.attempt_id}-title`}
      className="rounded-[6px] border-2 border-[var(--error-fg)] bg-[var(--error-bg)] px-3 py-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <OctagonAlert className="h-4 w-4 text-[var(--error-fg)]" aria-hidden />
        <h3 id={`unknown-${attempt.attempt_id}-title`} className="text-[14px] font-semibold text-[var(--error-fg)]">
          Outcome unknown
        </h3>
        <Badge tone={isDrop ? "drop" : "create"}>{attempt.operation}</Badge>
        <TargetBadge target={target} fallbackId={attempt.database_target_id} />
      </div>
      <p className="mt-1.5 text-[12px] font-medium text-[var(--content-primary)]">
        PartOps cannot confirm whether the target database committed this operation.
      </p>
      <p className="mt-0.5 text-[12px] font-semibold text-[var(--error-fg)]">
        Do not run the job again until the target state has been inspected.
      </p>
      {!compact ? (
        <p className="mt-0.5 text-[11px] text-[var(--content-secondary)]">
          Automatic scheduling of this job is paused, and edits / re-enabling are blocked, until this
          attempt is resolved. There is intentionally no retry action.
        </p>
      ) : null}

      <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1.5 sm:grid-cols-2">
        <Field label="Target" value={target ? `${target.display_name} (${target.target_key})` : attempt.target_key} />
        <Field label="Job" value={`${attempt.job_name || "Job"} · #${attempt.job_id}`} />
        <Field label="Operation" value={`${attempt.operation} · ${attempt.execution_kind.toLowerCase()} run`} />
        <Field label="Schema.table" value={table} mono />
        <Field label="Expected run" value={attempt.expected_run_time ? formatWhen(attempt.expected_run_time) : "— (manual)"} mono />
        <Field label="Dispatch time" value={formatWhen(attempt.dispatch_started_at)} mono />
        <Field label="Attempt" value={`#${attempt.attempt_id}`} mono />
        <Field label="Reason" value={safeText(attempt.error_summary) || "Connection lost or commit not confirmed"} />
      </dl>

      {done ? <p className="mt-2 text-[12px] font-semibold text-[var(--success-fg)]" role="status">{done}</p> : null}

      {!open && !done ? (
        <div className="mt-3">
          <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
            Record inspection result…
          </Button>
        </div>
      ) : null}

      {open ? (
        <div className="mt-3 space-y-2.5 border-t border-[var(--error-fg)] pt-2.5">
          <fieldset>
            <legend className="mb-1 text-[11px] font-semibold text-[var(--content-primary)]">
              What did you find on {target?.target_key || attempt.target_key}? (no default — choose deliberately)
            </legend>
            {(["APPLIED", "NOT_APPLIED"] as const).map((r) => (
              <label key={r} className="flex items-start gap-2 py-0.5 text-[12px]">
                <input
                  type="radio"
                  name={`resolution-${attempt.attempt_id}`}
                  className="mt-0.5"
                  checked={choice === r}
                  onChange={() => {
                    setChoice(r);
                    setVerified(false);
                  }}
                />
                <span>
                  <span className="font-semibold">{r === "APPLIED" ? "The operation was applied" : "The operation was not applied"}</span>
                  <span className="block text-[11px] text-[var(--content-secondary)]">
                    {r === "APPLIED"
                      ? "Recorded as a successful run; the job's next scheduled run is recalculated."
                      : "Recorded as not executed; the next scheduled run is recalculated. Run it manually later only if still needed."}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>

          {choice ? (
            <label className="flex items-start gap-2 text-[12px] font-semibold text-[var(--content-primary)]">
              <input type="checkbox" className="mt-0.5" checked={verified} onChange={(e) => setVerified(e.target.checked)} />
              {VERIFY_TEXT[choice]}
            </label>
          ) : null}

          <div>
            <Label htmlFor={`note-${attempt.attempt_id}`}>Inspection note (required)</Label>
            <Textarea
              id={`note-${attempt.attempt_id}`}
              value={note}
              maxLength={400}
              rows={3}
              className="min-h-[64px] font-sans"
              placeholder="What you checked on the target (partitions present / absent, time, by whom)"
              onChange={(e) => setNote(e.target.value)}
            />
          </div>

          {isDrop ? (
            <div>
              <Label htmlFor={`typed-${attempt.attempt_id}`}>
                DROP: type <span className="font-mono">{table}</span> to confirm the table you inspected
              </Label>
              <Input
                id={`typed-${attempt.attempt_id}`}
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                className="font-mono"
                autoComplete="off"
                spellCheck={false}
              />
            </div>
          ) : null}

          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant={isDrop ? "danger" : "default"}
              disabled={!canSubmit}
              onClick={() => void submit()}
              aria-busy={busy}
            >
              {busy ? (
                <>
                  <PartOpsLoader size="sm" />
                  Recording…
                </>
              ) : (
                "Record resolution"
              )}
            </Button>
            <Button size="sm" variant="ghost" onClick={cancel} disabled={busy}>
              Cancel
            </Button>
          </div>
          <p className="text-[11px] text-[var(--content-secondary)]">
            Recording a resolution executes nothing on any database.
          </p>
        </div>
      ) : null}

      {error ? (
        <p className="mt-2 text-[12px] text-[var(--error-fg)]" role="alert">
          <span className="font-semibold">{error.title}.</span> {error.detail}
        </p>
      ) : null}
    </section>
  );
}

/** Full attempt detail (technical state preserved next to the human label). */
export function AttemptDetail({
  attempt,
  onChanged,
}: {
  attempt: ExecutionAttempt;
  onChanged?: (message: string) => void;
}) {
  const { targets } = useHub();
  const target = resolveTarget(attempt.database_target_id, targets);
  const info = attemptInfo(attempt.state);
  return (
    <div className="space-y-3">
      {String(attempt.state).toUpperCase() === "OUTCOME_UNKNOWN" ? (
        <UnknownOutcomePanel attempt={attempt} onResolved={onChanged} />
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <AttemptStateBadge state={attempt.state} />
        <span className="font-mono text-[11px] text-[var(--content-tertiary)]">{attempt.state}</span>
        <TargetBadge target={target} fallbackId={attempt.database_target_id} />
      </div>
      <p className="text-[12px] text-[var(--content-secondary)]">{info.meaning}</p>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
        <Field label="Attempt" value={`#${attempt.attempt_id}`} mono />
        <Field label="Kind" value={attempt.execution_kind} />
        <Field label="Job" value={`${attempt.job_name || "Job"} · #${attempt.job_id}`} />
        <Field label="Operation" value={attempt.operation} />
        <Field label="Schema.table" value={attemptTable(attempt)} mono />
        <Field label="Expected run" value={attempt.expected_run_time ? formatWhen(attempt.expected_run_time) : "— (manual)"} mono />
        <Field label="Created" value={formatWhen(attempt.created_at)} mono />
        <Field label="Dispatched" value={formatWhen(attempt.dispatch_started_at)} mono />
        <Field label="Target finished" value={formatWhen(attempt.target_finished_at)} mono />
        <Field label="Finalized" value={formatWhen(attempt.finalized_at)} mono />
        <Field
          label="Result"
          value={attempt.result ? `${attemptResultLabel(attempt.result)} (${attempt.result})` : "—"}
        />
        <Field label="Failures (proven, retried)" value={String(attempt.failure_count)} />
        <Field label="Duration" value={formatDurationMs(attempt.execution_duration_ms)} />
        <Field label="Error category" value={errorClassLabel(attempt.error_class)} />
      </dl>
      {attempt.error_summary && String(attempt.state).toUpperCase() !== "OUTCOME_UNKNOWN" ? (
        <pre className="whitespace-pre-wrap rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-tertiary)] p-2 font-mono text-[11px] text-[var(--content-secondary)]">
          {safeText(attempt.error_summary)}
        </pre>
      ) : null}
    </div>
  );
}

/** Sheet that loads one attempt by id. */
export function AttemptSheet({
  attemptId,
  onClose,
  onChanged,
}: {
  attemptId: number | null;
  onClose: () => void;
  onChanged?: (message: string) => void;
}) {
  const [attempt, setAttempt] = useState<ExecutionAttempt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (id: number) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.attempt(id);
      setAttempt(res.attempt);
    } catch (err) {
      const f = friendlyError(err, "load");
      setError(`${f.title}: ${f.detail}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (attemptId != null) void load(attemptId);
    else setAttempt(null);
  }, [attemptId, load]);

  return (
    <Sheet
      open={attemptId != null}
      onOpenChange={(open) => !open && onClose()}
      title={attemptId != null ? `Execution attempt #${attemptId}` : "Execution attempt"}
      description="Remote execution record in the control database"
      wide
    >
      {loading && !attempt ? (
        <div className="flex items-center gap-2 py-6" role="status" aria-busy="true">
          <PartOpsLoader size="md" label="Loading attempt…" />
        </div>
      ) : null}
      {error ? <p className="text-[12px] text-[var(--error-fg)]">{error}</p> : null}
      {attempt ? (
        <AttemptDetail
          attempt={attempt}
          onChanged={(m) => {
            onChanged?.(m);
            if (attemptId != null) void load(attemptId);
          }}
        />
      ) : null}
    </Sheet>
  );
}
