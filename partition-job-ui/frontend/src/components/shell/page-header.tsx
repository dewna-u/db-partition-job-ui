"use client";

import { useEffect } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";

export function PageHeader({
  title,
  subtitle,
  showActions = true,
}: {
  title: string;
  subtitle: string;
  showActions?: boolean;
}) {
  useEffect(() => {
    document.title = `PartOps | ${title}`;
  }, [title]);

  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="max-w-3xl">
        <div className="mb-2 inline-flex items-center gap-2 text-[0.62rem] font-extrabold uppercase tracking-[0.14em] text-pj-muted">
          <span className="h-1.5 w-1.5 rounded-full bg-pj-primary" />
          PartOps
        </div>
        <h1 className="text-[2.05rem] font-extrabold leading-none tracking-[-0.03em] text-pj-ink">
          {title}
        </h1>
        <p className="mt-2 max-w-2xl text-sm text-pj-muted">{subtitle}</p>
      </div>
      {showActions ? (
        <div className="flex items-center gap-2">
          <Button variant="secondary" asChild>
            <a href={api.exportCsvUrl()} download="partition_jobs.csv">
              Export
            </a>
          </Button>
          <Button asChild>
            <Link href="/jobs/new">+ New job</Link>
          </Button>
        </div>
      ) : null}
    </div>
  );
}
