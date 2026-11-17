"use client";

import Image from "next/image";
import Link from "next/link";
import { AppSidebar } from "@/components/shell/sidebar";
import { HubProvider, useHub } from "@/components/hub/hub-context";
import { ThemeProvider } from "@/components/theme/theme-provider";
import { ThemeToggle } from "@/components/theme/theme-toggle";

function AppShellInner({ children }: { children: React.ReactNode }) {
  const {
    schedulerOk,
    scheduler,
    schedulerMessage,
    dbConnected,
    targets,
    refreshScheduler,
  } = useHub();

  const heartbeat =
    schedulerOk
      ? scheduler?.last_refresh_at
        ? "ok"
        : scheduler?.last_refresh_result || "ok"
      : "unavailable";

  return (
    <div className="flex min-h-screen flex-col bg-[var(--surface-secondary)] text-[var(--content-primary)]">
      <header className="sticky top-0 z-30 flex h-[52px] shrink-0 items-center justify-between gap-3 border-b border-[var(--border-default)] bg-[var(--surface-chrome)] px-4">
        <Link
          href="/"
          className="flex min-w-0 items-center"
          aria-label="PartOps home"
        >
          <Image
            src="/branding/partops-logo-transparent.png"
            alt="PartOps"
            width={280}
            height={64}
            priority
            className="h-8 w-auto max-w-[140px] object-contain object-left"
          />
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
          targets={targets}
          onRefreshScheduler={() => void refreshScheduler()}
        />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <main className="flex-1 px-5 py-4 md:px-6">{children}</main>
          <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--border-default)] px-5 py-2 text-[11px] text-[var(--content-tertiary)] md:px-6">
            <span className="font-medium text-[var(--content-secondary)]">PartOps</span>
            <span>GTN EDB Partition Operations Platform · this hub only</span>
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
      <HubProvider>
        <AppShellInner>{children}</AppShellInner>
      </HubProvider>
    </ThemeProvider>
  );
}
