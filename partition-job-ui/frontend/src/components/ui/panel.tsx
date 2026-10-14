import { cn } from "@/lib/utils";
import { PartOpsLoader } from "@/components/ui/partops-loader";

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
  loadingLabel = "Loading…",
  children,
}: {
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyMessage?: string;
  loadingLabel?: string;
  children: React.ReactNode;
}) {
  if (loading) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 px-3 py-10"
        aria-busy="true"
        role="status"
      >
        <PartOpsLoader size="md" />
        <span className="text-[12px] text-[var(--content-secondary)]">{loadingLabel}</span>
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
