"use client";

import { Button } from "@/components/ui/button";
import type { PageInfo } from "@/lib/types";

/** Phase 5F.1 — previous / next for server-side pages (no total count). */
export function Pager({
  page,
  count,
  busy,
  onOffset,
  label,
}: {
  page: PageInfo | null;
  count: number;
  busy?: boolean;
  onOffset: (offset: number) => void;
  label: string;
}) {
  if (!page || (page.previous_offset == null && page.next_offset == null)) return null;
  const from = count ? page.offset + 1 : 0;
  const to = page.offset + count;
  return (
    <nav
      className="flex items-center justify-end gap-2 px-3 py-2 text-[11px] text-[var(--content-secondary)]"
      aria-label={`${label} pages`}
    >
      <span className="tabular-nums">
        Rows {from}–{to}
      </span>
      <Button
        variant="secondary"
        size="sm"
        disabled={busy || page.previous_offset == null}
        onClick={() => page.previous_offset != null && onOffset(page.previous_offset)}
      >
        Previous
      </Button>
      <Button
        variant="secondary"
        size="sm"
        disabled={busy || page.next_offset == null}
        onClick={() => page.next_offset != null && onOffset(page.next_offset)}
      >
        Next
      </Button>
    </nav>
  );
}
