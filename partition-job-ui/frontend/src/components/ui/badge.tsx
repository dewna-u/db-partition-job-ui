import { cn } from "@/lib/utils";

export function Badge({
  children,
  tone = "mute",
  className,
}: {
  children: React.ReactNode;
  tone?: "ok" | "warn" | "fail" | "info" | "mute" | "create" | "drop";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-2 py-0.5 text-[0.58rem] font-extrabold tracking-wide",
        tone === "ok" && "bg-[#e4f3ea] text-[#1f8a64]",
        tone === "warn" && "bg-[#fff0e5] text-[#b8672d]",
        tone === "fail" && "bg-[#fff0ee] text-[#c4473a]",
        tone === "info" && "bg-[#e7edff] text-[#165dff]",
        tone === "mute" && "bg-[#efece4] text-[#6d7973]",
        tone === "create" && "bg-[#e7edff] text-[#165dff]",
        tone === "drop" && "bg-[#fff0e5] text-[#c76b2d]",
        className,
      )}
    >
      {children}
    </span>
  );
}

export function StatusDot({
  tone,
  className,
}: {
  tone: "green" | "amber" | "red" | "blue" | "mute";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 rounded-[2px] rotate-45",
        tone === "green" && "bg-[#1f8a64]",
        tone === "amber" && "bg-[#c76b2d]",
        tone === "red" && "bg-[#c4473a]",
        tone === "blue" && "bg-[#165dff]",
        tone === "mute" && "bg-[#8a958e]",
        className,
      )}
    />
  );
}
