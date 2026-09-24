"use client";

import { useCallback, useEffect, useState } from "react";
import { AppSidebar } from "@/components/shell/sidebar";
import { api } from "@/lib/api";
import type { SchedulerStatus } from "@/lib/types";

export function AppShell({ children }: { children: React.ReactNode }) {
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

  return (
    <div className="flex min-h-screen bg-[#f3f0e9] text-[#1c2730]">
      <AppSidebar
        schedulerOk={schedulerOk}
        scheduler={scheduler}
        schedulerMessage={schedulerMessage}
        dbConnected={dbConnected}
        onRefreshScheduler={() => void refresh()}
      />
      <div className="flex min-h-screen min-w-0 flex-1 flex-col">
        <main className="flex-1 px-8 py-7">{children}</main>
        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-[#ebe8e1] px-8 py-3 text-[0.68rem] text-[#8b958f]">
          <strong className="font-extrabold text-[#5e6d66]">Partition Manager</strong>
          <span>
            Scheduler heartbeat{" "}
            {schedulerOk
              ? scheduler?.last_refresh_at
                ? "ok"
                : scheduler?.last_refresh_result || "ok"
              : "unavailable"}
          </span>
          <span>
            {schedulerOk && scheduler?.scheduler_active
              ? "All systems operational"
              : schedulerOk
                ? "Scheduler idle"
                : "Scheduler unavailable"}
          </span>
        </footer>
      </div>
    </div>
  );
}
