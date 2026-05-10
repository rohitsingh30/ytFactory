import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatNumber(value: number | null | undefined, opts: Intl.NumberFormatOptions = {}): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1, ...opts }).format(value);
}

export function compactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

export function relativeTime(date: string | Date | null | undefined): string {
  if (!date) return "—";
  const d = typeof date === "string" ? new Date(date) : date;
  const diffSec = Math.round((Date.now() - d.getTime()) / 1000);
  const abs = Math.abs(diffSec);
  const fmt = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (abs < 60) return fmt.format(-diffSec, "second");
  if (abs < 3600) return fmt.format(-Math.round(diffSec / 60), "minute");
  if (abs < 86_400) return fmt.format(-Math.round(diffSec / 3600), "hour");
  if (abs < 604_800) return fmt.format(-Math.round(diffSec / 86_400), "day");
  if (abs < 2_592_000) return fmt.format(-Math.round(diffSec / 604_800), "week");
  if (abs < 31_536_000) return fmt.format(-Math.round(diffSec / 2_592_000), "month");
  return fmt.format(-Math.round(diffSec / 31_536_000), "year");
}
