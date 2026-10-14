"use client";

import Image from "next/image";
import { cn } from "@/lib/utils";

const SIZES = {
  sm: { px: 16, className: "h-4 w-4" },
  md: { px: 24, className: "h-6 w-6" },
  lg: { px: 44, className: "h-11 w-11" },
} as const;

export type PartOpsLoaderSize = keyof typeof SIZES;

export function PartOpsLoader({
  size = "md",
  className,
  label,
}: {
  size?: PartOpsLoaderSize;
  className?: string;
  /** Visible/assistive label; omit when parent already provides text */
  label?: string;
}) {
  const dim = SIZES[size];
  return (
    <span
      className={cn("inline-flex items-center gap-2", className)}
      role={label ? "status" : undefined}
      aria-live={label ? "polite" : undefined}
    >
      <Image
        src="/branding/partops-loader.png"
        alt=""
        width={dim.px}
        height={dim.px}
        className={cn("po-loader-spin shrink-0 object-contain", dim.className)}
        aria-hidden
        unoptimized
      />
      {label ? (
        <span className="text-[12px] text-[var(--content-secondary)]">{label}</span>
      ) : null}
    </span>
  );
}
