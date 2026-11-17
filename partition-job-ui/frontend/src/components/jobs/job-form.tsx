"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { useHub } from "@/components/hub/hub-context";
import { Button } from "@/components/ui/button";
import { PartOpsLoader } from "@/components/ui/partops-loader";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { friendlyError, healthInfo, targetRole } from "@/lib/targets";
import type { DatabaseTarget, JobCreatePayload, JobUpdatePayload } from "@/lib/types";

const FREQUENCY_UNITS = ["minute", "hour", "day", "week", "month", "year"] as const;
const PARTITION_UNITS = ["day", "week", "month", "year"] as const;
const INTERVAL_UNITS = ["day", "week", "month", "year"] as const;

export type JobFormValues = {
  job_name: string;
  is_enabled: boolean;
  is_create: boolean;
  table_schema: string;
  table_name: string;
  job_schedule: string;
  frequency_amount: number;
  frequency_unit: string;
  auto_next_run: boolean;
  next_run_time: string;
  partition_unit: string;
  partition_period: number;
  create_drop_amount: number;
  create_drop_unit: string;
  db_config: string;
  /** Phase 5D/5E: hub-local database target (null = not chosen / control default). */
  database_target_id: number | null;
};

const DEFAULTS: JobFormValues = {
  job_name: "",
  is_enabled: true,
  is_create: true,
  table_schema: "",
  table_name: "",
  job_schedule: "0 0 2 * * *",
  frequency_amount: 1,
  frequency_unit: "day",
  auto_next_run: true,
  next_run_time: "",
  partition_unit: "day",
  partition_period: 1,
  create_drop_amount: 1,
  create_drop_unit: "month",
  db_config: "{}",
  database_target_id: null,
};

export function JobForm({
  initial,
  submitLabel = "Create job",
  onSuccess,
  mode = "create",
  jobId,
  baseline,
  onCancel,
}: {
  initial?: Partial<JobFormValues>;
  submitLabel?: string;
  onSuccess?: (message: string) => void;
  mode?: "create" | "edit";
  jobId?: number;
  /** Original values used to detect dangerous edits and dirty state. */
  baseline?: Partial<JobFormValues>;
  onCancel?: () => void;
}) {
  const [values, setValues] = useState<JobFormValues>({ ...DEFAULTS, ...initial });
  const [preview, setPreview] = useState<{
    valid: boolean;
    error: string | null;
    next_run: string | null;
    description: string | null;
  } | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [confirmDangerous, setConfirmDangerous] = useState(false);
  const [confirmTarget, setConfirmTarget] = useState(false);
  const { targets, targetsAvailable, health, healthLoading, loadHealth } = useHub();
  const targetList = useMemo(() => targets || [], [targets]);

  // Health of managed targets is needed to offer them (cached 60 s, no polling).
  useEffect(() => {
    for (const t of targetList) {
      if (t.is_enabled && !t.is_control_database) void loadHealth(t.target_id);
    }
  }, [targetList, loadHealth]);

  // Create: default to the control database once the registry is known.
  useEffect(() => {
    if (mode !== "create" || values.database_target_id != null) return;
    const ctl = targetList.find((t) => t.is_control_database && t.is_enabled);
    if (ctl) setValues((v) => ({ ...v, database_target_id: ctl.target_id }));
  }, [mode, targetList, values.database_target_id]);

  function targetUsability(t: DatabaseTarget): { ok: boolean; reason: string } {
    if (!t.is_enabled) return { ok: false, reason: "disabled by DBA" };
    if (t.is_control_database) return { ok: true, reason: "" };
    const h = health[t.target_id];
    if (!h || healthLoading[t.target_id]) return { ok: false, reason: "checking health…" };
    if (!h.health) return { ok: false, reason: "health check failed" };
    const info = healthInfo(h.health.state);
    return info.usable ? { ok: true, reason: info.tone === "warning" ? "warnings" : "" } : { ok: false, reason: info.label.toLowerCase() };
  }

  useEffect(() => {
    if (initial) setValues((v) => ({ ...v, ...initial }));
  }, [initial]);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      void api
        .cronPreview(values.job_schedule)
        .then((res) => {
          setPreview(res);
          if (values.auto_next_run && res.next_run) {
            setValues((v) => ({ ...v, next_run_time: res.next_run!.replace(" ", "T") }));
          }
        })
        .catch(() => setPreview(null));
    }, 250);
    return () => window.clearTimeout(handle);
  }, [values.job_schedule, values.auto_next_run]);

  const payload: JobCreatePayload | null = useMemo(() => {
    if (!values.next_run_time) return null;
    const next = values.next_run_time.includes("T")
      ? values.next_run_time
      : values.next_run_time.replace(" ", "T");
    return {
      job_name: values.job_name,
      is_enabled: values.is_enabled,
      table_schema: values.table_schema,
      table_name: values.table_name,
      db_config: values.db_config,
      job_schedule: values.job_schedule,
      frequency_amount: Number(values.frequency_amount),
      frequency_unit: values.frequency_unit,
      next_run_time: next,
      partition_unit: values.partition_unit,
      partition_period: Number(values.partition_period),
      is_create: values.is_create,
      create_drop_amount: Number(values.create_drop_amount),
      create_drop_unit: values.create_drop_unit,
    };
  }, [values]);


  const base = baseline || initial || DEFAULTS;
  const baseTargetId = base.database_target_id ?? null;
  const targetChanged =
    mode === "edit" && values.database_target_id != null && values.database_target_id !== baseTargetId;
  const fromTarget = targetList.find((t) => t.target_id === baseTargetId) || null;
  const toTarget = targetList.find((t) => t.target_id === values.database_target_id) || null;
  const selectedUsable = toTarget ? targetUsability(toTarget).ok : true;
  const dangerous =
    mode === "edit" &&
    (Boolean(base.is_create) !== values.is_create ||
      String(base.table_schema || "") !== values.table_schema ||
      String(base.table_name || "") !== values.table_name);

  const dirty = useMemo(() => {
    if (mode !== "edit") return true;
    const keys: (keyof JobFormValues)[] = [
      "job_name",
      "is_enabled",
      "is_create",
      "table_schema",
      "table_name",
      "job_schedule",
      "frequency_amount",
      "frequency_unit",
      "partition_unit",
      "partition_period",
      "create_drop_amount",
      "create_drop_unit",
      "db_config",
      "database_target_id",
    ];
    if (!values.auto_next_run) {
      keys.push("next_run_time");
    }
    return keys.some((key) => String(values[key] ?? "") !== String(base[key] ?? ""));
  }, [mode, values, base]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!payload) {
      setError("Next run time is required.");
      return;
    }
    if (preview && !preview.valid) {
      setError(preview.error || "Invalid schedule");
      return;
    }
    if (mode === "edit" && !dirty) {
      setError("No changes to save");
      return;
    }
    if (dangerous && !confirmDangerous) {
      setError("Confirm the dangerous configuration change before saving.");
      return;
    }
    if (targetChanged && !confirmTarget) {
      setError("Confirm the database target change before saving.");
      return;
    }
    if ((mode === "create" || targetChanged) && toTarget && !selectedUsable) {
      setError(`Database target ${toTarget.target_key.toUpperCase()} is not usable right now.`);
      return;
    }
    setBusy(true);
    setError(null);
    setOk(null);
    try {
      if (mode === "edit" && jobId != null) {
        const needsConfirm = dangerous || targetChanged;
        const body: JobUpdatePayload = {
          ...payload,
          // Only send a target when it changes (unchanged → backend keeps it).
          ...(targetChanged ? { database_target_id: values.database_target_id } : {}),
          confirm_dangerous:
            needsConfirm &&
            (!dangerous || confirmDangerous) &&
            (!targetChanged || confirmTarget),
        };
        const res = await api.updateJob(jobId, body);
        setOk(res.message);
        onSuccess?.(res.message);
      } else {
        const res = await api.createJob(
          targetsAvailable && values.database_target_id != null
            ? { ...payload, database_target_id: values.database_target_id }
            : payload,
        );
        setOk(res.message);
        onSuccess?.(res.message);
      }
    } catch (err) {
      if (err instanceof ApiError) {
        const f = friendlyError(err, "save");
        setError(f.title === "Request failed" ? f.detail : `${f.title}: ${f.detail}`);
      } else {
        setError(mode === "edit" ? "Update failed" : "Create failed");
      }
    } finally {
      setBusy(false);
    }
  }

  function set<K extends keyof JobFormValues>(key: K, value: JobFormValues[K]) {
    setValues((v) => ({ ...v, [key]: value }));
  }

  function reset() {
    setValues({ ...DEFAULTS, ...initial });
    setError(null);
    setOk(null);
    setConfirmDangerous(false);
    setConfirmTarget(false);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      <section className="rounded-card border border-pj-line bg-pj-card p-3">
        <SectionTitle>Job</SectionTitle>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label="Job name">
            <Input
              value={values.job_name}
              onChange={(e) => set("job_name", e.target.value)}
              required
            />
          </Field>
          <Field label="Enabled" group>
            <label className="flex h-8 items-center gap-3 text-sm font-semibold">
              <span
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${
                  values.is_enabled ? "bg-[var(--success-fg)]" : "bg-[var(--content-tertiary)]"
                }`}
              >
                <input
                  type="checkbox"
                  className="peer sr-only"
                  checked={values.is_enabled}
                  onChange={(e) => set("is_enabled", e.target.checked)}
                />
                <span
                  className={`inline-block h-5 w-5 transform rounded-full bg-[var(--surface-primary)] shadow transition ${
                    values.is_enabled ? "translate-x-5" : "translate-x-0.5"
                  }`}
                />
              </span>
              {values.is_enabled ? "ON" : "OFF"}
            </label>
          </Field>
        </div>
        <div className="mt-3">
          <Label>CREATE / DROP</Label>
          <div className="flex gap-2">
            {(["CREATE", "DROP"] as const).map((op) => (
              <button
                key={op}
                type="button"
                onClick={() => set("is_create", op === "CREATE")}
                className={`rounded-[6px] border px-2.5 py-1.5 text-[12px] font-medium ${
                  (op === "CREATE") === values.is_create
                    ? op === "CREATE"
                      ? "border-[var(--client-primary)] bg-[var(--info-bg)] text-[var(--client-primary)]"
                      : "border-[var(--warning-fg)] bg-[var(--warning-bg)] text-[var(--warning-fg)]"
                    : "border-[var(--border-default)] bg-[var(--surface-primary)] text-[var(--content-secondary)]"
                }`}
              >
                {op}
              </button>
            ))}
          </div>
          {!values.is_create ? (
            <p className="mt-2 rounded-lg border border-[var(--border-default)] bg-[var(--warning-bg)] px-3 py-2 text-xs font-bold text-[var(--warning-fg)]">
              DROP operation: review retention carefully. Wrong values can remove
              production data.
            </p>
          ) : null}
        </div>
      </section>

      <section className="rounded-card border border-pj-line bg-pj-card p-3">
        <SectionTitle>Database target</SectionTitle>
        {targetsAvailable && targetList.length ? (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <Field label="Database target">
              <Select
                value={values.database_target_id ?? ""}
                onChange={(e) =>
                  set("database_target_id", e.target.value === "" ? null : Number(e.target.value))
                }
                aria-describedby="target-help"
              >
                {values.database_target_id == null ? <option value="">Select a target…</option> : null}
                {targetList.map((t) => {
                  const u = targetUsability(t);
                  const isCurrent = mode === "edit" && t.target_id === baseTargetId;
                  return (
                    <option key={t.target_id} value={t.target_id} disabled={!u.ok && !isCurrent}>
                      {t.target_key.toUpperCase()} — {targetRole(t)}
                      {u.reason ? ` (${u.reason})` : ""}
                      {isCurrent ? " · current" : ""}
                    </option>
                  );
                })}
              </Select>
            </Field>
            <p id="target-help" className="self-end text-[11px] text-[var(--content-secondary)]">
              The database this job&apos;s CREATE / DROP runs on. Only enabled targets that pass their
              health check can be chosen. Targets are registered by a DBA, not here.
            </p>
          </div>
        ) : (
          <p className="mt-2 text-[11px] text-[var(--content-secondary)]">
            {targets === null
              ? "Loading targets…"
              : "Single-database installation: jobs run on the control database."}
          </p>
        )}
      </section>

      <section className="rounded-card border border-pj-line bg-pj-card p-3">
        <SectionTitle>Target table</SectionTitle>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label="Schema">
            <Input
              value={values.table_schema}
              onChange={(e) => set("table_schema", e.target.value)}
              required
            />
          </Field>
          <Field label="Table">
            <Input
              value={values.table_name}
              onChange={(e) => set("table_name", e.target.value)}
              required
            />
          </Field>
        </div>
      </section>

      <section className="rounded-card border border-pj-line bg-pj-card p-3">
        <SectionTitle>Partition configuration</SectionTitle>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label="Partition unit">
            <Select
              value={values.partition_unit}
              onChange={(e) => set("partition_unit", e.target.value)}
            >
              {PARTITION_UNITS.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Partition period">
            <Input
              type="number"
              min={1}
              value={values.partition_period}
              onChange={(e) => set("partition_period", Number(e.target.value))}
            />
          </Field>
          <Field label={values.is_create ? "Create ahead amount" : "Retention amount"}>
            <Input
              type="number"
              min={1}
              value={values.create_drop_amount}
              onChange={(e) => set("create_drop_amount", Number(e.target.value))}
            />
          </Field>
          <Field label={values.is_create ? "Create ahead unit" : "Retention unit"}>
            <Select
              value={values.create_drop_unit}
              onChange={(e) => set("create_drop_unit", e.target.value)}
            >
              {INTERVAL_UNITS.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </section>

      <section className="rounded-card border border-pj-line bg-pj-card p-3">
        <SectionTitle>Schedule</SectionTitle>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label="Cron expression (six-field)">
            <Input
              value={values.job_schedule}
              onChange={(e) => set("job_schedule", e.target.value)}
              className="font-mono"
              required
            />
          </Field>
          <div className="rounded-xl border border-[var(--border-default)] bg-[var(--info-bg)] px-3 py-2 text-xs text-[var(--info-fg)]">
            <div className="font-bold">Schedule preview</div>
            <div className="mt-1">
              {preview?.valid
                ? preview.description || "Valid schedule"
                : preview?.error || "Enter a six-field cron expression"}
            </div>
            <div className="mt-2 font-semibold">
              Next execution: {preview?.next_run || "—"}
            </div>
          </div>
          <Field label="Frequency amount">
            <Input
              type="number"
              min={1}
              value={values.frequency_amount}
              onChange={(e) => set("frequency_amount", Number(e.target.value))}
            />
          </Field>
          <Field label="Frequency unit">
            <Select
              value={values.frequency_unit}
              onChange={(e) => set("frequency_unit", e.target.value)}
            >
              {FREQUENCY_UNITS.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <label className="mt-3 flex items-center gap-2 text-xs font-semibold">
          <input
            type="checkbox"
            checked={values.auto_next_run}
            onChange={(e) => set("auto_next_run", e.target.checked)}
          />
          Use next run calculated from the schedule
        </label>
        {!values.auto_next_run ? (
          <div className="mt-2">
            <Field label="Next run (local ISO)">
              <Input
                type="datetime-local"
                value={values.next_run_time.slice(0, 16)}
                onChange={(e) => set("next_run_time", e.target.value)}
                required
              />
            </Field>
          </div>
        ) : null}
      </section>

      <section className="rounded-card border border-pj-line bg-pj-card p-3">
        <button
          type="button"
          className="flex w-full items-center justify-between text-left"
          onClick={() => setAdvancedOpen((v) => !v)}
        >
          <SectionTitle>Advanced settings</SectionTitle>
          <span className="text-xs font-bold text-pj-muted">
            {advancedOpen ? "Hide" : "Show"} db_config_para
          </span>
        </button>
        {advancedOpen ? (
          <div className="mt-3">
            <Field label="db_config_para (JSON object)">
              <Textarea
                value={values.db_config}
                onChange={(e) => set("db_config", e.target.value)}
                rows={6}
              />
            </Field>
          </div>
        ) : null}
      </section>

      {targetChanged ? (
        <section
          role="group"
          aria-labelledby="target-change-title"
          className="rounded-[6px] border-2 border-[var(--warning-fg)] bg-[var(--warning-bg)] px-3 py-3"
        >
          <div id="target-change-title" className="flex items-center gap-1.5 text-[13px] font-semibold text-[var(--content-primary)]">
            <AlertTriangle className="h-4 w-4 text-[var(--warning-fg)]" aria-hidden />
            Database target changed
          </div>
          <p className="mt-1 text-[12px] text-[var(--content-primary)]">
            This job will execute against a different database.
          </p>
          <dl className="mt-2 grid grid-cols-[110px_1fr] gap-y-1 text-[12px]">
            <dt className="text-[var(--content-secondary)]">Current</dt>
            <dd className="font-mono">{fromTarget ? `${fromTarget.target_key.toUpperCase()} — ${fromTarget.display_name}` : String(baseTargetId ?? "—")}</dd>
            <dt className="text-[var(--content-secondary)]">New</dt>
            <dd className="font-mono">{toTarget ? `${toTarget.target_key.toUpperCase()} — ${toTarget.display_name}` : "—"}</dd>
            <dt className="text-[var(--content-secondary)]">Schema.table</dt>
            <dd className="break-all font-mono">{`${values.table_schema}.${values.table_name}`}</dd>
            <dt className="text-[var(--content-secondary)]">Operation</dt>
            <dd className="font-mono">{values.is_create ? "CREATE" : "DROP"}</dd>
          </dl>
          <p className="mt-2 text-[11px] text-[var(--content-secondary)]">
            The table must exist with the same partitioning on the new database. The change is refused
            while this job has an active or unresolved execution.
          </p>
          <label className="mt-2 flex items-start gap-2 text-[12px] font-semibold text-[var(--content-primary)]">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={confirmTarget}
              onChange={(e) => setConfirmTarget(e.target.checked)}
            />
            I confirm this job will run against {toTarget ? toTarget.target_key.toUpperCase() : "the new target"} instead of {fromTarget ? fromTarget.target_key.toUpperCase() : "its current target"}.
          </label>
        </section>
      ) : null}

      {dangerous ? (
        <label className="flex items-start gap-2 rounded-xl border border-[var(--border-default)] bg-[var(--warning-bg)] px-3 py-3 text-xs font-semibold text-[var(--warning-fg)]">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={confirmDangerous}
            onChange={(e) => setConfirmDangerous(e.target.checked)}
          />
          I confirm this high-impact change (CREATE↔DROP and/or target schema.table).
        </label>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button
          type="submit"
          disabled={
            busy ||
            Boolean(preview && !preview.valid) ||
            (mode === "edit" && !dirty) ||
            (dangerous && !confirmDangerous) ||
            (targetChanged && !confirmTarget) ||
            ((mode === "create" || targetChanged) && !selectedUsable)
          }
          aria-busy={busy}
        >
          {busy ? (
            <>
              <PartOpsLoader size="sm" />
              {mode === "edit" ? "Saving…" : "Creating job…"}
            </>
          ) : (
            submitLabel
          )}
        </Button>
        {mode === "edit" && onCancel ? (
          <Button type="button" variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
        ) : (
          <Button type="button" variant="secondary" onClick={reset}>
            Reset
          </Button>
        )}
      </div>
      {mode === "edit" && !dirty ? (
        <p className="text-sm font-semibold text-pj-muted">No changes to save</p>
      ) : null}
      {ok ? <p className="text-sm font-semibold text-[var(--success-fg)]">{ok}</p> : null}
      {error ? <p className="text-sm font-semibold text-[var(--error-fg)]">{error}</p> : null}
    </form>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[14px] font-semibold text-[var(--content-primary)]">
      {children}
    </div>
  );
}

/**
 * Wraps the control in a <label> so it gets an accessible name (screen
 * readers / click-to-focus). `group` is for compound controls that already
 * contain their own label (e.g. the Enabled switch).
 */
function Field({
  label,
  children,
  group = false,
}: {
  label: string;
  children: React.ReactNode;
  group?: boolean;
}) {
  if (group) {
    return (
      <div role="group" aria-label={label}>
        <Label>{label}</Label>
        {children}
      </div>
    );
  }
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-medium text-[var(--content-secondary)]">{label}</span>
      {children}
    </label>
  );
}
