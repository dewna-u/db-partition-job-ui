import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "flex h-9 w-full rounded-lg border border-[#d1cdc4] bg-white px-3 py-2 text-[0.78rem] text-[#1c2730] placeholder:text-[#8a958e] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#165dff]/40 disabled:cursor-not-allowed disabled:opacity-50",
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
      "flex min-h-[96px] w-full rounded-lg border border-[#d1cdc4] bg-white px-3 py-2 font-mono text-[0.78rem] text-[#1c2730] placeholder:text-[#8a958e] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#165dff]/40",
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
        "mb-1.5 block text-[0.62rem] font-bold uppercase tracking-[0.12em] text-[#5f6d68]",
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
        "flex h-9 w-full rounded-lg border border-[#d1cdc4] bg-white px-3 text-[0.78rem] text-[#1c2730] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#165dff]/40",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
}
