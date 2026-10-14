"use client";

import { useCallback, useEffect, useState } from "react";
import Image from "next/image";
import Link from "next/link";
import { AppSidebar } from "@/components/shell/sidebar";
import { ThemeProvider } from "@/components/theme/theme-provider";
import { ThemeToggle } from "@/components/theme/theme-toggle";
import { api } from "@/lib/api";
import type { SchedulerStatus } from "@/lib/types";

function AppShellInner({ children }: { children: React.ReactNode }) {
  const [schedulerOk, setSchedulerOk] = useState(false);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [schedulerMessage, setSchedulerMessage] = useState("");
  const [dbConnected, setDbConnected] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const sched = await api.schedulerStatus();
      setSchedulerOk(sched.ok);
      setScheduler(sched.status);
      setSchedulerMessage(sched.message);
    } catch (err) {
      setSchedulerOk(false);
      setScheduler(null);
      setSchedulerMessage(err instanceof Error ? err.message : "Unavailable");
    }
    try {
      await api.readiness();
      setDbConnected(true);
    } catch {
      setDbConnected(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 30000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const heartbeat =
    schedulerOk
      ? scheduler?.last_refresh_at
        ? "ok"
        : scheduler?.last_refresh_result || "ok"
      : "unavailable";

  return (
    <div className="flex min-h-screen flex-col bg-[var(--surface-secondary)] text-[var(--content-primary)]">
      <header className="sticky top-0 z-30 flex h-[52px] shrink-0 items-center justify-between gap-3 border-b border-[var(--border-default)] bg-[var(--surface-chrome)] px-4">
        <Link href="/" className="flex min-w-0 items-center gap-2.5">
          <Image
            src="/branding/partops-mark.svg"
            alt=""
            width={28}
            height={28}
            className="h-7 w-7 shrink-0"
            priority
          />
          <div className="min-w-0">
            <div className="truncate text-[14px] font-semibold tracking-tight text-[var(--content-primary)]">
              PartOps
            </div>
            <div className="truncate text-[11px] text-[var(--content-tertiary)]">
              GTN EDB Partition Operations
            </div>
          </div>
        </Link>
        <div className="flex items-center gap-3">
          <div className="hidden text-[11px] text-[var(--content-secondary)] sm:block">
            Scheduler heartbeat{" "}
            <span className="font-medium tabular-nums text-[var(--content-primary)]">
              {heartbeat}
            </span>
          </div>
          <ThemeToggle />
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <AppSidebar
          schedulerOk={schedulerOk}
          scheduler={scheduler}
          schedulerMessage={schedulerMessage}
          dbConnected={dbConnected}
          onRefreshScheduler={() => void refresh()}
        />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <main className="flex-1 px-5 py-4 md:px-6">{children}</main>
          <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border-default)] px-5 py-2 text-[11px] text-[var(--content-tertiary)] md:px-6">
            <span className="font-medium text-[var(--content-secondary)]">PartOps</span>
            <span>GTN EDB Partition Operations Platform</span>
            <span>
              {schedulerOk && scheduler?.scheduler_active
                ? "Scheduler online"
                : schedulerOk
                  ? "Scheduler idle"
                  : "Scheduler not reporting"}
            </span>
          </footer>
        </div>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <AppShellInner>{children}</AppShellInner>
    </ThemeProvider>
  );
}
