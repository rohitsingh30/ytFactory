"use client";

import { Eye, ThumbsUp, Users, Video } from "lucide-react";
import { cn } from "@/lib/utils";
import { fmtCount } from "@/components/app/channel-meta";

interface ChannelStatStripProps {
  subscribers?: number | null;
  videoCount?: number | null;
  totalViews?: number | null;
  totalLikes?: number | null;
  className?: string;
  size?: "sm" | "md";
  /** Render as a compact one-line strip vs an inline grid of pills. */
  layout?: "inline" | "grid";
}

interface Stat {
  Icon: React.ComponentType<{ className?: string }>;
  value: string;
  label: string;
  show: boolean;
}

/**
 * Subs · videos · views — reads as a real channel header.
 * Hides cells when the underlying stat is null (avoids a forest of "—").
 */
export function ChannelStatStrip({
  subscribers,
  videoCount,
  totalViews,
  totalLikes,
  className,
  size = "md",
  layout = "inline",
}: ChannelStatStripProps) {
  const stats: Stat[] = [
    { Icon: Users, value: fmtCount(subscribers), label: "subs", show: subscribers != null },
    { Icon: Video, value: fmtCount(videoCount), label: "videos", show: videoCount != null },
    { Icon: Eye, value: fmtCount(totalViews), label: "views", show: totalViews != null },
    { Icon: ThumbsUp, value: fmtCount(totalLikes), label: "likes", show: totalLikes != null },
  ].filter((s) => s.show);

  if (stats.length === 0) {
    return (
      <span className={cn("font-mono text-[10.5px] uppercase tracking-[0.18em] text-muted-foreground/70", className)}>
        no stats yet
      </span>
    );
  }

  const numCls = size === "sm" ? "text-[12px]" : "text-[13.5px]";
  const labelCls = size === "sm" ? "text-[9.5px]" : "text-[10.5px]";

  if (layout === "grid") {
    return (
      <dl className={cn("grid grid-cols-3 gap-3", className)}>
        {stats.map((s) => (
          <div key={s.label} className="rounded-md border border-border bg-surface-2/60 px-3 py-2">
            <dd className={cn("num font-medium tracking-tight text-foreground", numCls)}>{s.value}</dd>
            <dt className={cn("mt-0.5 font-mono uppercase tracking-[0.16em] text-muted-foreground", labelCls)}>{s.label}</dt>
          </div>
        ))}
      </dl>
    );
  }

  return (
    <ul className={cn("flex flex-wrap items-center gap-x-3 gap-y-1.5 text-foreground/90", className)}>
      {stats.map((s, i) => (
        <li key={s.label} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-muted-foreground/40" aria-hidden>·</span>}
          <s.Icon className={cn("text-muted-foreground/70", size === "sm" ? "h-3 w-3" : "h-3.5 w-3.5")} />
          <span className={cn("num font-medium tabular-nums", numCls)}>{s.value}</span>
          <span className={cn("font-mono uppercase tracking-[0.16em] text-muted-foreground", labelCls)}>{s.label}</span>
        </li>
      ))}
    </ul>
  );
}
