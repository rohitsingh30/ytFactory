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
  Sparkles,
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
import { jobsApi, pollJob } from "@/lib/api";
import type { Job, TimelineEntry } from "@/lib/types";
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

  const previewSrc = job?.preview_url ?? jobsApi.previewUrl(jobId);

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
              <Link href="/app/library">
                <ArrowLeft className="h-3.5 w-3.5" />
                Library
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
            <Link href="/app/library">Back to library</Link>
          </Button>
        </div>
      )}

      {!error && (
        <div className="grid flex-1 gap-6 px-6 py-6 md:px-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
          {/* Left — player + meta */}
          <div className="flex flex-col gap-5">
            <PlayerCard job={job} src={previewSrc} />
            <PublishedCard job={job} />
            <MetaCard job={job} />
          </div>

          {/* Right — timeline + critique + script */}
          <div className="flex flex-col gap-5">
            <StageTimeline job={job} />
            <CritiqueCard job={job} />
            <ScriptCard job={job} />
          </div>
        </div>
      )}
    </div>
  );
}

function PlayerCard({ job, src }: { job: Job | null; src: string }) {
  const ready = job && (job.status === "done" || job.status === "uploading" || job.status === "rendering");
  const done = job?.status === "done";
  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Preview
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">9:16 · auto-loop</div>
        </div>
        {job && <StatusPill status={job.status} pulse={job.status === "rendering" || job.status === "uploading"} />}
      </div>
      <div className="mt-4 flex justify-center">
        <div className="relative aspect-[9/16] w-full max-w-xs overflow-hidden rounded-lg border border-border bg-background">
          {ready ? (
            <video
              key={src}
              src={src}
              controls
              autoPlay
              loop
              muted
              playsInline
              className="h-full w-full bg-black"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center bg-surface-2">
              {!job ? (
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              ) : job.status === "failed" || job.status === "cancelled" ? (
                <div className="text-center text-[12px] text-muted-foreground">
                  <X className="mx-auto mb-2 h-4 w-4" />
                  No preview
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
      {done && (
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

  const completed = timeline.filter((t) => t.status === "done").length;
  const total = timeline.length;
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

function CritiqueCard({ job }: { job: Job | null }) {
  if (!job?.critique) return null;
  const v = job.critique;
  const tone =
    v.verdict === "SHIP"
      ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-200"
      : v.verdict === "FIX"
        ? "border-amber-500/30 bg-amber-500/5 text-amber-200"
        : "border-rose-500/30 bg-rose-500/5 text-rose-200";
  return (
    <div className={cn("rounded-xl border bg-surface p-5", tone)}>
      <div className="flex items-center gap-2">
        <Sparkles className="h-3.5 w-3.5" />
        <div className="font-mono text-[10px] uppercase tracking-[0.18em]">Critic</div>
        <span className="ml-auto rounded-md border border-current/30 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em]">
          {v.verdict}
        </span>
      </div>
      {v.weakest_param && (
        <div className="mt-3 text-[12.5px] tracking-tight text-foreground">
          weakest param ·{" "}
          <span className="font-mono text-[12px] text-muted-foreground">{v.weakest_param}</span>
        </div>
      )}
      {v.notes && <p className="mt-2 text-[12.5px] leading-relaxed">{v.notes}</p>}
    </div>
  );
}

function ScriptCard({ job }: { job: Job | null }) {
  const proposal = job?.proposal as Record<string, unknown> | undefined;
  if (!proposal) return null;
  const topic = proposal.topic as string | undefined;
  const notes = proposal.notes as string | undefined;
  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        Proposal
      </div>
      {topic && (
        <div className="mt-3">
          <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-muted-foreground">Topic</div>
          <p className="mt-1 text-[13px] leading-relaxed text-foreground">{topic}</p>
        </div>
      )}
      {notes && (
        <div className="mt-4">
          <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-muted-foreground">Notes</div>
          <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">{notes}</p>
        </div>
      )}
    </div>
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
