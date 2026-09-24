"use client";

import { useMemo, useState } from "react";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { JobForm, type JobFormValues } from "@/components/jobs/job-form";
import { api, ApiError } from "@/lib/api";

type Step = 1 | 2 | 3 | 4;

export default function ConvertPage() {
  const [step, setStep] = useState<Step>(1);
  const [jobId, setJobId] = useState("1");
  const [browse, setBrowse] = useState<Record<string, unknown>[]>([]);
  const [details, setDetails] = useState<Record<string, unknown> | null>(null);
  const [stepChoices, setStepChoices] = useState<Record<string, unknown>[]>([]);
  const [selectedStep, setSelectedStep] = useState<number | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const initial = useMemo(() => detailsToForm(details), [details]);

  async function loadBrowse() {
    setError(null);
    try {
      const res = await api.pgagentJobs();
      setBrowse(res.jobs || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load pgAgent jobs");
    }
  }

  async function loadDetails(stepId?: number) {
    setBusy(true);
    setError(null);
    try {
      const res = await api.pgagentDetails(Number(jobId), stepId);
      const d = res.details;
      setDetails(d);
      const choices = (d.step_choices as Record<string, unknown>[]) || [];
      setStepChoices(choices);
      if (choices.length && stepId == null) {
        setStep(2);
      } else {
        setStep(3);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load job details");
      setStep(2);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pj-enter mx-auto max-w-4xl space-y-5">
      <PageHeader
        title="Convert an existing job"
        subtitle="Read an old table-specific pgAgent job and store one parameterized configuration row. The original pgAgent job is never modified."
        showActions={false}
      />

      <div className="flex flex-wrap gap-2">
        {[
          "Select existing job",
          "Detected configuration",
          "Review",
          "Create partition job",
        ].map((label, index) => {
          const n = (index + 1) as Step;
          const active = step === n;
          const done = step > n;
          return (
            <span
              key={label}
              className={`rounded-full border px-3 py-1 text-xs font-bold ${
                active
                  ? "border-[#b7c9f5] bg-[#e7edff] text-[#165dff]"
                  : done
                    ? "border-[#b7d9c7] bg-[#e4f3ea] text-[#1f8a64]"
                    : "border-[#d1cdc4] bg-white text-[#718078]"
              }`}
            >
              {label}
            </span>
          );
        })}
      </div>

      <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
        <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
          Select existing job
        </div>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <div className="w-40">
            <Label>pgAgent Job ID</Label>
            <Input
              type="number"
              min={1}
              value={jobId}
              onChange={(e) => setJobId(e.target.value)}
            />
          </div>
          <Button disabled={busy} onClick={() => void loadDetails()}>
            {busy ? "Loading…" : "Load job details"}
          </Button>
          <Button variant="secondary" type="button" onClick={() => void loadBrowse()}>
            Browse pgAgent jobs
          </Button>
        </div>
        {browse.length ? (
          <div className="mt-4 overflow-hidden rounded-xl border border-pj-line">
            <table className="w-full text-left text-xs">
              <thead className="bg-[#f3f0e9] text-[0.62rem] uppercase text-[#7e8982]">
                <tr>
                  <th className="px-3 py-2">ID</th>
                  <th className="px-3 py-2">Name</th>
                  <th className="px-3 py-2">Enabled</th>
                </tr>
              </thead>
              <tbody>
                {browse.slice(0, 40).map((row) => (
                  <tr
                    key={String(row.job_id)}
                    className="cursor-pointer border-t border-[#ebe8e1] hover:bg-[#f7f5ef]"
                    onClick={() => {
                      setJobId(String(row.job_id));
                      void loadDetails();
                    }}
                  >
                    <td className="px-3 py-2 font-bold">{String(row.job_id)}</td>
                    <td className="px-3 py-2">{String(row.job_name || "")}</td>
                    <td className="px-3 py-2">{String(row.enabled)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {stepChoices.length ? (
        <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
          <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
            Multiple steps found
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {stepChoices.map((choice) => (
              <Button
                key={String(choice.step_id)}
                variant={selectedStep === choice.step_id ? "default" : "secondary"}
                onClick={() => {
                  setSelectedStep(Number(choice.step_id));
                  void loadDetails(Number(choice.step_id));
                }}
              >
                {String(choice.step_name || `Step ${choice.step_id}`)}
              </Button>
            ))}
          </div>
        </section>
      ) : null}

      {details ? (
        <section className="rounded-card border border-pj-line bg-pj-card p-5 shadow-card">
          <div className="mb-3 flex items-center gap-2">
            <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
              Detected configuration
            </div>
            <Badge tone="info">Automatically detected</Badge>
            {Array.isArray(details.warnings) && details.warnings.length ? (
              <Badge tone="warn">Needs review</Badge>
            ) : null}
          </div>
          <dl className="grid gap-2 text-xs md:grid-cols-2">
            {[
              ["Job name", String((details.autofill as Record<string, unknown>)?.job_name || details.job_name || "—")],
              ["Target", targetFromAutofill(details)],
              ["Operation", opFromAutofill(details)],
              ["Schedule", String((details.autofill as Record<string, unknown>)?.job_schedule || "—")],
            ].map(([k, v]) => (
              <div key={k} className="rounded-lg border border-[#ebe8e1] bg-white px-3 py-2">
                <dt className="text-[0.58rem] font-bold uppercase tracking-[0.1em] text-[#8a958e]">
                  {k}
                </dt>
                <dd className="mt-1 font-semibold">{v}</dd>
              </div>
            ))}
          </dl>
          {Array.isArray(details.warnings) && details.warnings.length ? (
            <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-[#b8672d]">
              {(details.warnings as string[]).map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}

      {details && step >= 3 ? (
        <section className="space-y-3">
          <div className="text-[0.62rem] font-extrabold uppercase tracking-[0.13em] text-[#86928a]">
            Review & create
          </div>
          <JobForm
            key={JSON.stringify(initial)}
            initial={initial}
            submitLabel="Create partition job"
            onSuccess={() => setStep(4)}
          />
        </section>
      ) : null}

      {error ? (
        <p className="text-sm font-semibold text-[#c4473a]">{error}</p>
      ) : null}
    </div>
  );
}

function detailsToForm(details: Record<string, unknown> | null): Partial<JobFormValues> {
  if (!details) return {};
  const autofill = (details.autofill || {}) as Record<string, unknown>;
  const next = autofill.next_run_time
    ? String(autofill.next_run_time).replace(" ", "T").slice(0, 19)
    : "";
  return {
    job_name: String(autofill.job_name || details.job_name || ""),
    is_enabled: autofill.is_enabled !== false,
    is_create: autofill.is_create !== false && autofill.is_create !== 0,
    table_schema: String(autofill.table_schema || ""),
    table_name: String(autofill.table_name || ""),
    job_schedule: String(autofill.job_schedule || "0 0 2 * * *"),
    frequency_amount: Number(autofill.frequency_amount || 1),
    frequency_unit: String(autofill.frequency_unit || "day"),
    partition_unit: String(autofill.partition_unit || "day"),
    partition_period: Number(autofill.partition_period || 1),
    create_drop_amount: Number(autofill.create_drop_amount || 1),
    create_drop_unit: String(autofill.create_drop_unit || "month"),
    db_config: String(autofill.db_config || "{}"),
    next_run_time: next,
    auto_next_run: !next,
  };
}

function targetFromAutofill(details: Record<string, unknown>) {
  const a = (details.autofill || {}) as Record<string, unknown>;
  if (a.table_schema && a.table_name) return `${a.table_schema}.${a.table_name}`;
  return "Unresolved — needs review";
}

function opFromAutofill(details: Record<string, unknown>) {
  const a = (details.autofill || {}) as Record<string, unknown>;
  if ("is_create" in a) return a.is_create ? "CREATE" : "DROP";
  return "Unresolved — needs review";
}
