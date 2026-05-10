"use client";

import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

interface PreviewableTileProps {
  selected?: boolean;
  onSelect?: () => void;
  /** The tile's main label (e.g., voice name, music name). */
  title: string;
  /** Optional secondary line (style, language, channel). */
  subtitle?: string | null;
  /** Optional pill chip (e.g., "EN", "Hi-EN"). */
  badge?: string;
  /** Slot for an audio/video play button — sits on the left edge. */
  preview?: React.ReactNode;
  /** Tiny meta line above the title (e.g., "voice"). */
  eyebrow?: string;
  className?: string;
}

/**
 * Generic "audition before commit" tile — the customize step uses this
 * for voices, music beds, and sample renders. Keeps every catalogue
 * surface visually consistent: round preview affordance left, label
 * stack center, selected check right.
 */
export function PreviewableTile({
  selected,
  onSelect,
  title,
  subtitle,
  badge,
  preview,
  eyebrow,
  className,
}: PreviewableTileProps) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "group flex w-full items-center gap-3 rounded-lg border bg-surface-2/60 p-3 text-left transition-colors",
        selected
          ? "border-foreground/45 ring-1 ring-foreground/30"
          : "border-border hover:border-border-strong",
        className,
      )}
    >
      {preview && <div className="shrink-0">{preview}</div>}
      <div className="min-w-0 flex-1">
        {eyebrow && (
          <div className="font-mono text-[9.5px] uppercase tracking-[0.18em] text-muted-foreground/80">
            {eyebrow}
          </div>
        )}
        <div className="flex items-center gap-2">
          <span className="truncate text-[12.5px] font-medium tracking-tight">{title}</span>
          {badge && (
            <span className="rounded-md border border-border bg-surface px-1.5 py-px font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground">
              {badge}
            </span>
          )}
        </div>
        {subtitle && (
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{subtitle}</div>
        )}
      </div>
      {selected && <Check className="h-3.5 w-3.5 shrink-0 text-emerald-300" />}
    </button>
  );
}
