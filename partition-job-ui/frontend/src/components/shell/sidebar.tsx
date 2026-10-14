"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  History,
  LayoutDashboard,
  PlusCircle,
  RefreshCw,
  ShieldCheck,
  Workflow,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { StatusDot } from "@/components/ui/badge";
import type { SchedulerStatus } from "@/lib/types";
import { formatAge } from "@/lib/format";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/jobs", label: "Partition jobs", icon: Activity },
  { href: "/jobs/new", label: "Create job", icon: PlusCircle },
  { href: "/convert", label: "Convert job", icon: Workflow },
  { href: "/history", label: "History", icon: History },
  { href: "/system", label: "System", icon: ShieldCheck },
] as const;

export function AppSidebar({
  schedulerOk,
  scheduler,
  schedulerMessage,
  dbConnected,
  onRefreshScheduler,
}: {
  schedulerOk: boolean;
  scheduler: SchedulerStatus | null;
  schedulerMessage?: string;
  dbConnected: boolean;
  onRefreshScheduler?: () => void;
}) {
  const pathname = usePathname();
  const active = Boolean(schedulerOk && scheduler?.scheduler_active);
  const sync = formatAge(scheduler?.last_refresh_at);

  return (
    <aside className="flex h-[calc(100vh-52px)] w-[228px] shrink-0 flex-col border-r border-[var(--border-default)] bg-[var(--surface-chrome)]">
      <div className="border-b border-[var(--border-default)] px-3 py-2.5">
        <Link href="/" className="block" aria-label="PartOps home">
          <Image
            src="/branding/partops-logo-transparent.png"
            alt="PartOps"
            width={280}
            height={64}
            className="h-8 w-auto max-w-[140px] object-contain object-left"
            priority
          />
        </Link>
      </div>

      <div className="px-3 pt-3">
        <div className="mb-1.5 px-2 text-[11px] font-medium uppercase tracking-[0.08em] text-[var(--content-tertiary)]">
          Manage
        </div>
        <nav className="space-y-0.5">
          {NAV.map((item) => {
            const Icon = item.icon;
            const isActive =
              item.href === "/"
                ? pathname === "/"
                : item.href === "/jobs"
                  ? pathname === "/jobs"
                  : pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "flex items-center gap-2 rounded-[6px] px-2.5 py-1.5 text-[14px] font-medium transition-colors",
                  isActive
                    ? "bg-[var(--info-bg)] text-[var(--client-primary)]"
                    : "text-[var(--content-secondary)] hover:bg-[var(--hover-surface-1)] hover:text-[var(--content-primary)]",
                )}
              >
                <Icon className="h-4 w-4 shrink-0 opacity-90" aria-hidden />
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>

      <div className="mt-auto space-y-2 border-t border-[var(--border-default)] px-3 py-3">
        <div className="px-2 text-[11px] font-medium uppercase tracking-[0.08em] text-[var(--content-tertiary)]">
          Status
        </div>
        <div className="rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-primary)] px-2.5 py-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 text-[12px] font-medium text-[var(--content-primary)]">
              <StatusDot tone={active ? "green" : schedulerOk ? "amber" : "red"} />
              Scheduler {active ? "online" : schedulerOk ? "idle" : "not reporting"}
            </div>
            {onRefreshScheduler ? (
              <button
                type="button"
                aria-label="Refresh scheduler status"
                onClick={onRefreshScheduler}
                className="rounded-[4px] p-1 text-[var(--content-tertiary)] hover:bg-[var(--hover-surface-1)] hover:text-[var(--content-primary)]"
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </button>
            ) : null}
          </div>
          <div className="mt-1.5 flex items-center gap-1.5 text-[12px] text-[var(--content-secondary)]">
            <StatusDot tone={dbConnected ? "green" : "red"} />
            Database {dbConnected ? "connected" : "unavailable"}
          </div>
          <div className="mt-1.5 text-[11px] text-[var(--content-tertiary)]">
            Last sync {schedulerOk ? sync : "unavailable"}
          </div>
          {!schedulerOk && schedulerMessage ? (
            <div className="mt-1 line-clamp-2 text-[11px] text-[var(--warning-fg)]">
              {schedulerMessage}
            </div>
          ) : null}
          <Link
            href="/system"
            className="mt-2 inline-block text-[11px] font-medium text-[var(--content-link)] hover:text-[var(--content-link-h)]"
          >
            Open system
          </Link>
        </div>
      </div>
    </aside>
  );
}
