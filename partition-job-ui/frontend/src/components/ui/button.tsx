import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-[6px] text-[12px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--client-primary)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--surface-secondary)] disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        default:
          "bg-[var(--client-primary)] text-[var(--content-accent)] hover:bg-[var(--client-primary-h)]",
        secondary:
          "border border-[var(--border-default)] bg-[var(--surface-primary)] text-[var(--content-primary)] hover:bg-[var(--hover-surface-1)]",
        outline:
          "border border-[var(--border-default)] bg-transparent text-[var(--content-primary)] hover:bg-[var(--hover-surface-1)]",
        ghost:
          "text-[var(--content-secondary)] hover:bg-[var(--hover-surface-1)] hover:text-[var(--content-primary)]",
        success:
          "bg-[var(--success-fg)] text-white hover:opacity-90",
        danger:
          "bg-[var(--error-fg)] text-white hover:opacity-90",
      },
      size: {
        default: "h-8 px-3",
        sm: "h-7 px-2.5 text-[11px]",
        lg: "h-9 px-4",
        icon: "h-8 w-8",
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
