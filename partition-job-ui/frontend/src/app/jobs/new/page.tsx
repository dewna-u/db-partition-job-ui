"use client";

import { PageHeader } from "@/components/shell/page-header";
import { JobForm } from "@/components/jobs/job-form";

export default function CreateJobPage() {
  return (
    <div className="pj-enter mx-auto max-w-4xl space-y-5">
      <PageHeader
        title="Create a new partition job"
        subtitle="Store one parameterized configuration row. The dedicated scheduler backend triggers it at next_run_time."
        showActions={false}
      />
      <JobForm />
    </div>
  );
}
