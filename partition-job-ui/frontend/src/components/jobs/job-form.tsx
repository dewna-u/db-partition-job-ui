"use client";

import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import type { JobCreatePayload, JobUpdatePayload } from "@/lib/types";

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
    setBusy(true);
    setError(null);
    setOk(null);
    try {
      if (mode === "edit" && jobId != null) {
        const body: JobUpdatePayload = {
          ...payload,
          confirm_dangerous: confirmDangerous,
        };
        const res = await api.updateJob(jobId, body);
        setOk(res.message);
        onSuccess?.(res.message);
      } else {
        const res = await api.createJob(payload);
        setOk(res.message);
        onSuccess?.(res.message);
      }
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : mode === "edit"
            ? "Update failed"
            : "Create failed",
      );
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
  }

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
        <SectionTitle>Basic information</SectionTitle>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label="Job name">
            <Input
              value={values.job_name}
              onChange={(e) => set("job_name", e.target.value)}
              required
            />
          </Field>
          <Field label="Enabled">
            <label className="flex h-9 items-center gap-3 text-sm font-semibold">
              <span
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${
                  values.is_enabled ? "bg-[#1f8a64]" : "bg-[#c5c0b6]"
                }`}
              >
                <input
                  type="checkbox"
                  className="peer sr-only"
                  checked={values.is_enabled}
                  onChange={(e) => set("is_enabled", e.target.checked)}
                />
                <span
                  className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
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
                className={`rounded-lg border px-3 py-2 text-xs font-bold ${
                  (op === "CREATE") === values.is_create
                    ? op === "CREATE"
                      ? "border-[#165dff] bg-[#e7edff] text-[#165dff]"
                      : "border-[#c76b2d] bg-[#fff0e5] text-[#c76b2d]"
                    : "border-[#d1cdc4] bg-white text-[#5f6d68]"
                }`}
              >
                {op}
              </button>
            ))}
          </div>
          {!values.is_create ? (
            <p className="mt-2 rounded-lg border border-[#e5c4a8] bg-[#fff0e5] px-3 py-2 text-xs font-bold text-[#8a4b16]">
              DROP operation: review retention carefully. Wrong values can remove
              production data.
            </p>
          ) : null}
        </div>
      </section>

      <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
        <SectionTitle>Target</SectionTitle>
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

      <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
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

      <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
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
          <div className="rounded-xl border border-[#cbd7ef] bg-[#eef3ff] px-3 py-2 text-xs text-[#40526f]">
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

      <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
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

      {dangerous ? (
        <label className="flex items-start gap-2 rounded-xl border border-[#e5c4a8] bg-[#fff0e5] px-3 py-3 text-xs font-semibold text-[#8a4b16]">
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
            (dangerous && !confirmDangerous)
          }
        >
          {busy
            ? mode === "edit"
              ? "Saving…"
              : "Creating…"
            : submitLabel}
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
      {ok ? <p className="text-sm font-semibold text-[#1f8a64]">{ok}</p> : null}
      {error ? <p className="text-sm font-semibold text-[#c4473a]">{error}</p> : null}
    </form>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
      {children}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <Label>{label}</Label>
      {children}
    </div>
  );
}
