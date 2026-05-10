import { cn } from "@/lib/utils";
import type { LucideIcon } from "lucide-react";

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-xl border border-dashed border-border bg-surface/40 px-6 py-16 text-center",
        className,
      )}
    >
      {Icon && (
        <div className="mb-4 grid h-10 w-10 place-items-center rounded-md border border-border bg-surface text-muted-foreground">
          <Icon className="h-4 w-4" />
        </div>
      )}
      <div className="text-[15px] font-medium tracking-tight text-foreground">{title}</div>
      {description && (
        <p className="mt-1.5 max-w-sm text-[13px] leading-relaxed text-muted-foreground">
          {description}
        </p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}
