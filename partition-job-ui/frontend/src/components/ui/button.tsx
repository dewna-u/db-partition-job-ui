import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[9px] text-[0.72rem] font-bold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#165dff] focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        default:
          "bg-[#165dff] text-white shadow-[0_8px_20px_#165dff2b] hover:bg-[#0f4fd6]",
        secondary:
          "border border-[#c9c6bd] bg-[#fbfaf7] text-[#3f4e4a] hover:border-[#7f8d87] hover:bg-white",
        outline:
          "border border-[#c9c6bd] bg-transparent text-[#3f4e4a] hover:bg-white",
        ghost: "text-[#5f6d68] hover:bg-[#efece4] hover:text-[#1c2730]",
        success:
          "bg-[#1f8a64] text-white shadow-[0_8px_20px_#1f8a642b] hover:bg-[#187554]",
        danger:
          "bg-[#c76b2d] text-white hover:bg-[#a85720]",
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
