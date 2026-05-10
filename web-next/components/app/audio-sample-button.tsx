"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { cn } from "@/lib/utils";
import { registerStopper, stopAllOthers } from "@/components/app/audio-bus";

interface AudioSampleButtonProps {
  src: string | null | undefined;
  /** Optional cap on playback time (sec). Default 8s — long enough to read a vibe, short enough to stay polite. */
  maxSeconds?: number;
  size?: "sm" | "md" | "lg";
  className?: string;
  /** Aria label / title — defaults to "Play sample". */
  label?: string;
}

const DIM = {
  sm: "h-7 w-7",
  md: "h-9 w-9",
  lg: "h-11 w-11",
};
const ICON = {
  sm: "h-3 w-3",
  md: "h-3.5 w-3.5",
  lg: "h-4 w-4",
};

/**
 * Round play/pause button with auto-mutex via the shared audio bus —
 * starting one button immediately stops every other AudioSampleButton
 * on the page (so the user never has to chase a hidden instance).
 *
 * Lazily creates the underlying <Audio> on first click, so a grid of
 * 30 buttons doesn't spawn 30 audio sockets up-front.
 */
export function AudioSampleButton({ src, maxSeconds = 8, size = "md", className, label = "Play sample" }: AudioSampleButtonProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const capTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [playing, setPlaying] = useState(false);

  const stop = useCallback(() => {
    if (capTimerRef.current) {
      clearTimeout(capTimerRef.current);
      capTimerRef.current = null;
    }
    if (audioRef.current && !audioRef.current.paused) {
      audioRef.current.pause();
    }
    setPlaying(false);
  }, []);

  // Register/unregister with the bus so siblings can auto-pause us.
  useEffect(() => {
    return registerStopper(stop);
  }, [stop]);

  // Stop on unmount + cleanup the audio element.
  useEffect(() => {
    return () => {
      stop();
      audioRef.current = null;
    };
  }, [stop]);

  const toggle = useCallback(
    (e: React.MouseEvent | React.KeyboardEvent) => {
      e.stopPropagation();
      e.preventDefault();
      if (!src) return;

      if (playing) {
        stop();
        return;
      }

      stopAllOthers(stop);

      if (!audioRef.current) {
        const el = new Audio(src);
        el.onended = () => {
          setPlaying(false);
          if (capTimerRef.current) {
            clearTimeout(capTimerRef.current);
            capTimerRef.current = null;
          }
        };
        el.onpause = () => setPlaying(false);
        audioRef.current = el;
      } else if (audioRef.current.src !== src) {
        // src changed (e.g. parent swapped between voice samples)
        audioRef.current.src = src;
      }

      audioRef.current.currentTime = 0;
      audioRef.current
        .play()
        .then(() => {
          setPlaying(true);
          if (capTimerRef.current) clearTimeout(capTimerRef.current);
          capTimerRef.current = setTimeout(stop, Math.max(1, maxSeconds) * 1000);
        })
        .catch(() => {
          // autoplay may be blocked; nothing to do — UI just shows it didn't start.
          setPlaying(false);
        });
    },
    [maxSeconds, playing, src, stop],
  );

  const disabled = !src;
  const dim = DIM[size];
  const icon = ICON[size];

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={disabled}
      aria-label={playing ? "Pause sample" : label}
      title={disabled ? "No sample available" : label}
      className={cn(
        "group relative grid shrink-0 place-items-center rounded-full border transition-colors",
        dim,
        disabled
          ? "cursor-not-allowed border-border bg-surface-2 text-muted-foreground/40"
          : playing
            ? "border-foreground bg-foreground text-background"
            : "border-border-strong bg-background text-foreground hover:bg-foreground hover:text-background",
        className,
      )}
    >
      {playing ? (
        <Pause className={icon} />
      ) : (
        // Optical centering — the Play triangle has more weight on the left.
        <Play className={cn(icon, "ml-0.5")} />
      )}
      {playing && (
        <span
          className="pointer-events-none absolute inset-0 rounded-full ring-2 ring-foreground/30 motion-safe:animate-ping"
          aria-hidden
        />
      )}
    </button>
  );
}
