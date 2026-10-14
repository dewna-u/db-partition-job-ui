"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-[var(--surface-overlay)]" />
        <Dialog.Content
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-[var(--border-default)] bg-[var(--surface-primary)] shadow-modal outline-none",
            wide ? "max-w-xl" : "max-w-md",
          )}
        >
          <div className="flex items-start justify-between border-b border-[var(--border-default)] px-4 py-3">
            <div className="min-w-0 pr-3">
              <Dialog.Title className="text-[16px] font-semibold tracking-tight text-[var(--content-primary)]">
                {title}
              </Dialog.Title>
              {description ? (
                <Dialog.Description className="mt-0.5 text-[12px] text-[var(--content-secondary)]">
                  {description}
                </Dialog.Description>
              ) : (
                <Dialog.Description className="sr-only">{title}</Dialog.Description>
              )}
            </div>
            <Dialog.Close
              aria-label="Close"
              className="rounded-[6px] p-1.5 text-[var(--content-secondary)] hover:bg-[var(--hover-surface-1)] hover:text-[var(--content-primary)]"
            >
              <X className="h-4 w-4" />
            </Dialog.Close>
          </div>
          <div className="flex-1 overflow-y-auto px-4 py-3">{children}</div>
          {footer ? (
            <div className="flex items-center justify-end gap-2 border-t border-[var(--border-default)] bg-[var(--surface-tertiary)] px-4 py-2.5">
              {footer}
            </div>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
