"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  ChevronRight,
  History,
  LayoutDashboard,
  PlusCircle,
  RefreshCw,
  ShieldCheck,
  Workflow,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { StatusDot } from "@/components/ui/badge";
import { ThemeToggle } from "@/components/theme/theme-toggle";
import type { SchedulerStatus } from "@/lib/types";
import { formatAge } from "@/lib/format";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/convert", label: "Convert job", icon: Workflow },
  { href: "/jobs/new", label: "Create new", icon: PlusCircle },
  { href: "/jobs", label: "Configured jobs", icon: Activity },
  { href: "/history", label: "Execution history", icon: History },
  { href: "/system", label: "System readiness", icon: ShieldCheck },
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
    <aside className="flex h-screen w-[248px] shrink-0 flex-col border-r border-pj-sidebar-border bg-pj-sidebar text-[#f3f1e9]">
      <div className="border-b border-pj-sidebar-border px-4 pb-4 pt-5">
        <Link href="/" className="flex items-center gap-2.5 rounded-lg outline-offset-2">
          <Image
            src="/branding/partops-mark.svg"
            alt=""
            width={36}
            height={36}
            className="h-9 w-9 shrink-0"
            priority
          />
          <div className="min-w-0">
            <div className="truncate text-[0.95rem] font-extrabold tracking-tight">
              Part<span className="text-pj-lime">Ops</span>
            </div>
            <div className="mt-0.5 truncate text-[0.62rem] text-[#aab5b1]">
              EDB Partition Operations
            </div>
          </div>
        </Link>
      </div>

      <div className="px-3 pt-4">
        <div className="mb-2 px-2 text-[0.58rem] font-extrabold uppercase tracking-[0.14em] text-pj-sidebar-muted">
          Manage
        </div>
        <nav className="space-y-1">
          {NAV.map((item) => {
            const Icon = item.icon;
            const isActive =
              item.href === "/"
                ? pathname === "/"
                : pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[0.78rem] font-semibold transition-colors",
                  isActive
                    ? "bg-pj-lime text-pj-sidebar"
                    : "text-[#d5ddd9] hover:bg-[#293840] hover:text-white",
                )}
              >
                <Icon className="h-4 w-4 shrink-0 opacity-90" />
                {item.label}
              </Link>
            );
          })}
        </nav>
      </div>

      <div className="mt-auto space-y-3 border-t border-pj-sidebar-border px-3 py-4">
        <div className="flex items-center justify-between px-2">
          <div className="text-[0.58rem] font-extrabold uppercase tracking-[0.14em] text-pj-sidebar-muted">
            Appearance
          </div>
          <ThemeToggle />
        </div>

        <div className="px-2 text-[0.58rem] font-extrabold uppercase tracking-[0.14em] text-pj-sidebar-muted">
          System
        </div>
        <Link
          href="/system"
          className="block rounded-[11px] border border-[#526067] bg-[#293840] px-3 py-3 transition-colors hover:border-[#6a7a82] hover:bg-[#2f3d45]"
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs font-bold">
              <StatusDot tone={active ? "green" : schedulerOk ? "amber" : "red"} />
              Scheduler{" "}
              {active ? "Online" : schedulerOk ? "Idle" : "Offline"}
            </div>
            <ChevronRight className="h-3.5 w-3.5 text-pj-sidebar-muted" />
          </div>
          <div className="mt-2 flex items-center gap-2 text-xs font-bold">
            <StatusDot tone={dbConnected ? "green" : "red"} />
            Database {dbConnected ? "Connected" : "Unavailable"}
          </div>
          <div className="mt-2 text-[0.65rem] text-[#aab5b1]">
            Last sync {schedulerOk ? sync : "unavailable"}
          </div>
          {!schedulerOk && schedulerMessage ? (
            <div className="mt-1 line-clamp-2 text-[0.62rem] text-[#c9a27a]">
              {schedulerMessage}
            </div>
          ) : null}
          {onRefreshScheduler ? (
            <button
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                onRefreshScheduler();
              }}
              className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-[#526067] px-2 py-1 text-[0.62rem] font-bold text-[#d5ddd9] hover:bg-[#334049]"
            >
              <RefreshCw className="h-3 w-3" /> Refresh status
            </button>
          ) : null}
          <div className="mt-2 text-[0.62rem] font-semibold text-pj-lime">
            Open system readiness →
          </div>
        </Link>
      </div>
    </aside>
  );
}
