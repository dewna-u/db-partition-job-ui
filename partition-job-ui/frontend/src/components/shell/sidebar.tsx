"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  History,
  LayoutDashboard,
  PlusCircle,
  RefreshCw,
  Workflow,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { StatusDot } from "@/components/ui/badge";
import type { SchedulerStatus } from "@/lib/types";
import { formatAge } from "@/lib/format";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/convert", label: "Convert job", icon: Workflow },
  { href: "/jobs/new", label: "Create new", icon: PlusCircle },
  { href: "/jobs", label: "Configured jobs", icon: Activity },
  { href: "/history", label: "Execution history", icon: History },
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
    <aside className="flex h-screen w-[248px] shrink-0 flex-col border-r border-[#cfcac0] bg-[#202c34] text-[#f3f1e9]">
      <div className="border-b border-[#45535a] px-4 pb-4 pt-5">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 -rotate-6 place-items-center rounded-[10px] bg-[#e5ff5c] text-sm font-black text-[#202c34]">
            P
          </div>
          <div>
            <div className="text-[0.95rem] font-extrabold tracking-tight">
              Partition <span className="text-[#e5ff5c]">Manager</span>
            </div>
            <div className="mt-0.5 text-[0.62rem] text-[#aab5b1]">
              PostgreSQL control plane
            </div>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-2.5 rounded-[11px] border border-[#526067] bg-[#293840] px-3 py-2.5">
          <div className="grid h-7 w-7 place-items-center rounded-lg bg-[#e5ff5c] text-[0.58rem] font-extrabold text-[#202c34]">
            MO
          </div>
          <div>
            <div className="text-[0.58rem] uppercase tracking-[0.12em] text-[#aab5b1]">
              Workspace
            </div>
            <div className="text-xs font-bold">mubasher_oms</div>
          </div>
        </div>
      </div>

      <div className="px-3 pt-4">
        <div className="mb-2 px-2 text-[0.58rem] font-extrabold uppercase tracking-[0.14em] text-[#8a9691]">
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
                    ? "bg-[#e5ff5c] text-[#202c34]"
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

      <div className="mt-auto space-y-3 border-t border-[#45535a] px-3 py-4">
        <div className="px-2 text-[0.58rem] font-extrabold uppercase tracking-[0.14em] text-[#8a9691]">
          System
        </div>
        <div className="rounded-[11px] border border-[#526067] bg-[#293840] px-3 py-3">
          <div className="flex items-center gap-2 text-xs font-bold">
            <StatusDot tone={active ? "green" : schedulerOk ? "amber" : "red"} />
            Scheduler{" "}
            {active ? "Online" : schedulerOk ? "Idle" : "Offline"}
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
              onClick={onRefreshScheduler}
              className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-[#526067] px-2 py-1 text-[0.62rem] font-bold text-[#d5ddd9] hover:bg-[#334049]"
            >
              <RefreshCw className="h-3 w-3" /> Refresh status
            </button>
          ) : null}
        </div>
        <div className="px-2 text-[0.62rem] leading-relaxed text-[#8a9691]">
          Readiness and realtime scheduler details live with each page’s system
          panels.
        </div>
      </div>
    </aside>
  );
}
