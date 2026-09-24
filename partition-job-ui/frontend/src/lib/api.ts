const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ||
  "http://127.0.0.1:8000";

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
  runJob: (id: number, confirmDrop = false) =>
    request<{ result: unknown; message: string }>(`/api/jobs/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ confirm_drop: confirmDrop }),
    }),
  logs: (limit = 100) =>
    request<{ logs: import("./types").JobLog[] }>(`/api/logs?limit=${limit}`),
  readiness: () =>
    request<{ readiness: Record<string, unknown> }>("/api/readiness"),
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
  exportCsvUrl: () => `${API_BASE}/api/jobs/export.csv`,
};

export { API_BASE };
