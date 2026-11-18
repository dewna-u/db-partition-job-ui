"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { OctagonAlert } from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { useHub } from "@/components/hub/hub-context";
import { AttemptSheet, attemptTable } from "@/components/attempts/attempt-review";
import { AttemptStateBadge, TargetBadge } from "@/components/targets/target-badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { Input, Select } from "@/components/ui/input";
import { Pager } from "@/components/ui/pager";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { Sheet } from "@/components/ui/sheet";
import { api, ApiError } from "@/lib/api";
import { formatDurationMs, formatWhen, isFailStatus, isSuccessStatus } from "@/lib/format";
import { attemptResultLabel, redactText, resolveTarget, safeText } from "@/lib/targets";
import type { ExecutionAttempt, JobLog, PageInfo } from "@/lib/types";

// Phase 5F.1: history and attempts are filtered and paged on the server.
const HISTORY_PAGE_SIZE = 100;
const ATTEMPTS_PAGE_SIZE = 50;
const JOB_FILTER_DEBOUNCE_MS = 350;

/** History statuses written by the local and remote executors. */
const HISTORY_STATUSES = [
  "SUCCESS",
  "FAIL",
  "MANUAL_SUCCESS",
  "MANUAL_FAIL",
  "OUTCOME_UNKNOWN",
  "MANUAL_OUTCOME_UNKNOWN",
  "RESOLVED_APPLIED",
  "RESOLVED_NOT_APPLIED",
] as const;

function logTone(status?: string | null) {
  const s = (status || "").toUpperCase();
  if (s.includes("OUTCOME_UNKNOWN")) return "error" as const;
  if (isFailStatus(s)) return "fail" as const;
  if (isSuccessStatus(s)) return "ok" as const;
  return "mute" as const;
}

export default function HistoryPage() {
  const { targets } = useHub();
  const [logs, setLogs] = useState<JobLog[]>([]);
  const [logPage, setLogPage] = useState<PageInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobFilter, setJobFilter] = useState("");
  const [jobQuery, setJobQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("All");
  const [targetFilter, setTargetFilter] = useState("All");
  const [operationFilter, setOperationFilter] = useState<"" | "create" | "drop">("");
  const [selected, setSelected] = useState<JobLog | null>(null);
  const [attempts, setAttempts] = useState<ExecutionAttempt[]>([]);
  const [attemptPage, setAttemptPage] = useState<PageInfo | null>(null);
  const [attemptsAvailable, setAttemptsAvailable] = useState(true);
  const [unknownCount, setUnknownCount] = useState(0);
  const [showFinalized, setShowFinalized] = useState(false);
  const [attemptId, setAttemptId] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Target filter is a key in the UI; the API filters by target id.
  const targetId = useMemo(() => {
    if (targetFilter === "All") return undefined;
    return (targets || []).find((t) => t.target_key === targetFilter)?.target_id;
  }, [targetFilter, targets]);

  // Typing in the job filter must not issue one request per keystroke.
  useEffect(() => {
    const id = window.setTimeout(() => setJobQuery(jobFilter.trim()), JOB_FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [jobFilter]);

  const loadAttempts = useCallback(
    async (all: boolean, offset = 0) => {
      try {
        const res = await api.attempts({
          open: !all,
          targetId,
          limit: ATTEMPTS_PAGE_SIZE,
          offset,
        });
        setAttempts(res.attempts || []);
        setAttemptsAvailable(res.available);
        setAttemptPage(res);
        // All unresolved unknown outcomes, not only this page / target.
        setUnknownCount(res.unknown_count ?? 0);
      } catch {
        setAttempts([]);
        setAttemptPage(null);
      }
    },
    [targetId],
  );

  const loadLogs = useCallback(
    async (offset = 0, opts?: { refresh?: boolean }) => {
      const isRefresh = Boolean(opts?.refresh);
      setError(null);
      if (isRefresh) setRefreshing(true);
      else setLoading(true);
      try {
        const res = await api.logsPage({
          limit: HISTORY_PAGE_SIZE,
          offset,
          targetId,
          status: statusFilter === "All" ? undefined : statusFilter,
          job: jobQuery || undefined,
          operation: operationFilter || undefined,
        });
        setLogs(res.logs || []);
        setLogPage(res);
      } catch (err) {
        setError(err instanceof ApiError ? safeText(err.message) : "Failed to load history");
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [targetId, statusFilter, jobQuery, operationFilter],
  );

  async function load(opts?: { refresh?: boolean }) {
    await Promise.all([loadLogs(logPage?.offset ?? 0, opts), loadAttempts(showFinalized, attemptPage?.offset ?? 0)]);
  }

  // Any filter change returns to the first page.
  useEffect(() => {
    void loadLogs(0);
  }, [loadLogs]);

  useEffect(() => {
    void loadAttempts(showFinalized, 0);
  }, [showFinalized, loadAttempts]);

  const unknown = attempts.filter((a) => String(a.state).toUpperCase() === "OUTCOME_UNKNOWN");

  return (
    <div className="pj-enter space-y-3">
      <PageHeader
        title="History"
        subtitle="Executions on every database target of this hub, and remote execution attempts."
        showActions={false}
      />

      <div className="flex flex-wrap gap-2">
        <Input
          className="max-w-xs"
          placeholder="Filter by job id or name"
          value={jobFilter}
          onChange={(e) => setJobFilter(e.target.value)}
          aria-label="Filter by job id or name"
        />
        {(targets || []).length > 1 ? (
          <Select
            className="w-32"
            value={targetFilter}
            onChange={(e) => setTargetFilter(e.target.value)}
            aria-label="Database target filter"
          >
            <option value="All">All targets</option>
            {(targets || []).map((t) => (
              <option key={t.target_id} value={t.target_key}>
                {t.target_key.toUpperCase()}
              </option>
            ))}
          </Select>
        ) : null}
        <Select
          className="w-36"
          value={operationFilter}
          onChange={(e) => setOperationFilter(e.target.value as "" | "create" | "drop")}
          aria-label="Operation filter"
        >
          <option value="">All operations</option>
          <option value="create">Create</option>
          <option value="drop">Drop</option>
        </Select>
        <Select
          className="w-52"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Status filter"
        >
          <option>All</option>
          {HISTORY_STATUSES.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </Select>
        <Button
          variant="secondary"
          onClick={() => void load({ refresh: true })}
          disabled={loading || refreshing}
          aria-busy={refreshing}
        >
          {refreshing ? (
            <>
              <PartOpsLoader size="sm" />
              Refreshing…
            </>
          ) : (
            "Refresh"
          )}
        </Button>
      </div>

      {notice ? (
        <p className="text-[12px] font-semibold text-[var(--success-fg)]" role="status">{notice}</p>
      ) : null}

      {unknownCount ? (
        <div
          role="alert"
          className="rounded-[6px] border-2 border-[var(--error-fg)] bg-[var(--error-bg)] px-3 py-2.5 text-[12px]"
        >
          <div className="flex items-center gap-1.5 font-semibold text-[var(--error-fg)]">
            <OctagonAlert className="h-4 w-4" aria-hidden />
            {unknownCount} execution outcome{unknownCount === 1 ? "" : "s"} unknown
          </div>
          <p className="mt-0.5 text-[var(--content-primary)]">
            PartOps cannot confirm whether the target database committed these operations. Do not run
            the jobs again until the target state has been inspected. Open an attempt to record the
            inspection result.
          </p>
        </div>
      ) : null}

      {loading && !logs.length ? (
        <div className="flex items-center gap-2 py-6" aria-busy="true" role="status">
          <PartOpsLoader size="md" label="Loading history…" />
        </div>
      ) : null}

      {error ? <p className="text-sm font-semibold text-[var(--error-fg)]">{error}</p> : null}

      <div className="overflow-x-auto rounded-card border border-pj-line bg-pj-card">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Execution history</caption>
          <thead className="bg-pj-table-head text-[0.62rem] uppercase tracking-[0.08em] text-pj-muted">
            <tr>
              <th className="px-3 py-2.5">Time</th>
              <th className="px-3 py-2.5">Target</th>
              <th className="px-3 py-2.5">Job</th>
              <th className="px-3 py-2.5">Status</th>
              <th className="px-3 py-2.5">Duration</th>
              <th className="px-3 py-2.5">Error</th>
              <th className="px-3 py-2.5">Details</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((row) => {
              const target = resolveTarget(row.database_target_id, targets);
              return (
                <tr key={row.job_log_id} className="border-t border-pj-line">
                  <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{formatWhen(row.job_runtime)}</td>
                  <td className="px-3 py-2.5">
                    <TargetBadge
                      target={target || (row.target_key ? { target_key: row.target_key, is_control_database: false, display_name: row.target_key } : null)}
                      fallbackId={row.database_target_id ?? null}
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="font-bold">{row.job_name || `Job ${row.job_id}`}</div>
                    <div className="text-[0.65rem] text-pj-muted">#{row.job_id}</div>
                  </td>
                  <td className="px-3 py-2.5">
                    <Badge tone={logTone(row.last_run_status)}>{row.last_run_status || "—"}</Badge>
                  </td>
                  <td className="px-3 py-2.5 tabular-nums font-semibold text-[var(--content-secondary)]">
                    {row.execution_duration_ms == null ? "—" : formatDurationMs(row.execution_duration_ms)}
                  </td>
                  <td className="max-w-[240px] truncate px-3 py-2.5 text-pj-muted" title={safeText(row.job_error)}>
                    {row.job_error ? safeText(row.job_error).slice(0, 120) : "—"}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5">
                    <div className="flex gap-1.5">
                      {row.execution_attempt_id != null ? (
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => setAttemptId(row.execution_attempt_id ?? null)}
                          aria-label={`Open execution attempt ${row.execution_attempt_id}`}
                        >
                          Attempt #{row.execution_attempt_id}
                        </Button>
                      ) : null}
                      {row.job_error ? (
                        <Button variant="ghost" size="sm" onClick={() => setSelected(row)}>
                          Error
                        </Button>
                      ) : null}
                      {row.execution_attempt_id == null && !row.job_error ? "—" : null}
                    </div>
                  </td>
                </tr>
              );
            })}
            {!logs.length && !loading ? (
              <tr>
                <td colSpan={7} className="px-3 py-10 text-center text-pj-muted">
                  No execution history rows match.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
        <Pager
          page={logPage}
          count={logs.length}
          busy={loading || refreshing}
          onOffset={(o) => void loadLogs(o)}
          label="History"
        />
      </div>

      <Panel>
        <PanelHeader
          title="Remote execution attempts"
          description={
            showFinalized
              ? "Recent attempts on managed targets (control-database ledger), including finalized ones."
              : "Attempts that are still open — in flight, awaiting finalization, retrying, or with an unknown outcome."
          }
          actions={
            <label className="flex items-center gap-1.5 text-[11px] text-[var(--content-secondary)]">
              <input
                type="checkbox"
                checked={showFinalized}
                onChange={(e) => setShowFinalized(e.target.checked)}
              />
              Include finalized
            </label>
          }
        />
        {!attemptsAvailable ? (
          <p className="px-3 py-3 text-[12px] text-[var(--content-secondary)]">
            Remote execution is not installed on this database (migrations 20261117_03/04).
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <caption className="sr-only">Remote execution attempts</caption>
              <thead className="bg-[var(--surface-tertiary)] text-[11px] text-[var(--content-secondary)]">
                <tr>
                  <th className="px-3 py-1.5 font-medium">Attempt</th>
                  <th className="px-3 py-1.5 font-medium">Target</th>
                  <th className="px-3 py-1.5 font-medium">Job</th>
                  <th className="px-3 py-1.5 font-medium">Operation</th>
                  <th className="px-3 py-1.5 font-medium">Schema.table</th>
                  <th className="px-3 py-1.5 font-medium">State</th>
                  <th className="px-3 py-1.5 font-medium">Result</th>
                  <th className="px-3 py-1.5 font-medium">Updated</th>
                </tr>
              </thead>
              <tbody>
                {[...unknown, ...attempts.filter((a) => !unknown.includes(a))].map((a) => {
                  const isUnknown = String(a.state).toUpperCase() === "OUTCOME_UNKNOWN";
                  return (
                    <tr
                      key={a.attempt_id}
                      className={`cursor-pointer border-t border-[var(--border-default)] hover:bg-[var(--hover-surface-1)] ${
                        isUnknown ? "bg-[var(--error-bg)] shadow-[inset_3px_0_0_var(--error-fg)]" : ""
                      }`}
                      onClick={() => setAttemptId(a.attempt_id)}
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setAttemptId(a.attempt_id);
                        }
                      }}
                      aria-label={`Open execution attempt ${a.attempt_id}`}
                    >
                      <td className="px-3 py-1.5 font-mono text-[11px]">#{a.attempt_id}</td>
                      <td className="px-3 py-1.5">
                        <TargetBadge target={resolveTarget(a.database_target_id, targets)} fallbackId={a.database_target_id} />
                      </td>
                      <td className="px-3 py-1.5">
                        {a.job_name || "Job"} <span className="text-[var(--content-tertiary)]">#{a.job_id}</span>
                      </td>
                      <td className="px-3 py-1.5">
                        <Badge tone={a.operation === "DROP" ? "drop" : "create"}>{a.operation}</Badge>
                        <span className="ml-1 text-[11px] text-[var(--content-tertiary)]">{a.execution_kind.toLowerCase()}</span>
                      </td>
                      <td className="max-w-[200px] truncate px-3 py-1.5 font-mono text-[11px]" title={attemptTable(a)}>
                        {attemptTable(a)}
                      </td>
                      <td className="px-3 py-1.5"><AttemptStateBadge state={a.state} /></td>
                      <td className="px-3 py-1.5 text-[var(--content-secondary)]">{attemptResultLabel(a.result)}</td>
                      <td className="whitespace-nowrap px-3 py-1.5 tabular-nums text-[var(--content-secondary)]">
                        {formatWhen(a.updated_at)}
                      </td>
                    </tr>
                  );
                })}
                {!attempts.length ? (
                  <tr>
                    <td colSpan={8} className="px-3 py-4 text-center text-[var(--content-tertiary)]">
                      {showFinalized ? "No remote execution attempts recorded." : "No open remote execution attempts."}
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
            <Pager
              page={attemptPage}
              count={attempts.length}
              onOffset={(o) => void loadAttempts(showFinalized, o)}
              label="Remote execution attempts"
            />
          </div>
        )}
      </Panel>

      <Sheet
        open={Boolean(selected)}
        onOpenChange={(open) => !open && setSelected(null)}
        title={
          selected
            ? `${selected.job_name || `Job ${selected.job_id}`} · ${selected.last_run_status}`
            : "Error"
        }
        description="Error recorded in partitioning_job_table_log"
        wide
      >
        <div className="space-y-3 text-xs">
          <div className="rounded-lg border border-pj-line bg-pj-surface px-3 py-2">
            <div className="text-[0.58rem] font-bold uppercase tracking-[0.1em] text-[var(--content-tertiary)]">
              Duration
            </div>
            <div className="mt-1 font-semibold">
              {selected?.execution_duration_ms == null
                ? "Not recorded"
                : formatDurationMs(selected.execution_duration_ms)}
            </div>
          </div>
          <pre className="whitespace-pre-wrap rounded-xl border border-[var(--border-default)] bg-[var(--info-bg)] p-3 font-mono text-xs text-[var(--info-fg)]">
            {redactText(selected?.job_error)}
          </pre>
        </div>
      </Sheet>

      <AttemptSheet
        attemptId={attemptId}
        onClose={() => setAttemptId(null)}
        onChanged={(m) => {
          setNotice(m);
          void load({ refresh: true });
        }}
      />
    </div>
  );
}
