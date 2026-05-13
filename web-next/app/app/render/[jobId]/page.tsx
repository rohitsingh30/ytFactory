"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  Loader2,
  Play,
  Send,
  Timer,
  Wand2,
  X,
} from "lucide-react";
import { motion } from "framer-motion";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { PageHeader } from "@/components/app/page-header";
import { ChannelIcon, channelLabel } from "@/components/app/channel-icon";
import { StatusPill } from "@/components/app/status-pill";
import { CritiqueChatPanel } from "@/components/app/critique-chat-panel";
import { jobsApi, pollJob } from "@/lib/api";
import { derivePreviewDisplay } from "@/lib/render-display";
import type { ArtifactEntry, Job, TimelineEntry } from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";

const STAGE_ORDER = ["rewrite", "cast", "images", "tts", "asr", "compose", "upload"];
const STAGE_LABELS: Record<string, string> = {
  rewrite: "Rewriting script",
  cast: "Casting voice & visuals",
  images: "Generating images",
  tts: "Synthesizing narration",
  asr: "Aligning captions",
  compose: "Composing video",
  upload: "Uploading to GCS",
};

export default function RenderDetailPage() {
  const params = useParams<{ jobId: string }>();
  const jobId = params.jobId;
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    const stop = pollJob(
      jobId,
      (j) => {
        setJob(j);
        setError(null);
      },
      {
        intervalMs: 800,
        stopWhen: (j) => j.status === "done" || j.status === "failed" || j.status === "cancelled",
      },
    );
    return stop;
  }, [jobId]);

  useEffect(() => {
    // initial load — surface 404 sooner if the job vanished
    if (!jobId) return;
    jobsApi.get(jobId).catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [jobId]);

  // Preview URL comes from the backend ONLY when the mp4 is actually
  // servable (preview_local_path or short_uri is set). Pre-2026-05-12
  // this fell back to constructing the URL ourselves, which mounted
  // the <video> element and triggered a 404 GET the moment status
  // flipped to "rendering" — long before any mp4 existed.
  const previewSrc = job?.preview_url ?? null;

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow={`Render · ${jobId.slice(0, 8)}…`}
        title={job?.topic ?? (error ? "Job not found" : "Loading render…")}
        description={
          job
            ? `${channelLabel(job.channel)} · ${job.proposal && (job.proposal as Record<string, unknown>).format ? String((job.proposal as Record<string, unknown>).format) : "—"}`
            : undefined
        }
        actions={
          <>
            <Button asChild variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
              <Link href="/app">
                <ArrowLeft className="h-3.5 w-3.5" />
                Dashboard
              </Link>
            </Button>
            {job && job.status === "done" && (
              <PublishDialog job={job} onPublished={(j) => setJob(j)} />
            )}
            {job && (job.status === "pending" || job.status === "rendering") && (
              <Button
                variant="destructive"
                size="sm"
                onClick={async () => {
                  try {
                    await jobsApi.cancel(jobId);
                    toast.success("Render cancelled");
                  } catch (e) {
                    toast.error("Cancel failed", { description: e instanceof Error ? e.message : String(e) });
                  }
                }}
              >
                <X className="h-3.5 w-3.5" />
                Cancel
              </Button>
            )}
          </>
        }
      />

      {error && !job && (
        <div className="mx-auto mt-12 max-w-md rounded-xl border border-border bg-surface p-6 text-center">
          <div className="text-[13px] font-medium tracking-tight text-foreground">Couldn&apos;t load this render.</div>
          <p className="mt-1 text-[12px] text-muted-foreground">{error}</p>
          <Button asChild size="sm" className="mt-4">
            <Link href="/app">Back to dashboard</Link>
          </Button>
        </div>
      )}

      {!error && (
        <div className="grid flex-1 gap-6 px-6 py-6 md:px-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)_minmax(380px,440px)]">
          {/* Left — player + meta */}
          <div className="flex flex-col gap-5">
            <PlayerCard job={job} src={previewSrc} />
            <PublishedCard job={job} />
            <MetaCard job={job} />
          </div>

          {/* Middle — timeline + live artifact previews */}
          <div className="flex flex-col gap-5">
            <StageTimeline job={job} />
            {/* LiveArtifactsCard removed 2026-05-13 — the user found
                the "Waiting for the first artifact (script lands ~5 s
                into the render)" placeholder misleading on long-form
                (script doesn't land for ~3 min) and footage_only (no
                script artifact at all). Per-stage progress lives in
                the StageTimeline above; live downloadable artifacts
                are surfaced via the GCS artifact links on the JobView. */}
          </div>

          {/* Right rail — pipeline-fix chat */}
          <div className="flex flex-col gap-5 lg:sticky lg:top-6 lg:self-start lg:max-h-[calc(100vh-3rem)]">
            {job && <CritiqueChatPanel jobId={jobId} channel={job.channel as string | undefined} />}
          </div>
        </div>
      )}
    </div>
  );
}

function PlayerCard({ job, src }: { job: Job | null; src: string | null }) {
  // Show the player ONLY when the backend says the preview is servable
  // (src derives from job.preview_url, which the backend now emits
  // only when preview_local_path or short_uri is set — see
  // control/routes/render_routes.py:_doc_to_view). Pre-2026-05-12 this
  // also flipped to true on status=="rendering", which mounted a
  // <video> element pointing at a not-yet-existent mp4 and produced
  // a noisy 404 in the user's DevTools.
  const ready = !!src;
  const done = job?.status === "done";

  // Pure aspect/kind derivation lives in lib/render-display.ts so it
  // can be unit-tested without React testing infra (pinned by
  // tests/render-display.test.mjs). Pre-2026-05-12 this card was
  // hard-coded to "9:16 · auto-loop" with `aspect-[9/16]`, so picking
  // "Long form" in the wizard rendered a 16:9 mp4 squashed into a
  // portrait letterbox.
  const { headerLabel, frameClass } = derivePreviewDisplay(
    (job?.render_spec ?? null) as Record<string, unknown> | null,
    (job?.proposal ?? null) as Record<string, unknown> | null,
  );

  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Preview
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">{headerLabel}</div>
        </div>
        {job && <StatusPill status={job.status} pulse={job.status === "rendering" || job.status === "uploading"} />}
      </div>
      <div className="mt-4 flex justify-center">
        <div className={cn("relative overflow-hidden rounded-lg border border-border bg-background", frameClass)}>
          {ready && src ? (
            <video
              key={src}
              src={src}
              controls
              autoPlay
              loop
              // muted={!ready} — when the video lands (status flips to
              // ready), unmute so the user actually hears their render.
              // Pre-2026-05-13 this was a hard-coded `muted` so autoPlay
              // would always succeed silently. The trade-off: browsers
              // may pause autoPlay on a sound-on element and surface
              // the play button instead, which is the right UX for a
              // FINISHED video (the user wants to hear it). For an
              // intermediate `rendering` state we keep it muted so the
              // pre-roll placeholder doesn't blast silence into a
              // muted-by-policy autoplay attempt.
              muted={!ready}
              playsInline
              className="h-full w-full bg-black"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center bg-surface-2">
              {!job ? (
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              ) : job.status === "failed" || job.status === "cancelled" ? (
                // Surface the error text inline so the user knows WHY
                // a render failed without having to crack open Cloud
                // Logging. The backend writes it via _update_job(...,
                // error=...) in cloud/render-worker-v2/entrypoint.py;
                // _doc_to_view forwards it as job.error.
                <div className="max-h-full overflow-auto px-4 py-3 text-left text-[11px] text-muted-foreground">
                  <div className="mb-1 flex items-center gap-1.5 text-rose-300">
                    <X className="h-3.5 w-3.5" />
                    <span className="font-medium">{job.status === "failed" ? "Render failed" : "Cancelled"}</span>
                  </div>
                  {job.error ? (
                    <pre className="whitespace-pre-wrap break-words font-mono text-[10px] leading-snug">
                      {job.error}
                    </pre>
                  ) : (
                    <span>No error message reported.</span>
                  )}
                </div>
              ) : (
                <div className="text-center text-[12px] text-muted-foreground">
                  <Loader2 className="mx-auto mb-2 h-4 w-4 animate-spin" />
                  Render in progress
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      {done && src && (
        <div className="mt-4 flex items-center justify-center gap-2">
          <Button asChild variant="outline" size="sm">
            <a href={src} download>
              <Download className="h-3.5 w-3.5" />
              Download mp4
            </a>
          </Button>
        </div>
      )}
    </div>
  );
}

function PublishedCard({ job }: { job: Job | null }) {
  if (!job?.youtube_url) return null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="rounded-xl border border-emerald-500/25 bg-emerald-500/5 p-5"
    >
      <div className="flex items-center gap-2.5">
        <Check className="h-4 w-4 text-emerald-300" />
        <div>
          <div className="text-[13px] font-medium tracking-tight">Published to YouTube</div>
          <a
            href={job.youtube_url}
            target="_blank"
            rel="noreferrer"
            className="mt-0.5 inline-flex items-center gap-1 font-mono text-[11px] text-emerald-200 hover:underline"
          >
            {job.youtube_url}
            <ExternalLink className="h-3 w-3" />
          </a>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="ml-auto h-7 w-7"
          onClick={() => {
            navigator.clipboard.writeText(job.youtube_url ?? "");
            toast.success("Copied");
          }}
        >
          <Copy className="h-3 w-3" />
        </Button>
      </div>
    </motion.div>
  );
}

function MetaCard({ job }: { job: Job | null }) {
  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        Job details
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-5 gap-y-3 text-[12.5px]">
        <Field label="Channel" value={job ? channelLabel(job.channel) : null} />
        <Field label="Stage" value={job?.stage ?? null} mono />
        <Field label="Created" value={job ? relativeTime(job.created_at) : null} />
        <Field label="Updated" value={job ? relativeTime(job.updated_at) : null} />
        <Field label="Job id" value={job?.job_id ?? null} mono full />
      </dl>
    </div>
  );
}

function Field({
  label,
  value,
  mono,
  full,
}: {
  label: string;
  value: string | null | undefined;
  mono?: boolean;
  full?: boolean;
}) {
  return (
    <div className={full ? "col-span-2" : undefined}>
      <dt className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </dt>
      <dd className={cn("mt-0.5 truncate text-foreground/90", mono && "font-mono text-[11px]")}>
        {value ?? "—"}
      </dd>
    </div>
  );
}

function StageTimeline({ job }: { job: Job | null }) {
  const timeline = useMemo<TimelineEntry[]>(() => {
    if (job?.timeline?.length) return job.timeline;
    return STAGE_ORDER.map((s) => ({ stage: s, status: "pending" as const }));
  }, [job]);

  // E2/E3 (2026-05-13): exclude pre-marked "skipped" stages from
  // the progress arithmetic so a long-form render's pill that
  // legitimately doesn't apply (cast / asr-when-authored) doesn't
  // inflate "completed". Detected via msg containing the word
  // "skipped" (the worker writes msgs like "skipped — long-form
  // has no cast stage" and "skipped — captions aligned from
  // authored TTS chunk timings"). Pre-fix, "1/4 stages truly done
  // but 3/7 done · skipped pre-marks" rendered as "3 / 7 stages,
  // 43%" which way overstated progress.
  const isSkipped = (t: TimelineEntry) =>
    t.status === "done" && typeof t.msg === "string" && t.msg.toLowerCase().includes("skipped");
  const realStages = timeline.filter((t) => !isSkipped(t));
  const completed = realStages.filter((t) => t.status === "done").length;
  const total = realStages.length;
  const pct = Math.round((completed / Math.max(total, 1)) * 100);

  return (
    <div className="rounded-xl border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Pipeline
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">
            {completed} / {total} stages
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="hidden h-1.5 w-32 overflow-hidden rounded-full bg-surface-2 sm:block">
            <div
              className="h-full bg-foreground/85 transition-[width] duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className="font-mono text-[11px] text-muted-foreground">{pct}%</span>
        </div>
      </div>
      <ol className="space-y-px p-3">
        {timeline.map((t, i) => (
          <TimelineRow key={`${t.stage}-${i}`} entry={t} />
        ))}
      </ol>
    </div>
  );
}

function TimelineRow({ entry }: { entry: TimelineEntry }) {
  const label = entry.label ?? STAGE_LABELS[entry.stage] ?? entry.stage;
  const tone =
    entry.status === "done"
      ? "text-foreground"
      : entry.status === "running"
        ? "text-foreground"
        : entry.status === "failed"
          ? "text-rose-300"
          : "text-muted-foreground";
  return (
    <li className="flex items-center gap-3 rounded-md px-3 py-2.5 transition-colors hover:bg-surface-2">
      <span className="grid h-5 w-5 shrink-0 place-items-center">
        {entry.status === "done" && <Check className="h-3.5 w-3.5 text-emerald-300" />}
        {entry.status === "running" && <Loader2 className="h-3.5 w-3.5 animate-spin text-violet-300" />}
        {entry.status === "failed" && <X className="h-3.5 w-3.5 text-rose-300" />}
        {entry.status === "pending" && <span className="h-1.5 w-1.5 rounded-full bg-border-strong" />}
      </span>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2">
          <span className={cn("text-[12.5px] tracking-tight", tone)}>{label}</span>
          <span className="font-mono text-[10px] text-muted-foreground/70">{entry.stage}</span>
        </div>
        {entry.msg && (
          <span className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">{entry.msg}</span>
        )}
      </div>
      {entry.ts && entry.status !== "pending" && (
        <span className="font-mono text-[10px] text-muted-foreground">
          <Timer className="mr-1 inline-block h-3 w-3" />
          {relativeTime(entry.ts)}
        </span>
      )}
    </li>
  );
}

function PublishDialog({ job, onPublished }: { job: Job; onPublished: (j: Job) => void }) {
  const [open, setOpen] = useState(false);
  const [visibility, setVisibility] = useState<"public" | "unlisted" | "private">("unlisted");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit() {
    setSubmitting(true);
    try {
      const res = await jobsApi.publish(job.job_id, {
        visibility,
        title: title.trim() || undefined,
        description: description.trim() || undefined,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      });
      if (res.youtube_url) {
        toast.success("Published", { description: res.youtube_url });
        onPublished({ ...job, youtube_url: res.youtube_url });
      } else {
        toast.success("Submitted");
      }
      setOpen(false);
    } catch (e) {
      toast.error("Publish failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">
          <Send className="h-3.5 w-3.5" />
          Publish
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Publish to YouTube</DialogTitle>
          <DialogDescription>
            Override metadata or accept the channel&apos;s defaults.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="vis" className="text-[12px]">
              Visibility
            </Label>
            <Select value={visibility} onValueChange={(v: string) => setVisibility(v as typeof visibility)}>
              <SelectTrigger id="vis">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="public">Public</SelectItem>
                <SelectItem value="unlisted">Unlisted (default)</SelectItem>
                <SelectItem value="private">Private</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="title" className="text-[12px]">
              Title override
            </Label>
            <Input
              id="title"
              placeholder={job.topic ?? ""}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={100}
              className="bg-surface-2"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="desc" className="text-[12px]">
              Description
            </Label>
            <Textarea
              id="desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Channel default if blank"
              className="min-h-[88px] bg-surface-2"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="tags" className="text-[12px]">
              Tags (comma-separated)
            </Label>
            <Input
              id="tags"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="shorts, tutorial, ai"
              className="bg-surface-2"
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button size="sm" onClick={submit} disabled={submitting}>
            {submitting ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Publishing
              </>
            ) : (
              <>
                <Wand2 className="h-3.5 w-3.5" />
                Publish
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Slice 4 — live artifact previews. Each per-render intermediate (script,
// narration, beats, images, video) appears here as soon as
// pipeline.render.artifacts.emit_artifact uploads it to GCS and writes
// the matching field on the Firestore job doc. The render-detail page's
// 800 ms poll loop picks up new entries and this component renders
// inline previews — script as text/blob link, narration as <audio>,
// images/panels as a responsive grid, intermediate video as a
// download link. Until the final mp4 is published these are labeled
// "Intermediate" so the user doesn't confuse a half-rendered job with
// the shipped output.
// ---------------------------------------------------------------------------

function LiveArtifactsCard({ job }: { job: Job | null }) {
  const artifacts = job?.artifacts ?? null;
  if (!job) return null;

  const script = singleArtifact(artifacts, "script");
  const envelope = singleArtifact(artifacts, "envelope");
  const narration = singleArtifact(artifacts, "narration");
  const beats = singleArtifact(artifacts, "beats");
  const video = singleArtifact(artifacts, "video");
  const images = listArtifact(artifacts, "images");
  const panels = listArtifact(artifacts, "panels");

  const intermediate = job.status === "rendering" || job.status === "uploading";

  const anyArtifacts =
    !!script || !!narration || !!beats || !!video || !!envelope ||
    images.length > 0 || panels.length > 0;

  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Live previews
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">
            Stage outputs as they land
          </div>
        </div>
        {intermediate && anyArtifacts && (
          <span className="rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[0.15em] text-amber-200">
            Intermediate
          </span>
        )}
      </div>

      {!anyArtifacts && (
        <div className="mt-4 flex items-center gap-2 rounded-lg border border-dashed border-border/60 bg-background/50 px-3 py-3 text-[12px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {/*
            E5 fix (2026-05-13): the previous "script lands ~5 s into
            the render" copy lied for long-form (script lands ~3 min)
            and for footage_only (no script artifact at all). Generic
            wording avoids the false promise.
          */}
          Waiting for the first artifact (script lands once the rewriter completes)…
        </div>
      )}

      {(script || envelope) && (
        <ArtifactBlock
          title={envelope ? "Sectioned envelope (long-form)" : "Script"}
          subtitle={
            envelope
              ? envelopeSubtitle(envelope)
              : scriptSubtitle(script ?? envelope!)
          }
          href={jobsApi.artifactUrl(job.job_id, envelope ? "envelope" : "script")}
          downloadName={envelope ? "envelope.json" : "script.json"}
        />
      )}
      {narration && (
        <ArtifactBlock
          title="Narration"
          subtitle={narrationSubtitle(narration)}
          href={jobsApi.artifactUrl(job.job_id, "narration")}
          audio
        />
      )}
      {beats && (
        <ArtifactBlock
          title="Beats / alignment"
          subtitle={beatsSubtitle(beats)}
          href={jobsApi.artifactUrl(job.job_id, "beats")}
          downloadName="beats.json"
        />
      )}
      {(images.length > 0 || panels.length > 0) && (
        <ImageGalleryBlock
          jobId={job.job_id}
          kind={images.length > 0 ? "images" : "panels"}
          entries={images.length > 0 ? images : panels}
        />
      )}
      {video && (
        <ArtifactBlock
          title="Video (intermediate)"
          subtitle={videoSubtitle(video)}
          href={jobsApi.artifactUrl(job.job_id, "video")}
          downloadName="video.mp4"
        />
      )}
    </div>
  );
}

function singleArtifact(
  artifacts: Job["artifacts"],
  kind: string,
): ArtifactEntry | null {
  if (!artifacts) return null;
  const entry = artifacts[kind];
  if (!entry || Array.isArray(entry)) return null;
  if (entry.status !== "ready") return null;
  return entry;
}

function listArtifact(
  artifacts: Job["artifacts"],
  kind: string,
): ArtifactEntry[] {
  if (!artifacts) return [];
  const entry = artifacts[kind];
  if (!entry || !Array.isArray(entry)) return [];
  return entry;
}

function scriptSubtitle(entry: ArtifactEntry): string {
  const hook = (entry.hook as string | undefined) || "";
  const n = (entry.n_words as number | undefined) ?? 0;
  return [hook ? `"${hook.slice(0, 80)}${hook.length > 80 ? "…" : ""}"` : null, n ? `${n} words` : null]
    .filter(Boolean)
    .join(" · ");
}

function envelopeSubtitle(entry: ArtifactEntry): string {
  const sections = (entry.n_sections as number | undefined) ?? 0;
  const panels = (entry.n_panels as number | undefined) ?? 0;
  return `${sections} sections · ${panels} panels`;
}

function narrationSubtitle(entry: ArtifactEntry): string {
  const dur = entry.duration_s as number | undefined;
  if (typeof dur !== "number" || dur <= 0) return "audio ready";
  const m = Math.floor(dur / 60);
  const s = Math.round(dur % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function beatsSubtitle(entry: ArtifactEntry): string {
  const n = entry.n_beats as number | undefined;
  return typeof n === "number" ? `${n} beats` : "alignment ready";
}

function videoSubtitle(entry: ArtifactEntry): string {
  const dur = entry.duration_s as number | undefined;
  const w = entry.width as number | undefined;
  const h = entry.height as number | undefined;
  const parts: string[] = [];
  if (w && h) parts.push(`${w}×${h}`);
  if (typeof dur === "number" && dur > 0) {
    const m = Math.floor(dur / 60);
    const s = Math.round(dur % 60);
    parts.push(`${m}:${String(s).padStart(2, "0")}`);
  }
  return parts.length ? parts.join(" · ") : "video ready";
}

function ArtifactBlock({
  title,
  subtitle,
  href,
  downloadName,
  audio,
}: {
  title: string;
  subtitle: string;
  href: string;
  downloadName?: string;
  audio?: boolean;
}) {
  return (
    <div className="mt-4 rounded-lg border border-border/70 bg-background/50 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-medium tracking-tight">{title}</div>
          <div className="mt-0.5 truncate text-[11.5px] text-muted-foreground">{subtitle}</div>
        </div>
        <Button asChild variant="outline" size="sm">
          <a href={href} download={downloadName} target={audio ? undefined : "_blank"} rel="noreferrer">
            {audio ? <Play className="h-3.5 w-3.5" /> : <Download className="h-3.5 w-3.5" />}
            {audio ? "Listen" : "Open"}
          </a>
        </Button>
      </div>
      {audio && (
        <audio
          key={href}
          controls
          src={href}
          className="mt-2 h-8 w-full"
          preload="none"
        />
      )}
    </div>
  );
}

function ImageGalleryBlock({
  jobId,
  kind,
  entries,
}: {
  jobId: string;
  kind: "images" | "panels";
  entries: ArtifactEntry[];
}) {
  const ready = entries.filter((e) => e?.status === "ready");
  if (ready.length === 0 && entries.length === 0) return null;
  const total = entries.length;
  const readyCount = ready.length;
  return (
    <div className="mt-4 rounded-lg border border-border/70 bg-background/50 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] font-medium tracking-tight">
            {kind === "panels" ? "Long-form panels" : "Generated images"}
          </div>
          <div className="mt-0.5 truncate text-[11.5px] text-muted-foreground">
            {readyCount} of {total} ready
          </div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-4 gap-1.5 sm:grid-cols-6">
        {entries.map((entry, i) => {
          const idx = (entry?.i as number | undefined) ?? i;
          const status = entry?.status ?? "pending";
          if (status === "ready") {
            return (
              // eslint-disable-next-line @next/next/no-img-element
              <a
                key={idx}
                href={jobsApi.artifactUrl(jobId, kind, idx)}
                target="_blank"
                rel="noreferrer"
                className="group relative aspect-square overflow-hidden rounded-md border border-border/40 bg-background"
              >
                <img
                  src={jobsApi.artifactUrl(jobId, kind, idx)}
                  alt={`${kind} ${idx}`}
                  className="h-full w-full object-cover transition group-hover:opacity-80"
                  loading="lazy"
                />
              </a>
            );
          }
          return (
            <div
              key={idx}
              className={cn(
                "flex aspect-square items-center justify-center rounded-md border border-dashed",
                status === "failed"
                  ? "border-rose-500/40 bg-rose-500/5 text-rose-300"
                  : "border-border/50 bg-background/40 text-muted-foreground",
              )}
            >
              {status === "failed" ? (
                <X className="h-3 w-3" />
              ) : (
                <Loader2 className="h-3 w-3 animate-spin" />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
