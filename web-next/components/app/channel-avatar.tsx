"use client";

import { useState } from "react";
import Image from "next/image";
import { cn } from "@/lib/utils";
import { channelsApi } from "@/lib/api";
import { CHANNEL_TONE, channelMonogram } from "@/components/app/channel-meta";

interface ChannelAvatarProps {
  channel: string;
  size?: "xs" | "sm" | "md" | "lg" | "xl";
  className?: string;
  /** Force the monogram fallback even if the mirrored avatar exists. */
  forceFallback?: boolean;
}

const SIZE: Record<NonNullable<ChannelAvatarProps["size"]>, { px: number; cls: string; text: string }> = {
  xs: { px: 20, cls: "h-5 w-5",   text: "text-[8.5px]" },
  sm: { px: 28, cls: "h-7 w-7",   text: "text-[10px]" },
  md: { px: 40, cls: "h-10 w-10", text: "text-[11.5px]" },
  lg: { px: 56, cls: "h-14 w-14", text: "text-[14px]" },
  xl: { px: 96, cls: "h-24 w-24", text: "text-[22px]" },
};

/**
 * Real, round YouTube-style channel avatar. Loads the mirrored
 * `/api/channels/<key>/avatar.jpg`; if that 404s (no auth or missing
 * mirror), falls back to a monogram chip with the channel's tone.
 *
 * Uses next/image for retina + `object-cover` so any source aspect
 * works. The `onError` toggle is the cheap-and-correct fallback path —
 * <img> 404s aren't great DevTools UX but they're accurate signal,
 * better than a 1x1 placeholder.
 */
export function ChannelAvatar({ channel, size = "md", className, forceFallback }: ChannelAvatarProps) {
  const [failed, setFailed] = useState(false);
  const dim = SIZE[size];
  const tone = CHANNEL_TONE[channel] ?? CHANNEL_TONE._fallback;
  const monogram = channelMonogram(channel);

  const showFallback = forceFallback || failed;

  return (
    <div
      className={cn(
        "relative shrink-0 overflow-hidden rounded-full ring-1 ring-border",
        dim.cls,
        className,
      )}
      aria-label={`${channel} avatar`}
    >
      {!showFallback && (
        <Image
          src={channelsApi.avatarUrl(channel)}
          alt=""
          width={dim.px}
          height={dim.px}
          unoptimized
          className="h-full w-full object-cover"
          onError={() => setFailed(true)}
        />
      )}
      {showFallback && (
        <div
          className={cn(
            "grid h-full w-full place-items-center font-mono font-semibold tracking-tighter",
            dim.text,
            tone.bg,
            tone.text,
          )}
        >
          {monogram}
        </div>
      )}
    </div>
  );
}
