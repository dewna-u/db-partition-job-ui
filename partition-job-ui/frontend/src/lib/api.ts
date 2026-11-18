const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.message || detail;
      if (Array.isArray(detail)) {
        detail = detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join("; ");
      }
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, String(detail));
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("text/csv")) {
    return (await res.text()) as T;
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ ok: boolean }>("/api/health"),
  dashboard: () => request<import("./types").DashboardSummary>("/api/dashboard/summary"),
  jobs: () => request<{ jobs: import("./types").PartitionJob[] }>("/api/jobs"),
  job: (id: number) =>
    request<{ job: import("./types").PartitionJob }>(`/api/jobs/${id}`),
  createJob: (body: import("./types").JobCreatePayload) =>
    request<{ result: unknown; message: string; refresh_ok: boolean; refresh_message: string }>(
      "/api/jobs",
      { method: "POST", body: JSON.stringify(body) },
    ),
  updateJob: (id: number, body: import("./types").JobUpdatePayload) =>
    request<{
      result: unknown;
      job_id: number;
      job?: import("./types").PartitionJob | null;
      message: string;
      refresh_ok: boolean;
      refresh_message: string;
    }>(`/api/jobs/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  runJob: (id: number, confirmDrop = false) =>
    request<{ result: import("./types").ManualRunResult | unknown; message: string }>(`/api/jobs/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ confirm_drop: confirmDrop }),
    }),
  logs: (limit = 100) =>
    request<import("./types").LogsPage>(`/api/logs?limit=${limit}`),
  // Phase 5F.1 — server-side filtered / paginated history.
  logsPage: (opts: import("./types").LogsQuery = {}) => {
    const q = new URLSearchParams();
    q.set("limit", String(opts.limit ?? 100));
    q.set("offset", String(opts.offset ?? 0));
    if (opts.targetId != null) q.set("target_id", String(opts.targetId));
    if (opts.status) q.set("status", opts.status);
    if (opts.job) q.set("job", opts.job);
    if (opts.operation) q.set("operation", opts.operation);
    return request<import("./types").LogsPage>(`/api/logs?${q.toString()}`);
  },
  // Phase 5F.1 — job drawer data for ONE job (replaces the 200-row history scan).
  jobRuntimeContext: (id: number) =>
    request<import("./types").JobRuntimeContext>(`/api/jobs/${id}/runtime-context`),
  jobAttention: (id: number) =>
    request<import("./types").AttentionListResponse>(`/api/jobs/${id}/attention`),
  readiness: () =>
    request<{ readiness: Record<string, unknown> }>("/api/readiness"),
  systemReadiness: (refresh?: boolean) => {
    const q = refresh ? "?refresh=1" : "";
    return request<import("./types").SystemReadinessReport>(
      `/api/system/readiness${q}`,
    );
  },
  schedulerStatus: () =>
    request<{
      ok: boolean;
      status: import("./types").SchedulerStatus | null;
      message: string;
    }>("/api/scheduler/status"),
  pgagentJobs: () =>
    request<{ jobs: Record<string, unknown>[] }>("/api/pgagent/jobs"),
  pgagentDetails: (jobId: number, stepId?: number) => {
    const q = stepId != null ? `?step_id=${stepId}` : "";
    return request<{ details: Record<string, unknown> }>(
      `/api/pgagent/jobs/${jobId}${q}`,
    );
  },
  cronPreview: (job_schedule: string) =>
    request<{
      valid: boolean;
      error: string | null;
      next_run: string | null;
      description: string | null;
    }>("/api/cron/preview", {
      method: "POST",
      body: JSON.stringify({ job_schedule }),
    }),
  attention: (status: "pending" | "acknowledged" | "manually_rerun" | "all" = "pending") =>
    request<import("./types").AttentionListResponse>(
      `/api/attention?status=${status}`,
    ),
  acknowledgeAttention: (id: number, note?: string) =>
    request<{ result: unknown; message: string }>(
      `/api/attention/${id}/acknowledge`,
      { method: "POST", body: JSON.stringify({ confirm: true, note: note || null }) },
    ),
  recoverAttention: (
    id: number,
    opts: { confirmDrop?: boolean; confirmTarget?: string } = {},
  ) =>
    request<{
      result: {
        status: string;
        message: string | null;
        execution_duration_ms: number | null;
        attention_status: string;
        job_id: number;
        attempt_id?: number | null;
      };
      message: string;
    }>(`/api/attention/${id}/recover`, {
      method: "POST",
      body: JSON.stringify({
        confirm: true,
        confirm_drop: Boolean(opts.confirmDrop),
        confirm_target: opts.confirmTarget ?? null,
      }),
    }),
  // Phase 5C/5D — hub-local database targets (read-only) and remote attempts.
  targets: () => request<import("./types").TargetsResponse>("/api/targets"),
  targetHealth: (id: number) =>
    request<import("./types").TargetHealthResponse>(`/api/targets/${id}/health`),
  attempts: (
    opts: {
      open?: boolean;
      state?: string;
      jobId?: number;
      targetId?: number;
      limit?: number;
      offset?: number;
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (opts.open) q.set("open", "true");
    if (opts.state) q.set("state", opts.state);
    if (opts.jobId != null) q.set("job_id", String(opts.jobId));
    if (opts.targetId != null) q.set("target_id", String(opts.targetId));
    q.set("limit", String(opts.limit ?? 50));
    q.set("offset", String(opts.offset ?? 0));
    return request<import("./types").AttemptsResponse>(`/api/execution-attempts?${q.toString()}`);
  },
  attempt: (id: number) =>
    request<{ attempt: import("./types").ExecutionAttempt }>(`/api/execution-attempts/${id}`),
  resolveAttempt: (id: number, body: import("./types").ResolveAttemptPayload) =>
    request<{ result: Record<string, unknown>; message: string }>(
      `/api/execution-attempts/${id}/resolve`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  exportCsvUrl: () => `${API_BASE}/api/jobs/export.csv`,
};

export { API_BASE };
