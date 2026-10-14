import { cn } from "@/lib/utils";

export function Panel({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-primary)]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function PanelHeader({
  title,
  description,
  actions,
  className,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start justify-between gap-2 border-b border-[var(--border-default)] px-3 py-2.5",
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="text-[14px] font-semibold text-[var(--content-primary)]">{title}</h2>
        {description ? (
          <p className="mt-0.5 text-[11px] text-[var(--content-secondary)]">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function QueryState({
  loading,
  error,
  empty,
  emptyMessage = "No data.",
  children,
}: {
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyMessage?: string;
  children: React.ReactNode;
}) {
  if (loading) {
    return (
      <div className="px-3 py-8 text-center text-[12px] text-[var(--content-secondary)]">
        Loading…
      </div>
    );
  }
  if (error) {
    return (
      <div className="px-3 py-6 text-[12px] font-medium text-[var(--error-fg)]">{error}</div>
    );
  }
  if (empty) {
    return (
      <div className="px-3 py-8 text-center text-[12px] text-[var(--content-secondary)]">
        {emptyMessage}
      </div>
    );
  }
  return <>{children}</>;
}
