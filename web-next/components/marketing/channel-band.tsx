"use client";

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { channelsApi } from "@/lib/api";
import type { ChannelSummary } from "@/lib/types";
import { ChannelAvatar } from "@/components/app/channel-avatar";
import { CHANNEL_TONE, fmtCount } from "@/components/app/channel-meta";
import { cn } from "@/lib/utils";

/**
 * Real social proof for the landing page — instead of a wordmark rail,
 * a horizontally-scrollable row of every live channel with its real
 * avatar + sub count + latest cover. Reads as "this is real" at a
 * glance; the wordmark version was indistinguishable from a fake demo.
 *
 * Falls back to a plain wordmark rail if /api/channels can't be reached
 * (the static landing should still render even if the control plane is
 * cold).
 */
const FALLBACK_LABELS = [
  "MyStoriesAnimated",
  "SportsRecapped",
  "HindutavaAnimated",
  "HistoryRecapped",
  "RhymeTimeJunction",
  "ScrollPulse",
  "CosmosDecoded",
];

export function ChannelBand() {
  const [channels, setChannels] = useState<ChannelSummary[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    channelsApi
      .list()
      .then((c) => setChannels(c.channels))
      .catch(() => setFailed(true));
  }, []);

  return (
    <div className="mx-auto max-w-6xl px-6">
      <motion.div
        initial={{ opacity: 0 }}
        whileInView={{ opacity: 1 }}
        viewport={{ once: true }}
        transition={{ duration: 0.4 }}
        className="text-center text-[10px] uppercase tracking-[0.22em] text-muted-foreground"
      >
        Live channels
      </motion.div>

      {failed || (channels && channels.length === 0) ? (
        <FallbackRail />
      ) : channels === null ? (
        <SkeletonRail />
      ) : (
        <div className="mt-7 grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
          {channels.map((c, i) => (
            <Link
              key={c.key}
              href={`/app/channels/${c.key}`}
              className="group relative block overflow-hidden rounded-xl border border-border bg-surface transition-colors hover:border-border-strong"
            >
              <motion.div
                initial={{ opacity: 0, y: 6 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.04, duration: 0.3 }}
                className="flex items-center gap-3 px-4 py-3"
              >
                <ChannelAvatar channel={c.key} size="md" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[12.5px] font-medium tracking-tight">
                    {c.label}
                  </div>
                  <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
                    {fmtCount(c.subscribers)} subs · {fmtCount(c.youtube_video_count)} videos
                  </div>
                </div>
                <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
              </motion.div>
              <div
                className={cn(
                  "h-0.5 w-full opacity-70",
                  (CHANNEL_TONE[c.key] ?? CHANNEL_TONE._fallback).bg,
                )}
              />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function SkeletonRail() {
  return (
    <div className="mt-7 grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
      {Array(8)
        .fill(0)
        .map((_, i) => (
          <div
            key={i}
            className="flex items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3"
          >
            <div className="h-10 w-10 animate-pulse rounded-full bg-surface-2" />
            <div className="flex-1 space-y-1.5">
              <div className="h-2.5 w-3/4 animate-pulse rounded bg-surface-2" />
              <div className="h-2 w-1/2 animate-pulse rounded bg-surface-2" />
            </div>
          </div>
        ))}
    </div>
  );
}

function FallbackRail() {
  return (
    <div className="mt-6 flex flex-wrap items-center justify-center gap-x-8 gap-y-3">
      {FALLBACK_LABELS.map((c) => (
        <span
          key={c}
          className="font-mono text-xs uppercase tracking-[0.18em] text-muted-foreground"
        >
          {c}
        </span>
      ))}
    </div>
  );
}
