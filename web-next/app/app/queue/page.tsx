"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  Gavel,
  ListChecks,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/app/page-header";
import { ChannelIcon, channelLabel } from "@/components/app/channel-icon";
import { StatusPill } from "@/components/app/status-pill";
import { EmptyState } from "@/components/app/empty-state";
import { critiquesApi, jobsApi, queueApi } from "@/lib/api";
import type {
  CritiqueDetail,
  Job,
  OperatorVerdict,
  QueueHeldEntry,
  QueueState,
  ResolveHoldAction,
  ResolveHoldResponse,
} from "@/lib/types";
import { relativeTime } from "@/lib/utils";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";

export default function QueuePage() {
  // Stale-while-revalidate: paint the previously-cached queue payload
  // instantly on mount (from in-memory + localStorage), then refresh
  // every 2 s. The new poll cadence is unchanged from the legacy raw
  // useVisiblePoll(refresh, 2000) but every NAVIGATION to /app/queue
  // now repaints from cache instead of flashing skeletons.
  const { data: queue, refresh, isLoading } = useStaleWhileRevalidate<QueueState>(
    CK.queueState,
    () => queueApi.get(),
    2000,
    // 2 s poll is intentionally tight for a live queue. Disable the
    // stale-fresh skip-on-mount gate so every poll really does fire.
    { freshForMs: 0 },
  );
  const refreshing = isLoading;

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio · Queue"
        title="Queue"
        description="What's running, what's waiting, what just shipped, what's held by the critic."
        actions={
          <Button variant="ghost" size="sm" onClick={refresh}>
            <RefreshCw className={refreshing ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
            Refresh
          </Button>
        }
      />

      <div className="grid flex-1 gap-5 px-6 py-6 md:grid-cols-2 md:px-8 xl:grid-cols-4">
        <Column
          title="Running"
          icon={Play}
          tone="text-violet-300"
          jobs={queue?.running ?? null}
          emptyHint="No worker active. Render something to wake the sim."
          showCancel
        />
        <Column
          title="Queued"
          icon={Loader2}
          tone="text-amber-300"
          jobs={queue?.queued ?? null}
          emptyHint="The queue is empty. The next render lands here."
          showCancel
        />
        <Column
          title="Completed"
          icon={CheckCircle2}
          tone="text-emerald-300"
          jobs={queue?.completed ?? null}
          emptyHint="Recently shipped renders show up here. Click to review."
        />
        <HeldColumn held={queue?.held ?? null} onResolved={refresh} />
      </div>
    </div>
  );
}

function Column({
  title,
  icon: Icon,
  tone,
  jobs,
  emptyHint,
  showCancel,
}: {
  title: string;
  icon: typeof Play;
  tone: string;
  jobs: Job[] | null;
  emptyHint: string;
  showCancel?: boolean;
}) {
  return (
    <section className="flex flex-col rounded-xl border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
        <div className="flex items-center gap-2">
          <Icon className={`h-3.5 w-3.5 ${tone}`} />
          <div className="text-[13px] font-medium tracking-tight">{title}</div>
        </div>
        <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
          {jobs ? jobs.length : "—"}
        </span>
      </div>
      <div className="flex-1 space-y-2 p-4">
        {jobs === null ? (
          <>
            <Skeleton className="h-16" />
            <Skeleton className="h-16" />
          </>
        ) : jobs.length === 0 ? (
          <EmptyState icon={ListChecks} title="Empty" description={emptyHint} className="border-none bg-transparent py-8" />
        ) : (
          jobs.map((j) => <QueueRow key={j.job_id} job={j} showCancel={showCancel} />)
        )}
      </div>
    </section>
  );
}

function QueueRow({ job, showCancel }: { job: Job; showCancel?: boolean }) {
  async function cancel(e: React.MouseEvent) {
    // Row-as-Link wraps the cancel button; without stopping the click
    // we'd both fire the cancel mutation AND navigate to the render
    // detail page. Stop both so the cancel reads as a clean intent.
    e.preventDefault();
    e.stopPropagation();
    try {
      await jobsApi.cancel(job.job_id);
      toast.success("Cancelled");
    } catch (err) {
      toast.error("Cancel failed", { description: err instanceof Error ? err.message : String(err) });
    }
  }
  return (
    <Link
      href={`/app/render/${job.job_id}`}
      className="flex items-center gap-3 rounded-md border border-border bg-surface-2/40 p-3 transition-colors hover:border-border-strong hover:bg-surface-2/70"
    >
      <ChannelIcon channel={job.channel ?? ""} size="sm" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12.5px] font-medium tracking-tight">
          {job.topic ?? "(untitled)"}
        </div>
        <div className="mt-0.5 truncate font-mono text-[10.5px] text-muted-foreground">
          {channelLabel(job.channel)} · {job.stage ?? "—"}
          {job.updated_at ? ` · ${relativeTime(job.updated_at)}` : ""}
        </div>
      </div>
      <StatusPill
        status={job.status}
        pulse={job.status === "rendering" || job.status === "uploading"}
      />
      {showCancel && (
        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground hover:text-rose-300" onClick={cancel}>
          <X className="h-3 w-3" />
        </Button>
      )}
    </Link>
  );
}

function HeldColumn({
  held,
  onResolved,
}: {
  held: QueueState["held"] | null;
  onResolved: () => void;
}) {
  // Single dialog instance hoisted to the column so the user clicking
  // through several held items in a row reuses one mounted modal — keeps
  // the open/close transition snappy and ensures only one critique is
  // visible at a time.
  const [active, setActive] = useState<QueueHeldEntry | null>(null);

  return (
    <section className="flex flex-col rounded-xl border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
        <div className="flex items-center gap-2">
          <Pause className="h-3.5 w-3.5 text-rose-300" />
          <div className="text-[13px] font-medium tracking-tight">Held by critic</div>
        </div>
        <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
          {held ? held.length : "—"}
        </span>
      </div>
      <div className="flex-1 space-y-2 p-4">
        {held === null ? (
          <>
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
          </>
        ) : held.length === 0 ? (
          <EmptyState
            icon={Pause}
            title="Nothing held"
            description="Critic verdicts that score below threshold land here."
            className="border-none bg-transparent py-8"
          />
        ) : (
          held.map((h) => (
            // Held entries don't carry a job_id — they're keyed by
            // (channel, slug) from _holds.json — so there's no
            // /app/render/{id} target. Instead clicking opens the actual
            // critique markdown the reviewer wrote (the file path is in
            // holds.json as `source_critique`), so the operator can read
            // the verdict + per-rubric scores + fix instructions without
            // leaving the Queue.
            <button
              key={`${h.channel}-${h.slug}`}
              type="button"
              onClick={() => setActive(h)}
              className="block w-full rounded-md border border-rose-500/20 bg-rose-500/5 p-3 text-left transition-colors hover:border-rose-500/40 hover:bg-rose-500/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-500/40"
            >
              <div className="flex items-center gap-2">
                <ChannelIcon channel={h.channel} size="sm" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[12.5px] font-medium tracking-tight">{h.slug}</div>
                  <div className="mt-0.5 truncate font-mono text-[10.5px] text-muted-foreground">
                    {channelLabel(h.channel)}
                    {h.set_at ? ` · ${relativeTime(h.set_at)}` : h.held_at ? ` · ${relativeTime(h.held_at)}` : ""}
                  </div>
                </div>
              </div>
              {h.reason && (
                <p className="mt-2 line-clamp-2 text-[12px] leading-relaxed text-rose-200/80">{h.reason}</p>
              )}
            </button>
          ))
        )}
      </div>
      <HeldCritiqueDialog
        entry={active}
        onClose={() => setActive(null)}
        onResolved={() => {
          setActive(null);
          onResolved();
        }}
      />
    </section>
  );
}

function verdictTone(verdict?: string | null): string {
  if (verdict === "SHIP") return "text-emerald-300 border-emerald-500/30 bg-emerald-500/5";
  if (verdict === "BLOCK") return "text-rose-300 border-rose-500/30 bg-rose-500/5";
  if (verdict === "FIX") return "text-amber-300 border-amber-500/30 bg-amber-500/5";
  return "text-muted-foreground border-border bg-surface-2";
}

function HeldCritiqueDialog({
  entry,
  onClose,
  onResolved,
}: {
  entry: QueueHeldEntry | null;
  onClose: () => void;
  onResolved: () => void;
}) {
  const [data, setData] = useState<CritiqueDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Reset to "critique" when a new entry is opened so each open starts on
  // the read-only view; the resolve tab is opt-in per render.
  const [tab, setTab] = useState<"critique" | "resolve">("critique");

  useEffect(() => {
    if (!entry) {
      setData(null);
      setError(null);
      return;
    }
    setTab("critique");
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    critiquesApi
      .getHeld(entry.channel, entry.slug)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [entry]);

  const open = entry !== null;
  return (
    <Dialog open={open} onOpenChange={(o) => (!o ? onClose() : undefined)}>
      <DialogContent className="max-h-[88vh] max-w-3xl overflow-hidden p-0">
        <div className="flex max-h-[88vh] flex-col">
          <DialogHeader className="border-b border-border px-6 py-4">
            <div className="flex items-center gap-2">
              <ChannelIcon channel={entry?.channel ?? ""} size="sm" />
              <DialogTitle className="text-[14px] font-medium tracking-tight">
                {entry?.slug ?? "—"}
              </DialogTitle>
              {data?.verdict && (
                <span
                  className={`ml-2 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] ${verdictTone(data.verdict)}`}
                >
                  {data.verdict}
                </span>
              )}
            </div>
            <DialogDescription className="mt-1 font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
              {channelLabel(entry?.channel)}
              {data?.date ? ` · ${data.date}` : ""}
              {data?.total != null && data?.avg != null
                ? ` · ${data.total} / 500 · avg ${data.avg.toFixed(1)} / 10`
                : ""}
            </DialogDescription>
          </DialogHeader>

          <Tabs
            value={tab}
            onValueChange={(v) => setTab(v as "critique" | "resolve")}
            className="flex flex-1 flex-col overflow-hidden"
          >
            <TabsList className="mx-6 mt-4 grid w-fit grid-cols-2">
              <TabsTrigger value="critique" className="gap-1.5">
                <Eye className="h-3.5 w-3.5" />
                Critique
              </TabsTrigger>
              <TabsTrigger value="resolve" className="gap-1.5">
                <Gavel className="h-3.5 w-3.5" />
                Resolve
              </TabsTrigger>
            </TabsList>

            <TabsContent value="critique" className="mt-0 flex-1 overflow-y-auto px-6 py-4">
              <CritiqueBody loading={loading} error={error} data={data} />
            </TabsContent>

            <TabsContent value="resolve" className="mt-0 flex-1 overflow-y-auto px-6 py-4">
              {entry && (
                <ResolveWorkflow
                  entry={entry}
                  hasAiCritique={!!data?.exists}
                  onResolved={onResolved}
                />
              )}
            </TabsContent>
          </Tabs>

          <DialogFooter className="border-t border-border px-6 py-3">
            {data?.path && (
              <span className="mr-auto truncate font-mono text-[10.5px] text-muted-foreground">
                {data.path}
              </span>
            )}
            <Button variant="outline" size="sm" onClick={onClose}>
              Close
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CritiqueBody({
  loading,
  error,
  data,
}: {
  loading: boolean;
  error: string | null;
  data: CritiqueDetail | null;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 py-6 text-[12px] text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Loading critique…
      </div>
    );
  }
  if (error) {
    return (
      <div className="flex items-start gap-2 rounded-md border border-rose-500/30 bg-rose-500/5 p-3 text-[12px] text-rose-200">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <div>
          <div className="font-medium">Couldn&apos;t load critique.</div>
          <div className="mt-0.5 font-mono text-[10.5px] text-rose-200/70">{error}</div>
        </div>
      </div>
    );
  }
  if (data && !data.exists) {
    return (
      <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-[12px] text-amber-200">
        <div className="font-medium">No critique file on disk yet.</div>
        <p className="mt-1 leading-relaxed text-amber-200/80">
          The slug is held in <code className="font-mono">_holds.json</code>, but the
          reviewer hasn&apos;t dropped a critique at:
        </p>
        <pre className="mt-2 overflow-x-auto rounded bg-background/40 p-2 font-mono text-[10.5px] text-amber-100/90">{data.path}</pre>
        <p className="mt-2 leading-relaxed text-amber-200/80">
          You can still resolve the hold via the <strong>Resolve</strong> tab — submit your
          own verdict or clear the hold.
        </p>
      </div>
    );
  }
  if (!data?.exists) return null;
  return (
    <div className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-3">
        <SummaryStat
          label="Weakest param"
          value={data.weakest_param ?? "—"}
          sub={data.weakest_score != null ? `${data.weakest_score} / 10` : undefined}
        />
        <SummaryStat
          label="Total"
          value={data.total != null ? `${data.total} / 500` : "—"}
          sub={data.avg != null ? `avg ${data.avg.toFixed(1)} / 10` : undefined}
        />
        <SummaryStat
          label="Critical fails"
          value={String(data.critical_failures?.length ?? 0)}
          sub={
            data.critical_failures && data.critical_failures.length > 0
              ? data.critical_failures.map(([p, s]) => `${p}(${s})`).join(", ")
              : "none"
          }
        />
      </div>

      {data.fix_instructions && (
        <section className="rounded-md border border-border bg-surface-2/40 p-4">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Specific fix instructions
          </div>
          <pre className="whitespace-pre-wrap font-mono text-[11.5px] leading-relaxed text-foreground/90">
            {data.fix_instructions}
          </pre>
        </section>
      )}

      <section className="rounded-md border border-border bg-surface-2/30 p-4">
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          Full critique
        </div>
        <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-foreground/85">
          {data.markdown}
        </pre>
      </section>
    </div>
  );
}

function ResolveWorkflow({
  entry,
  hasAiCritique,
  onResolved,
}: {
  entry: QueueHeldEntry;
  hasAiCritique: boolean;
  onResolved: () => void;
}) {
  // Three workflows the operator can pick from. Each has wildly
  // different state implications (writes critique file vs archives
  // existing vs pure unhold), so they're surfaced as a radio choice
  // first — then the form below adapts. This keeps the "I clicked the
  // wrong destructive button" failure mode away.
  const [action, setAction] = useState<ResolveHoldAction>("operator_verdict");
  const [verdict, setVerdict] = useState<OperatorVerdict>("SHIP");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ResolveHoldResponse | null>(null);

  async function submit() {
    setSubmitting(true);
    try {
      const body =
        action === "operator_verdict"
          ? { action, verdict, notes }
          : { action };
      const r = await critiquesApi.resolve(entry.channel, entry.slug, body);
      setResult(r);
      const verb =
        action === "clear"
          ? "Hold cleared"
          : action === "request_recritique"
            ? "Re-critique requested"
            : `Verdict ${verdict} recorded`;
      toast.success(verb, { description: r.next_step_hint ?? undefined });
      // Brief pause so the operator can read the success state before
      // the dialog closes and the queue refreshes.
      setTimeout(onResolved, 1500);
    } catch (e) {
      toast.error("Resolve failed", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setSubmitting(false);
    }
  }

  if (result) {
    // Success state — show what changed + the next-step hint.
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-4 text-[12px] text-emerald-100">
          <div className="flex items-center gap-2 font-medium">
            <CheckCircle2 className="h-4 w-4" /> Resolved
          </div>
          <div className="mt-2 grid gap-1 font-mono text-[11px] text-emerald-100/85">
            <div>action: {result.action}</div>
            <div>hold cleared: {String(result.hold_cleared)}</div>
            {result.new_hold_reason && <div>new hold reason: {result.new_hold_reason}</div>}
            {result.operator_critique_path && (
              <div>operator critique: {result.operator_critique_path}</div>
            )}
            {result.archived_critique_path && (
              <div>archived critique: {result.archived_critique_path}</div>
            )}
          </div>
        </div>
        {result.next_step_hint && (
          <div className="rounded-md border border-border bg-surface-2/40 p-3 text-[12px] leading-relaxed text-muted-foreground">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
              Next step
            </div>
            {result.next_step_hint}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-[12px] leading-relaxed text-muted-foreground">
        Pick how to clear this hold. Each path writes an audit trail and updates STATUS.md so
        the next operator sees what happened.
      </p>

      <div className="grid gap-2">
        <ActionChoice
          checked={action === "operator_verdict"}
          onSelect={() => setAction("operator_verdict")}
          icon={Gavel}
          title="Submit my own verdict"
          desc="Override the AI critic with SHIP / FIX / BLOCK + notes. SHIP clears the hold; FIX/BLOCK rewrites the reason. Writes a side-by-side operator critique markdown for audit."
        />
        <ActionChoice
          checked={action === "request_recritique"}
          onSelect={() => setAction("request_recritique")}
          icon={Sparkles}
          title="Request fresh AI re-critique"
          desc={`Archive the current critique${hasAiCritique ? "" : " (none on disk yet)"} and clear the hold so a re-rendered mp4 can flow through. The website doesn't run the judge — you'll get a CLI hint to launch /judge-video.`}
        />
        <ActionChoice
          checked={action === "clear"}
          onSelect={() => setAction("clear")}
          icon={Trash2}
          title="Clear hold (dismiss)"
          desc="Pure unhold, no audit file. Use when the critic was wrong about a non-issue."
        />
      </div>

      {action === "operator_verdict" && (
        <div className="space-y-3 rounded-md border border-border bg-surface-2/30 p-4">
          <div className="space-y-1.5">
            <Label className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
              Your verdict
            </Label>
            <div className="flex gap-2">
              {(["SHIP", "FIX", "BLOCK"] as OperatorVerdict[]).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setVerdict(v)}
                  className={`flex-1 rounded-md border px-3 py-2 font-mono text-[11px] uppercase tracking-[0.16em] transition-colors ${
                    verdict === v
                      ? verdictTone(v)
                      : "border-border bg-surface-2/50 text-muted-foreground hover:border-border-strong"
                  }`}
                >
                  {v}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label
              htmlFor="operator-notes"
              className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground"
            >
              Notes (audit trail)
            </Label>
            <Textarea
              id="operator-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder={
                verdict === "SHIP"
                  ? "Why are you overriding the critic? e.g. 'Audio clip in original was a non-issue, manually verified.'"
                  : "What still needs fixing? e.g. 'Re-render with peak limiter at -1.0 dBTP.'"
              }
              className="min-h-[90px] font-mono text-[11.5px]"
            />
          </div>
        </div>
      )}

      <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
        <Button variant="ghost" size="sm" onClick={() => setResult(null)} disabled={submitting}>
          Reset
        </Button>
        <Button onClick={submit} size="sm" disabled={submitting}>
          {submitting ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5" />
          )}
          {action === "operator_verdict"
            ? `Submit ${verdict}`
            : action === "request_recritique"
              ? "Archive & request"
              : "Clear hold"}
        </Button>
      </div>
    </div>
  );
}

function ActionChoice({
  checked,
  onSelect,
  icon: Icon,
  title,
  desc,
}: {
  checked: boolean;
  onSelect: () => void;
  icon: typeof Gavel;
  title: string;
  desc: string;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`flex items-start gap-3 rounded-md border p-3 text-left transition-colors ${
        checked
          ? "border-violet-500/40 bg-violet-500/5"
          : "border-border bg-surface-2/30 hover:border-border-strong hover:bg-surface-2/50"
      }`}
    >
      <span
        className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${
          checked ? "border-violet-400 bg-violet-500/20" : "border-border-strong bg-surface-2"
        }`}
      >
        {checked && <span className="h-1.5 w-1.5 rounded-full bg-violet-300" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
          <div className="text-[12.5px] font-medium tracking-tight">{title}</div>
        </div>
        <p className="mt-1 text-[11.5px] leading-relaxed text-muted-foreground">{desc}</p>
      </div>
    </button>
  );
}

function SummaryStat({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="rounded-md border border-border bg-surface-2/40 p-3">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 truncate text-[13px] font-medium tracking-tight">{value}</div>
      {sub && (
        <div className="mt-0.5 truncate font-mono text-[10.5px] text-muted-foreground">{sub}</div>
      )}
    </div>
  );
}
