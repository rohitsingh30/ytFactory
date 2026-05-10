"use client";

import { useEffect, useRef, useState } from "react";
import { ExternalLink, Play, Sparkles } from "lucide-react";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { channelsApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ChannelAvatar } from "@/components/app/channel-avatar";
import { channelLabel, fmtCount } from "@/components/app/channel-meta";

interface InspirationDrawerProps {
  channel: string | null;
  /** Optional custom trigger; defaults to the gold "Hear the vibe" button. */
  trigger?: React.ReactNode;
}

interface InspirationVideo {
  video_id: string;
  title: string;
  thumbnail: string | null;
  views: number | null;
  watch_url: string;
}

/**
 * Slide-out drawer showing the picked channel's most-recent 3 shorts as
 * inline-playable iframes. Lets the user audition the channel's actual
 * voice/visual identity before tweaking knobs on the customize step.
 *
 * Lazy-fetches once per channel; if the user reopens for the same
 * channel we keep the cached result.
 */
export function InspirationDrawer({ channel, trigger }: InspirationDrawerProps) {
  const [open, setOpen] = useState(false);
  const [videos, setVideos] = useState<InspirationVideo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  const fetchedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!open || !channel) return;
    if (fetchedFor.current === channel) return;
    setVideos(null);
    setError(null);
    setActiveIdx(null);
    fetchedFor.current = channel;
    channelsApi
      .inspiration(channel, 3)
      .then((res) => setVideos(res.videos as InspirationVideo[]))
      .catch((e: Error) => {
        setError(e.message);
        setVideos([]);
      });
  }, [open, channel]);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        {trigger ?? (
          <Button variant="outline" size="sm" disabled={!channel}>
            <Sparkles className="h-3.5 w-3.5" />
            Hear the vibe
          </Button>
        )}
      </SheetTrigger>
      <SheetContent side="right" className="w-full max-w-md sm:max-w-lg">
        <SheetHeader className="space-y-3">
          <div className="flex items-center gap-3">
            {channel && <ChannelAvatar channel={channel} size="md" />}
            <div className="min-w-0">
              <SheetTitle className="truncate text-[15px] font-medium tracking-tight">
                {channel ? channelLabel(channel) : "Pick a channel first"}
              </SheetTitle>
              <SheetDescription className="text-[11.5px] text-muted-foreground">
                Recent shorts on this channel — play to feel the vibe before tweaking knobs.
              </SheetDescription>
            </div>
          </div>
        </SheetHeader>

        <div className="mt-6 space-y-3">
          {!channel && (
            <p className="text-[12.5px] text-muted-foreground">
              No channel picked yet — close this and pick one from Step 1.
            </p>
          )}
          {channel && videos === null && (
            <div className="grid gap-3">
              {[0, 1, 2].map((i) => (
                <div key={i} className="aspect-[9/16] animate-pulse rounded-lg bg-surface-2" />
              ))}
            </div>
          )}
          {channel && videos !== null && videos.length === 0 && (
            <p className="rounded-md border border-dashed border-border bg-surface-2 p-4 text-center text-[12px] text-muted-foreground">
              {error
                ? `Couldn't load: ${error}`
                : "No recent shorts on this channel yet — yours will be the first."}
            </p>
          )}
          {channel && videos && videos.length > 0 && (
            <ul className="space-y-3">
              {videos.map((v, i) => (
                <li key={v.video_id} className="overflow-hidden rounded-lg border border-border bg-surface-2">
                  {activeIdx === i ? (
                    <div className="aspect-[9/16] w-full bg-black">
                      <iframe
                        title={v.title}
                        src={`https://www.youtube.com/embed/${v.video_id}?autoplay=1&playsinline=1`}
                        className="h-full w-full"
                        allow="autoplay; encrypted-media; picture-in-picture; web-share"
                        allowFullScreen
                      />
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setActiveIdx(i)}
                      className="group relative block aspect-[9/16] w-full overflow-hidden bg-background"
                      aria-label={`Play ${v.title}`}
                    >
                      {v.thumbnail && (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={v.thumbnail}
                          alt=""
                          className="absolute inset-0 h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.03]"
                        />
                      )}
                      <span className={cn(
                        "absolute inset-0 grid place-items-center bg-gradient-to-t from-black/70 via-black/10 to-transparent",
                      )}>
                        <span className="grid h-12 w-12 place-items-center rounded-full bg-foreground/95 text-background shadow-lg">
                          <Play className="ml-0.5 h-5 w-5" />
                        </span>
                      </span>
                    </button>
                  )}
                  <div className="flex items-start justify-between gap-2 p-3">
                    <div className="min-w-0">
                      <div className="line-clamp-2 text-[12.5px] font-medium leading-snug tracking-tight">
                        {v.title}
                      </div>
                      <div className="mt-1 font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
                        {fmtCount(v.views)} views
                      </div>
                    </div>
                    <a
                      href={v.watch_url}
                      target="_blank"
                      rel="noreferrer"
                      className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
                      aria-label="Open on YouTube"
                    >
                      <ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="mt-6 flex justify-end">
          <SheetClose asChild>
            <Button variant="ghost" size="sm">Close</Button>
          </SheetClose>
        </div>
      </SheetContent>
    </Sheet>
  );
}
