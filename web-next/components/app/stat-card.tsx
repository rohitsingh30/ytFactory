import { cn, compactNumber } from "@/lib/utils";
import type { LucideIcon } from "lucide-react";

interface StatCardProps {
  label: string;
  value: number | string | null | undefined;
  hint?: string;
  delta?: { value: number; positive?: boolean } | null;
  icon?: LucideIcon;
  className?: string;
}

export function StatCard({ label, value, hint, delta, icon: Icon, className }: StatCardProps) {
  const display =
    value === null || value === undefined
      ? "—"
      : typeof value === "number"
        ? compactNumber(value)
        : value;

  return (
    <div
      className={cn(
        "group relative overflow-hidden rounded-xl border border-border bg-surface p-5 transition-colors hover:border-border-strong",
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          {label}
        </div>
        {Icon && <Icon className="h-3.5 w-3.5 text-muted-foreground" />}
      </div>
      <div className="mt-3 flex items-baseline gap-2">
        <div className="num text-[28px] font-medium leading-none tracking-tight text-foreground md:text-[32px]">
          {display}
        </div>
        {delta && (
          <span
            className={cn(
              "font-mono text-[11px] tabular-nums",
              delta.positive ? "text-emerald-300" : "text-muted-foreground",
            )}
          >
            {delta.positive ? "+" : ""}
            {delta.value}
          </span>
        )}
      </div>
      {hint && (
        <div className="mt-1.5 text-[11px] text-muted-foreground/80">{hint}</div>
      )}
    </div>
  );
}
