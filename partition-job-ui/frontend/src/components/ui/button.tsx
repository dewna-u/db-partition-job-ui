import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[9px] text-[0.72rem] font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-pj-primary focus-visible:ring-offset-2 focus-visible:ring-offset-pj-bg disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        default:
          "bg-pj-primary text-white shadow-[0_8px_20px_color-mix(in_srgb,var(--pj-primary)_17%,transparent)] hover:opacity-90",
        secondary:
          "border border-pj-line bg-pj-card text-pj-ink hover:border-pj-muted hover:bg-pj-surface",
        outline:
          "border border-pj-line bg-transparent text-pj-ink hover:bg-pj-surface",
        ghost: "text-pj-muted hover:bg-pj-table-head hover:text-pj-ink",
        success:
          "bg-pj-ok text-white shadow-[0_8px_20px_color-mix(in_srgb,var(--pj-ok)_17%,transparent)] hover:opacity-90",
        danger: "bg-pj-warn text-white hover:opacity-90",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 px-3",
        lg: "h-10 px-5",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";
