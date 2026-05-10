"use client";

import Link from "next/link";
import Image from "next/image";
import { motion } from "framer-motion";
import { ArrowUpRight, Check } from "lucide-react";
import type { ChannelSummary } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ChannelAvatar } from "@/components/app/channel-avatar";
import { ChannelBanner } from "@/components/app/channel-banner";
import { ChannelStatStrip } from "@/components/app/channel-stat-strip";
import { CHANNEL_TONE } from "@/components/app/channel-meta";

interface Props {
  channel: ChannelSummary;
  /** When omitted, the card behaves as a button via onSelect. */
  href?: string;
  /** Click handler when used as a non-link selectable card. */
  onSelect?: () => void;
  selected?: boolean;
  index?: number;
  /** Compact mode — omits banner + recent reel; for dense rows. */
  compact?: boolean;
}

/**
 * The canonical channel card — banner + round avatar over it (YouTube
 * pattern), stats, tagline, latest-3 reel, provider chips. Used on the
 * dashboard, channels gallery, create wizard, landing page.
 *
 * Renders as a Link when `href` is set, a button when `onSelect` is.
 * `compact` collapses to a single-line dense row (used in the create
 * step's "selected channel" readout).
 */
export function ChannelHeroCard({ channel, href, onSelect, selected, index = 0, compact }: Props) {
  const tone = CHANNEL_TONE[channel.key] ?? CHANNEL_TONE._fallback;

  const inner = (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.035, 0.35), duration: 0.28, ease: "easeOut" }}
      className={cn(
        "group relative h-full overflow-hidden rounded-xl border-2 bg-surface text-left transition-all",
        "hover:border-border-strong hover:bg-surface-2",
        selected
          ? "border-emerald-400 bg-emerald-400/[0.04] shadow-[0_0_0_4px_rgba(52,211,153,0.18),0_8px_24px_-6px_rgba(52,211,153,0.35)] -translate-y-0.5"
          : "border-border",
      )}
    >
      {selected && (
        <div className="absolute right-3 top-3 z-10 flex h-7 w-7 items-center justify-center rounded-full bg-emerald-400 text-background shadow-lg">
          <Check className="h-4 w-4" strokeWidth={3} />
        </div>
      )}
      {!compact && (
        <div className="relative">
          <ChannelBanner channel={channel.key} height="compact" />
          {/* Avatar punches up out of the banner like a real YT page. */}
          <div className="absolute -bottom-6 left-5">
            <ChannelAvatar channel={channel.key} size="lg" className="ring-4 ring-surface" />
          </div>
        </div>
      )}

      <div className={cn("flex flex-col gap-3 p-5", !compact && "pt-9")}>
        <div className="flex items-start justify-between gap-3">
          {compact && <ChannelAvatar channel={channel.key} size="md" />}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="truncate text-[14.5px] font-medium tracking-tight">{channel.label}</span>
              {channel.youtube_url && (
                <a
                  href={channel.youtube_url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label="Open on YouTube"
                  title="Open on YouTube"
                >
                  <ArrowUpRight className="h-3.5 w-3.5" />
                </a>
              )}
            </div>
            {channel.custom_url && (
              <div className="mt-0.5 font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
                {channel.custom_url}
              </div>
            )}
            <p className="mt-2 line-clamp-2 text-[12.5px] leading-relaxed text-muted-foreground">
              {channel.tagline}
            </p>
          </div>
        </div>

        <ChannelStatStrip
          subscribers={channel.subscribers}
          videoCount={channel.youtube_video_count}
          totalViews={channel.total_views}
          size="sm"
        />

        {!compact && channel.recent_videos && channel.recent_videos.length > 0 && (
          <div className="grid grid-cols-3 gap-1.5">
            {channel.recent_videos.slice(0, 3).map((v) => (
              <div
                key={v.video_id}
                className={cn(
                  "relative aspect-[9/16] overflow-hidden rounded-md border border-border bg-background",
                )}
              >
                {v.thumbnail ? (
                  <Image
                    src={v.thumbnail}
                    alt=""
                    fill
                    unoptimized
                    sizes="120px"
                    className="object-cover transition-transform duration-300 group-hover:scale-[1.04]"
                  />
                ) : (
                  <div className={cn("h-full w-full", tone.banner)} />
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </motion.div>
  );

  if (href) {
    return (
      <Link href={href} className="block h-full">
        {inner}
      </Link>
    );
  }
  return (
    <button type="button" onClick={onSelect} className="block h-full w-full text-left">
      {inner}
    </button>
  );
}
