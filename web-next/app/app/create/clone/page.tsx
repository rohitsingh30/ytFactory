"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, ArrowRight, Check, Loader2, Sparkles, Video, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/app/page-header";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface CloneState {
  request_id: string;
  state: string; // queued | downloading | extracting | transcribing | analyzing | done | failed
  error?: string | null;
  duration_s?: number | null;
  frames_count?: number;
  has_transcript?: boolean;
  fingerprint?: Record<string, unknown> | null;
  log_tail?: string;
}

const STAGES: { id: string; label: string }[] = [
  { id: "queued", label: "Queued" },
  { id: "downloading", label: "Downloading" },
  { id: "extracting", label: "Frames + audio" },
  { id: "transcribing", label: "Transcribe" },
  { id: "analyzing", label: "Analyse" },
  { id: "done", label: "Done" },
];

/**
 * Clone-a-video page. Posts the URL to /api/clone_video, polls the
 * request, and renders the resulting niche fingerprint inline.
 *
 * Backed by ``control/clone_video_routes.py`` — yt-dlp downloads the
 * video, ffmpeg pulls keyframes + audio, Azure Whisper transcribes
 * (when configured), and Azure GPT (vision) returns a structured niche
 * fingerprint the user can review.
 */
export default function CloneVideoPage() {
  const [url, setUrl] = useState("");
  const [notes, setNotes] = useState("");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [status, setStatus] = useState<CloneState | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Poll until the request is in a terminal state.
  useEffect(() => {
    if (!requestId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const r = await api.get<CloneState>(`/api/clone_video/${requestId}`);
        if (cancelled) return;
        setStatus(r);
        if (r.state !== "done" && r.state !== "failed") {
          timer = setTimeout(tick, 2500);
        }
      } catch (e) {
        if (cancelled) return;
        toast.error("Polling failed", { description: e instanceof Error ? e.message : String(e) });
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [requestId]);

  async function submit() {
    if (!url.trim()) {
      toast.error("Paste a video URL first");
      return;
    }
    setSubmitting(true);
    setStatus(null);
    setRequestId(null);
    try {
      const r = await api.post<{ request_id: string }>("/api/clone_video", {
        url: url.trim(),
        notes: notes.trim(),
      });
      setRequestId(r.request_id);
      toast.success("Analysis started", { description: `Request ${r.request_id.slice(0, 8)}…` });
    } catch (e) {
      toast.error("Couldn't queue analysis", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setSubmitting(false);
    }
  }

  function reset() {
    setRequestId(null);
    setStatus(null);
  }

  const isRunning = status && status.state !== "done" && status.state !== "failed";
  const isDone = status?.state === "done";
  const isFailed = status?.state === "failed";

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        title="Clone a video"
        description="Paste a video URL — we deep-analyse the format (hook, pacing, captions, visuals, voice profile) and turn it into a fresh niche your channels can produce."
        actions={
          <Button asChild variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
            <Link href="/app/create">
              <ArrowLeft className="h-3.5 w-3.5" />
              Back
            </Link>
          </Button>
        }
      />

      <div className="mx-auto w-full max-w-3xl flex-1 px-6 py-10 md:px-8 space-y-5">
        {!requestId && (
          <div className="space-y-5 rounded-xl border border-border bg-surface p-6">
            <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
              <Sparkles className="h-3.5 w-3.5 text-amber-300" />
              Powered by yt-dlp + Azure GPT-vision · keyframes + transcript → niche spec
            </div>

            <div className="space-y-2">
              <Label htmlFor="clone-url" className="text-[12px] font-medium">Video URL</Label>
              <Input
                id="clone-url"
                type="url"
                placeholder="https://www.youtube.com/shorts/… · TikTok · Reels · X · raw mp4"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                autoFocus
              />
              <p className="text-[11px] text-muted-foreground">
                YouTube Shorts, TikTok, Instagram Reels, X clips, or a raw mp4 URL.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="clone-notes" className="text-[12px] font-medium">Notes (optional)</Label>
              <Textarea
                id="clone-notes"
                rows={3}
                placeholder="What about this video should we replicate? e.g. 'mostly the captioning style and hook' / 'the whole format'"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
              <Button asChild variant="ghost" size="sm">
                <Link href="/app/create">Cancel</Link>
              </Button>
              <Button onClick={submit} disabled={submitting || !url.trim()}>
                {submitting ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" /> Queuing…
                  </>
                ) : (
                  <>
                    <Video className="h-3.5 w-3.5" />
                    Analyse video
                    <ArrowRight className="h-3.5 w-3.5" />
                  </>
                )}
              </Button>
            </div>
          </div>
        )}

        {requestId && status && (
          <div className="space-y-5">
            <div className="rounded-xl border border-border bg-surface p-5">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                    Request {requestId}
                  </div>
                  <div className="mt-0.5 text-[13px] font-medium tracking-tight truncate">
                    {url}
                  </div>
                </div>
                {(isDone || isFailed) && (
                  <Button size="sm" variant="outline" onClick={reset}>
                    <X className="h-3.5 w-3.5" /> New
                  </Button>
                )}
              </div>

              {/* Stage progress */}
              <div className="mt-4 flex items-center gap-2 overflow-x-auto pb-1">
                {STAGES.map((s) => {
                  const idx = STAGES.findIndex((x) => x.id === s.id);
                  const cur = STAGES.findIndex((x) => x.id === status.state);
                  const isPast = cur > idx || isDone;
                  const isActive = !isFailed && cur === idx && !isDone;
                  return (
                    <div
                      key={s.id}
                      className={cn(
                        "flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px]",
                        isPast
                          ? "border-emerald-400/40 bg-emerald-400/[0.06] text-emerald-200"
                          : isActive
                          ? "border-sky-400/40 bg-sky-400/[0.08] text-sky-200"
                          : "border-border text-muted-foreground/70",
                      )}
                    >
                      {isPast ? <Check className="h-3 w-3" /> : isActive ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                      {s.label}
                    </div>
                  );
                })}
              </div>

              {status.duration_s != null && status.duration_s > 0 && (
                <div className="mt-3 grid grid-cols-3 gap-3 text-[11px] text-muted-foreground">
                  <Stat label="Runtime" value={`${status.duration_s.toFixed(1)}s`} />
                  <Stat label="Frames" value={String(status.frames_count ?? 0)} />
                  <Stat label="Transcript" value={status.has_transcript ? "yes" : "no"} />
                </div>
              )}
            </div>

            {isFailed && (
              <div className="rounded-xl border border-rose-400/40 bg-rose-400/[0.04] p-5">
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-rose-300">
                  Failed
                </div>
                <p className="mt-2 text-[12.5px] text-rose-200/90">{status.error || "(no error message)"}</p>
                {status.log_tail && (
                  <pre className="mt-3 max-h-48 overflow-auto rounded-md bg-background/50 p-3 text-[10.5px] font-mono text-muted-foreground">
                    {status.log_tail}
                  </pre>
                )}
              </div>
            )}

            {isRunning && (
              <div className="rounded-xl border border-border bg-surface p-5">
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  Live log
                </div>
                <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-background/50 p-3 text-[10.5px] font-mono text-muted-foreground whitespace-pre-wrap">
                  {status.log_tail || "(no output yet)"}
                </pre>
              </div>
            )}

            {isDone && status.fingerprint && (
              <div className="rounded-xl border border-emerald-400/30 bg-emerald-400/[0.03] p-5">
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-emerald-300">
                  Niche fingerprint
                </div>
                <div className="mt-2 text-[13px] font-medium tracking-tight">
                  {String((status.fingerprint as Record<string, unknown>).label ?? "(no label)")}
                </div>
                {Boolean((status.fingerprint as Record<string, unknown>).description) && (
                  <p className="mt-1 text-[12.5px] text-muted-foreground">
                    {String((status.fingerprint as Record<string, unknown>).description)}
                  </p>
                )}
                <pre className="mt-4 max-h-96 overflow-auto rounded-md bg-background/50 p-3 text-[11px] font-mono text-foreground/85">
                  {JSON.stringify(status.fingerprint, null, 2)}
                </pre>
                <div className="mt-4 flex items-center justify-end gap-2">
                  <Button asChild variant="outline" size="sm">
                    <Link href="/app/channels">Pick a channel to add it to</Link>
                  </Button>
                </div>
              </div>
            )}

            {/* Frames thumbnails */}
            {status.frames_count != null && status.frames_count > 0 && (
              <div className="rounded-xl border border-border bg-surface p-5">
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  Sampled frames
                </div>
                <div className="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-6">
                  {Array.from({ length: status.frames_count }).map((_, i) => (
                    /* eslint-disable-next-line @next/next/no-img-element */
                    <img
                      key={i}
                      src={`/api/clone_video/${requestId}/frames/${i + 1}.jpg`}
                      alt={`frame ${i + 1}`}
                      className="aspect-[9/16] w-full rounded-md border border-border object-cover bg-background"
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
      <div className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-0.5 font-mono text-[12px] text-foreground tabular-nums">{value}</div>
    </div>
  );
}

