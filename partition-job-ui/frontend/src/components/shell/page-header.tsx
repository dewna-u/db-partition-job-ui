"use client";

import { useEffect } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  subtitle,
  count,
  showActions = true,
  actions,
  className,
}: {
  title: string;
  subtitle?: string;
  count?: string | number;
  showActions?: boolean;
  actions?: React.ReactNode;
  className?: string;
}) {
  useEffect(() => {
    document.title = `PartOps | ${title}`;
  }, [title]);

  return (
    <div
      className={cn(
        "mb-4 flex flex-wrap items-start justify-between gap-3",
        className,
      )}
    >
      <div className="min-w-0 max-w-3xl">
        <div className="flex flex-wrap items-baseline gap-2">
          <h1 className="text-[24px] font-semibold tracking-tight text-[var(--content-primary)]">
            {title}
          </h1>
          {count !== undefined && count !== null ? (
            <span className="text-[12px] font-medium tabular-nums text-[var(--content-secondary)]">
              {count}
            </span>
          ) : null}
        </div>
        {subtitle ? (
          <p className="mt-1 max-w-2xl text-[12px] text-[var(--content-secondary)]">
            {subtitle}
          </p>
        ) : null}
      </div>
      {actions ? (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      ) : showActions ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="secondary" size="sm" asChild>
            <a href={api.exportCsvUrl()} download="partition_jobs.csv">
              Export
            </a>
          </Button>
          <Button size="sm" asChild>
            <Link href="/jobs/new">Create job</Link>
          </Button>
        </div>
      ) : null}
    </div>
  );
}
