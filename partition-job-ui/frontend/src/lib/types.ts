export type PartitionJob = {
  job_id: number;
  job_name: string | null;
  is_enabled: boolean;
  table_schema: string | null;
  table_name: string | null;
  db_config_para?: unknown;
  frequency?: string | null;
  last_run_time?: string | null;
  next_run_time?: string | null;
  last_run_status?: string | null;
  partition_unit?: string | null;
  partition_period?: number | null;
  is_create?: boolean;
  create_drop_interval?: string | null;
  job_schedule?: string | null;
  last_execution_duration_ms?: number | null;
  avg_execution_duration_ms?: number | null;
};

export type JobLog = {
  job_log_id: number;
  job_id: number;
  job_name: string | null;
  last_run_status: string | null;
  job_runtime: string | null;
  job_error: string | null;
  execution_duration_ms?: number | null;
};

export type SchedulerStatus = {
  started_at?: string | null;
  last_refresh_at?: string | null;
  last_refresh_result?: string | null;
  upcoming_job_count?: number;
  next_job_id?: number | null;
  next_expected_run_time?: string | null;
  last_execution_job_id?: number | null;
  last_execution_result?: string | null;
  scheduler_active?: boolean;
  lookahead_seconds?: number;
  reconcile_seconds?: number;
};

export type DashboardSummary = {
  jobs: PartitionJob[];
  logs: JobLog[];
  counts: {
    total: number;
    enabled: number;
    disabled: number;
    create: number;
    drop: number;
  };
  next_execution: {
    available: boolean;
    countdown: string;
    detail: string;
    job_id?: number | null;
    next_run_time?: string | null;
    absolute?: string;
  };
  success_rate: {
    available: boolean;
    label: string;
    detail: string;
    rate?: number | null;
  };
  scheduler_uptime: {
    available: boolean;
    label: string;
    detail: string;
  };
  failed_executions: number;
  failed_last_24h?: number;
  average_duration?: {
    available: boolean;
    average_ms?: number | null;
    label: string;
    detail: string;
    sample_count?: number;
  };
  insights: string[];
  scheduler: {
    ok: boolean;
    status: SchedulerStatus | null;
    message: string;
  };
  readiness: Record<string, unknown> | null;
  readiness_error: string | null;
  jobs_error: string | null;
  logs_error: string | null;
};

export type JobCreatePayload = {
  job_name: string;
  is_enabled: boolean;
  table_schema: string;
  table_name: string;
  db_config: string;
  job_schedule: string;
  frequency_amount: number;
  frequency_unit: string;
  next_run_time: string;
  partition_unit: string;
  partition_period: number;
  is_create: boolean;
  create_drop_amount: number;
  create_drop_unit: string;
};

export type JobUpdatePayload = JobCreatePayload & {
  confirm_dangerous?: boolean;
};

export type ReadinessStatus =
  | "ready"
  | "warning"
  | "failed"
  | "not_required"
  | "unknown";

export type SystemReadinessCheck = {
  key: string;
  label: string;
  category: string;
  status: ReadinessStatus | string;
  summary: string;
  details?: Record<string, unknown> | unknown;
  remediation_hint?: string;
};

export type SystemReadinessCategory = {
  key: string;
  label: string;
  status: ReadinessStatus | string;
  checks: SystemReadinessCheck[];
};

export type SystemReadinessCounts = {
  ready: number;
  warning: number;
  failed: number;
  not_required: number;
  unknown: number;
};

export type SystemReadinessReport = {
  overall_status: ReadinessStatus | string;
  overall_label: string;
  checked_at: string;
  from_cache: boolean;
  counts: SystemReadinessCounts;
  categories: SystemReadinessCategory[];
  product: {
    name: string;
    subtitle: string;
  };
};
