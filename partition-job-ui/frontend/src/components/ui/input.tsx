import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "flex h-8 w-full rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-primary)] px-2.5 py-1.5 text-[12px] text-[var(--content-primary)] placeholder:text-[var(--content-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--client-primary)]/35 disabled:cursor-not-allowed disabled:bg-[var(--disabled-surface)] disabled:text-[var(--disabled-content)]",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "flex min-h-[88px] w-full rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-primary)] px-2.5 py-2 font-mono text-[12px] text-[var(--content-primary)] placeholder:text-[var(--content-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--client-primary)]/35",
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";

export function Label({
  className,
  ...props
}: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn(
        "mb-1 block text-[11px] font-medium text-[var(--content-secondary)]",
        className,
      )}
      {...props}
    />
  );
}

export function Select({
  className,
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "flex h-8 w-full rounded-[6px] border border-[var(--border-default)] bg-[var(--surface-primary)] px-2.5 text-[12px] text-[var(--content-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--client-primary)]/35",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
}
