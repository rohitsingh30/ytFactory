"use client";

import { useState } from "react";
import Image from "next/image";
import { cn } from "@/lib/utils";
import { channelsApi } from "@/lib/api";
import { CHANNEL_TONE } from "@/components/app/channel-meta";

interface ChannelBannerProps {
  channel: string;
  className?: string;
  /** When true, render as a soft 16:5 wide strip; when false, full bleed. */
  height?: "compact" | "tall";
  /** Add a top→bottom dark veil so overlaid text reads. Default true. */
  veil?: boolean;
}

/**
 * Wide channel banner — uses the mirrored YT banner when present, else
 * a per-channel gradient (CHANNEL_TONE.banner). Always self-contained:
 * caller doesn't need to know whether the banner exists.
 *
 * Default ratio is 16:5 — close to YouTube's banner crop on desktop.
 * `height="tall"` swaps to 16:7 for the channel-detail hero.
 */
export function ChannelBanner({ channel, className, height = "compact", veil = true }: ChannelBannerProps) {
  const [failed, setFailed] = useState(false);
  const tone = CHANNEL_TONE[channel] ?? CHANNEL_TONE._fallback;
  const aspect = height === "tall" ? "aspect-[16/7]" : "aspect-[16/5]";

  return (
    <div
      className={cn(
        "relative w-full overflow-hidden",
        aspect,
        // Always paint the gradient — image (when present) sits on top.
        // Avoids a flash of empty bg before the image loads.
        tone.banner,
        className,
      )}
      aria-hidden
    >
      {!failed && (
        <Image
          src={channelsApi.bannerUrl(channel)}
          alt=""
          fill
          unoptimized
          sizes="(max-width: 768px) 100vw, 1200px"
          className="object-cover"
          onError={() => setFailed(true)}
        />
      )}
      {veil && (
        <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-background via-background/40 to-transparent" />
      )}
    </div>
  );
}
