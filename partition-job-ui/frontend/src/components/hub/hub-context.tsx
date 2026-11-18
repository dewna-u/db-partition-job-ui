"use client";

/**
 * Phase 5E — shared hub state (ONE hub: this PartOps instance's targets).
 *
 * Polling budget (Phase 5F.1):
 *  - scheduler status: every 30 s (in-memory on the scheduler, no DB access)
 *  - control-DB readiness (sidebar dot): every 60 s (one DB connection each)
 *  - target registry: on mount and every 5 min (DBA-managed, changes rarely)
 *  - target health: ON DEMAND only, cached 60 s per target (each check opens
 *    a short read-only connection to that database)
 *  - while the browser tab is hidden nothing is polled; on return, anything
 *    older than its interval is refreshed once.
 *  - explicit refresh buttons always fetch immediately.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { DatabaseTarget, SchedulerStatus, TargetHealth } from "@/lib/types";

export const TARGETS_REFRESH_MS = 5 * 60 * 1000;
export const SCHEDULER_REFRESH_MS = 30 * 1000;
export const DB_READINESS_REFRESH_MS = 60 * 1000;
const HEALTH_TTL_MS = 60 * 1000;

/**
 * Run ``fn`` now and every ``intervalMs`` while the page is visible. Hidden
 * tabs skip ticks; becoming visible triggers one catch-up run if stale.
 */
function useVisiblePolling(fn: () => Promise<void>, intervalMs: number) {
  const lastRun = useRef(0);
  useEffect(() => {
    const run = () => {
      lastRun.current = Date.now();
      void fn();
    };
    run();
    const id = window.setInterval(() => {
      if (!document.hidden) run();
    }, intervalMs);
    const onVisibility = () => {
      if (!document.hidden && Date.now() - lastRun.current >= intervalMs) run();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [fn, intervalMs]);
}

type HealthEntry = { health: TargetHealth | null; error: string | null; at: number };

type HubState = {
  schedulerOk: boolean;
  scheduler: SchedulerStatus | null;
  schedulerMessage: string;
  dbConnected: boolean;
  targets: DatabaseTarget[] | null;
  targetsAvailable: boolean;
  targetsError: string | null;
  health: Record<number, HealthEntry>;
  refreshScheduler: () => Promise<void>;
  refreshTargets: () => Promise<void>;
  /** Cached unless force; returns the entry after loading. */
  loadHealth: (targetId: number, force?: boolean) => Promise<HealthEntry>;
  healthLoading: Record<number, boolean>;
};

const HubContext = createContext<HubState | null>(null);

export function HubProvider({ children }: { children: React.ReactNode }) {
  const [schedulerOk, setSchedulerOk] = useState(false);
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [schedulerMessage, setSchedulerMessage] = useState("");
  const [dbConnected, setDbConnected] = useState(false);
  const [targets, setTargets] = useState<DatabaseTarget[] | null>(null);
  const [targetsAvailable, setTargetsAvailable] = useState(true);
  const [targetsError, setTargetsError] = useState<string | null>(null);
  const [health, setHealth] = useState<Record<number, HealthEntry>>({});
  const [healthLoading, setHealthLoading] = useState<Record<number, boolean>>({});
  const healthRef = useRef<Record<number, HealthEntry>>({});
  const inflight = useRef<Record<number, Promise<HealthEntry> | undefined>>({});

  const pollScheduler = useCallback(async () => {
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
  }, []);

  const pollDb = useCallback(async () => {
    try {
      await api.readiness();
      setDbConnected(true);
    } catch {
      setDbConnected(false);
    }
  }, []);

  // Explicit refresh (buttons): both, immediately.
  const refreshScheduler = useCallback(async () => {
    await Promise.all([pollScheduler(), pollDb()]);
  }, [pollScheduler, pollDb]);

  const refreshTargets = useCallback(async () => {
    try {
      const res = await api.targets();
      setTargets(res.targets || []);
      setTargetsAvailable(Boolean(res.available));
      setTargetsError(res.available ? null : res.message);
    } catch (err) {
      setTargetsError(err instanceof Error ? err.message : "Targets unavailable");
    }
  }, []);

  const loadHealth = useCallback(async (targetId: number, force = false): Promise<HealthEntry> => {
    const cached = healthRef.current[targetId];
    if (!force && cached && Date.now() - cached.at < HEALTH_TTL_MS) return cached;
    const pending = inflight.current[targetId];
    if (pending) return pending;
    setHealthLoading((m) => ({ ...m, [targetId]: true }));
    const p = api
      .targetHealth(targetId)
      .then((res) => ({ health: res.health, error: null, at: Date.now() }))
      .catch((err: unknown) => ({
        health: null,
        error: err instanceof Error ? err.message : "Health check failed",
        at: Date.now(),
      }))
      .then((entry) => {
        healthRef.current = { ...healthRef.current, [targetId]: entry };
        setHealth(healthRef.current);
        setHealthLoading((m) => ({ ...m, [targetId]: false }));
        inflight.current[targetId] = undefined;
        return entry;
      });
    inflight.current[targetId] = p;
    return p;
  }, []);

  useVisiblePolling(pollScheduler, SCHEDULER_REFRESH_MS);
  useVisiblePolling(pollDb, DB_READINESS_REFRESH_MS);
  useVisiblePolling(refreshTargets, TARGETS_REFRESH_MS);

  const value = useMemo<HubState>(
    () => ({
      schedulerOk,
      scheduler,
      schedulerMessage,
      dbConnected,
      targets,
      targetsAvailable,
      targetsError,
      health,
      refreshScheduler,
      refreshTargets,
      loadHealth,
      healthLoading,
    }),
    [schedulerOk, scheduler, schedulerMessage, dbConnected, targets, targetsAvailable,
     targetsError, health, refreshScheduler, refreshTargets, loadHealth, healthLoading],
  );

  return <HubContext.Provider value={value}>{children}</HubContext.Provider>;
}

export function useHub(): HubState {
  const ctx = useContext(HubContext);
  if (!ctx) throw new Error("useHub must be used inside HubProvider");
  return ctx;
}
