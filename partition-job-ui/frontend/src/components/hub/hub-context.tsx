"use client";

/**
 * Phase 5E — shared hub state (ONE hub: this PartOps instance's targets).
 *
 * Polling budget (conservative; Phase 5F optimises further):
 *  - scheduler status + DB readiness: every 30 s (unchanged shell cycle)
 *  - target registry: on mount and every 5 min (DBA-managed, changes rarely)
 *  - target health: ON DEMAND only, cached 60 s per target (each check opens
 *    a short read-only connection to that database)
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { DatabaseTarget, SchedulerStatus, TargetHealth } from "@/lib/types";

const TARGETS_REFRESH_MS = 5 * 60 * 1000;
const SCHEDULER_REFRESH_MS = 30 * 1000;
const HEALTH_TTL_MS = 60 * 1000;

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

  const refreshScheduler = useCallback(async () => {
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

  useEffect(() => {
    void refreshScheduler();
    const id = window.setInterval(() => void refreshScheduler(), SCHEDULER_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [refreshScheduler]);

  useEffect(() => {
    void refreshTargets();
    const id = window.setInterval(() => void refreshTargets(), TARGETS_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [refreshTargets]);

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
