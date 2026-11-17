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
  /** Phase 5C/5D: hub-local database target (null/absent = control database, pre-5C). */
  database_target_id?: number | null;
};

/** Phase 5A/5D: why an item needs review. */
export type AttentionReason =
  | "MISSED_SCHEDULED_RUN"
  | "RETRY_EXHAUSTED"
  | "REMOTE_OUTCOME_UNKNOWN";

/** Phase 5A: a scheduled occurrence PartOps did NOT execute automatically. */
export type AttentionStatus = "PENDING_REVIEW" | "MANUALLY_RERUN" | "ACKNOWLEDGED";

export type AttentionItem = {
  attention_id: number;
  job_id: number;
  job_name: string | null;
  table_schema: string | null;
  table_name: string | null;
  /** Operation at detection time (snapshot). */
  is_create: boolean | null;
  reason: AttentionReason | string;
  missed_run_time: string;
  detected_at: string;
  rescheduled_to: string | null;
  detail: string | null;
  status: AttentionStatus | string;
  reviewed_at: string | null;
  resolution: string | null;
  manual_execution_result: string | null;
  manual_execution_message: string | null;
  manual_execution_duration_ms: number | null;
  manual_executed_at: string | null;
  /** Job's CURRENT normal schedule (unchanged by recovery). */
  current_next_run_time: string | null;
  job_is_enabled: boolean | null;
  /** Phase 5D: target of the occurrence (null = control database / pre-5D). */
  database_target_id?: number | null;
  /** Phase 5D: remote execution attempt (REMOTE_OUTCOME_UNKNOWN, remote retries). */
  execution_attempt_id?: number | null;
};

export type AttentionListResponse = {
  items: AttentionItem[];
  available: boolean;
  pending_count: number;
  message: string | null;
};

export type JobLog = {
  job_log_id: number;
  job_id: number;
  job_name: string | null;
  last_run_status: string | null;
  job_runtime: string | null;
  job_error: string | null;
  execution_duration_ms?: number | null;
  /** Phase 5D: target the run executed on (null = control database). */
  database_target_id?: number | null;
  /** Registry key resolved by the API (e.g. "db02"). */
  target_key?: string | null;
  execution_attempt_id?: number | null;
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
  max_concurrent_jobs?: number;
  active_job_count?: number;
  queued_job_count?: number;
  framework_status?: string;
  /** Phase 5D: global concurrency and per-target operational state. */
  global_active?: number;
  global_max?: number;
  unknown_outcomes?: number;
  finalization_pending?: number;
  ledger_writes_pending?: number;
  pending_review_count?: number;
  targets?: Record<string, PerTargetSchedulerStatus>;
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
  /** Phase 5D: omit to keep the current target (edit) / use the control DB (create). */
  database_target_id?: number | null;
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

// ---------------------------------------------------------------------------
// Phase 5C/5D — hub-local database targets and remote execution
// ---------------------------------------------------------------------------

/** Safe public view of one registry row (no passwords / secret values / ids). */
export type DatabaseTarget = {
  target_id: number;
  target_key: string;
  display_name: string;
  database_name: string;
  host?: string | null;
  port?: number | null;
  credential_provider: "inherit" | "env" | "aws" | string;
  secret_configured: boolean;
  aws_region?: string | null;
  ssl_mode?: string | null;
  environment?: string | null;
  is_enabled: boolean;
  is_control_database: boolean;
  max_concurrent_jobs: number;
  executable: boolean;
};

export type TargetsResponse = {
  targets: DatabaseTarget[];
  invalid_targets: { target_id: number | null; target_key: string | null; error: string }[];
  available: boolean;
  message: string | null;
};

export type TargetHealthState =
  | "HEALTHY"
  | "HEALTHY_WITH_WARNINGS"
  | "DISABLED"
  | "CONFIG_ERROR"
  | "UNREACHABLE"
  | "AUTH_FAILED"
  | "WRONG_DATABASE"
  | "FRAMEWORK_INCOMPATIBLE";

export type FrameworkProblem = {
  status: string;
  object: string;
  check: string;
  detail: string;
};

export type TargetHealth = {
  target_id: number;
  target_key: string;
  is_control_database: boolean;
  checked_at: string;
  state: TargetHealthState | string;
  detail?: string | null;
  framework: {
    overall: string;
    counts: { PASS?: number; WARNING?: number; FAIL?: number };
    expected_version: string | null;
    database_version: string | null;
    problems: FrameworkProblem[];
  } | null;
};

export type TargetHealthResponse = { target: DatabaseTarget; health: TargetHealth };

/** Scheduler in-memory view of one target (Phase 5D). */
export type PerTargetSchedulerStatus = {
  target_id: number;
  is_control_database: boolean;
  is_enabled: boolean;
  active_jobs: number;
  waiting_jobs: number;
  max_concurrent_jobs: number | null;
  available: boolean;
  consecutive_failures: number;
  cooldown_seconds_remaining: number;
  last_error_class: string | null;
};

export type AttemptState =
  | "PREPARED"
  | "DISPATCHING"
  | "TARGET_SUCCEEDED"
  | "TARGET_FAILED"
  | "OUTCOME_UNKNOWN"
  | "FINALIZED";

export type ExecutionAttempt = {
  attempt_id: number;
  job_id: number;
  database_target_id: number;
  target_key: string;
  execution_kind: "SCHEDULED" | "MANUAL" | string;
  expected_run_time: string | null;
  operation: "CREATE" | "DROP" | string;
  job_name: string | null;
  table_schema: string;
  table_name: string;
  partition_unit: string | null;
  partition_period: number | null;
  create_drop_interval: string | null;
  planned_next_run_time: string | null;
  attention_id: number | null;
  state: AttemptState | string;
  failure_count: number;
  created_at: string;
  updated_at: string;
  dispatch_started_at: string | null;
  target_finished_at: string | null;
  finalized_at: string | null;
  result: string | null;
  error_class: string | null;
  error_summary: string | null;
  execution_duration_ms: number | null;
};

export type AttemptsResponse = {
  attempts: ExecutionAttempt[];
  available: boolean;
  message: string | null;
};

export type AttemptResolution = "APPLIED" | "NOT_APPLIED";

export type ResolveAttemptPayload = {
  confirm: boolean;
  resolution: AttemptResolution;
  note: string;
  confirm_target?: string | null;
};

export type ManualRunResult = {
  status?: string;
  message?: string | null;
  execution_duration_ms?: number | null;
  attempt_id?: number | null;
  target_key?: string | null;
  operation?: string | null;
};
