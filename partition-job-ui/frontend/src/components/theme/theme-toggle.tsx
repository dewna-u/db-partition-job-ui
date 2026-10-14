"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme, type ThemeMode } from "@/components/theme/theme-provider";

const MODES: { mode: ThemeMode; label: string; Icon: typeof Sun }[] = [
  { mode: "light", label: "Light theme", Icon: Sun },
  { mode: "dark", label: "Dark theme", Icon: Moon },
  { mode: "system", label: "System theme", Icon: Monitor },
];

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();

  return (
    <div
      className={cn(
        "inline-flex rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-tertiary)] p-0.5",
        className,
      )}
      role="group"
      aria-label="Color theme"
    >
      {MODES.map(({ mode, label, Icon }) => {
        const active = theme === mode;
        return (
          <button
            key={mode}
            type="button"
            title={label}
            aria-label={label}
            aria-pressed={active}
            onClick={() => setTheme(mode)}
            className={cn(
              "inline-flex h-7 w-7 items-center justify-center rounded-[4px] transition-colors",
              active
                ? "bg-[var(--info-bg)] text-[var(--client-primary)]"
                : "text-[var(--content-tertiary)] hover:bg-[var(--hover-surface-2)] hover:text-[var(--content-primary)]",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
          </button>
        );
      })}
    </div>
  );
}
