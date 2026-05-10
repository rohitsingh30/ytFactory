import { cn } from "@/lib/utils";

const TONES: Record<string, string> = {
  pending: "bg-amber-500/10 text-amber-300 border-amber-500/20",
  rendering: "bg-violet-500/10 text-violet-300 border-violet-500/25",
  uploading: "bg-blue-500/10 text-blue-300 border-blue-500/25",
  done: "bg-emerald-500/10 text-emerald-300 border-emerald-500/25",
  failed: "bg-rose-500/10 text-rose-300 border-rose-500/25",
  cancelled: "bg-zinc-500/10 text-zinc-300 border-zinc-500/25",
  running: "bg-violet-500/10 text-violet-300 border-violet-500/25",
  queued: "bg-amber-500/10 text-amber-300 border-amber-500/20",
  held: "bg-rose-500/10 text-rose-300 border-rose-500/25",
};

const LABELS: Record<string, string> = {
  pending: "queued",
  rendering: "rendering",
  uploading: "uploading",
  done: "ready",
  failed: "failed",
  cancelled: "cancelled",
  running: "running",
  queued: "queued",
  held: "held",
};

interface StatusPillProps {
  status: string | null | undefined;
  pulse?: boolean;
  className?: string;
}

export function StatusPill({ status, pulse, className }: StatusPillProps) {
  const s = (status || "pending").toLowerCase();
  const tone = TONES[s] ?? "bg-surface-2 text-muted-foreground border-border";
  const label = LABELS[s] ?? s;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em]",
        tone,
        className,
      )}
    >
      {pulse ? (
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
        </span>
      ) : (
        <span className="h-1.5 w-1.5 rounded-full bg-current" />
      )}
      {label}
    </span>
  );
}
