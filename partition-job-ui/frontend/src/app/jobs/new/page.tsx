"use client";

import { PageHeader } from "@/components/shell/page-header";
import { JobForm } from "@/components/jobs/job-form";

export default function CreateJobPage() {
  return (
    <div className="pj-enter mx-auto max-w-3xl space-y-3">
      <PageHeader
        title="Create job"
        subtitle="Define target table, operation, schedule, and partition settings."
        showActions={false}
      />
      <JobForm />
    </div>
  );
}
