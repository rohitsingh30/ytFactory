"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import {
  AlertOctagon,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDashed,
  CircleSlash,
  Clock,
  ListChecks,
  Loader2,
  Package,
  XCircle,
} from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, ApiError } from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";
import { cn } from "@/lib/utils";

interface JobStage {
  name: string;
  duration_ms: number | null;
  success: boolean;
  ts: number | null;
  provider: string | null;
}

interface JobRow {
  job_id: string;
  channel: string | null;
  topic: string | null;
  status: string;
  stage: string | null;
  render_kind: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  duration_ms: number | null;
  stage_total_ms: number;
  stages: JobStage[];
  youtube_url: string | null;
  cloud_execution: string | null;
}

interface JobsResponse {
  hours: number;
  jobs: JobRow[];
  error: string | null;
}

const STATUS_ORDER = [
  "all",
  "failed",
  "done",
  "rendering",
  "uploading",
  "pending",
  "cancelled",
] as const;

const STATUS_BADGE: Record<string, { color: string; label: string; icon: typeof CheckCircle2 }> = {
  done:        { color: "text-emerald-300 bg-emerald-300/10",   label: "DONE",        icon: CheckCircle2 },
  failed:      { color: "text-rose-300 bg-rose-300/10",         label: "FAILED",      icon: XCircle },
  cancelled:   { color: "text-slate-400 bg-slate-400/10",       label: "CANCELLED",   icon: CircleSlash },
  rendering:   { color: "text-amber-300 bg-amber-300/10",       label: "RENDERING",   icon: Loader2 },
  uploading:   { color: "text-amber-300 bg-amber-300/10",       label: "UPLOADING",   icon: Loader2 },
  pending:     { color: "text-sky-300 bg-sky-300/10",           label: "PENDING",     icon: CircleDashed },
};

const STAGE_COLORS: Record<string, string> = {
  "stage.rewrite":    "#fbbf24",
  "stage.cast":       "#fbbf24",
  "stage.shotlist":   "#fbbf24",
  "stage.tts":        "#7dd3fc",
  "stage.image":      "#a78bfa",
  "stage.images":     "#a78bfa",
  "stage.compose":    "#34d399",
  "stage.upload":     "#f472b6",
  "stage.editing":    "#22d3ee",
  "stage.bootstrap":  "#94a3b8",
  "stage.dispatch":   "#94a3b8",
};
const DEFAULT_STAGE_COLOR = "#94a3b8";
const FAIL_COLOR = "#fb7185";

function colorForStage(s: JobStage): string {
  if (!s.success) return FAIL_COLOR;
  return STAGE_COLORS[s.name] ?? DEFAULT_STAGE_COLOR;
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
}

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const dt = (Date.now() - ts) / 1000;
  if (dt < 60) return `${Math.round(dt)} s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)} m ago`;
  if (dt < 86400) return `${Math.round(dt / 3600)} h ago`;
  return `${Math.round(dt / 86400)} d ago`;
}

function StageWaterfall({
  stages,
  totalMs,
  rowMaxMs,
}: {
  stages: JobStage[];
  totalMs: number;
  rowMaxMs: number;
}) {
  const widthPct = rowMaxMs > 0 ? (totalMs / rowMaxMs) * 100 : 0;
  const visible = stages.filter((s) => s.duration_ms != null && s.duration_ms > 0);
  const sum = visible.reduce((acc, s) => acc + (s.duration_ms ?? 0), 0);
  if (visible.length === 0) {
    // No stage durations — show a thin grey track so the row still
    // has a visible width tied to wall-clock duration.
    return (
      <div
        className="h-2.5 rounded-sm bg-slate-700/50"
        style={{ width: `${Math.max(widthPct, 2)}%` }}
        title={`${formatMs(totalMs)} (no stage data)`}
      />
    );
  }
  return (
    <div
      className="relative flex h-2.5 rounded-sm bg-surface/30"
      style={{ width: `${widthPct}%` }}
      title={`${formatMs(totalMs)} total`}
    >
      {visible.map((s, i) => {
        const pct = sum > 0 ? ((s.duration_ms ?? 0) / sum) * 100 : 0;
        return (
          <div
            key={`${s.name}-${i}`}
            className="h-full"
            style={{ width: `${pct}%`, backgroundColor: colorForStage(s) }}
            title={`${s.name}: ${formatMs(s.duration_ms)}${s.success ? "" : " — FAILED"}`}
          />
        );
      })}
    </div>
  );
}

export function TelemetryJobsSection({ refreshKey }: { refreshKey: number }) {
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [hours, setHours] = useState<number>(0);  // 0 = all-time

  const url = useMemo(() => {
    const params = new URLSearchParams();
    params.set("hours", String(hours));
    params.set("limit", "200");
    if (statusFilter !== "all") params.set("status", statusFilter);
    return `/api/telemetry/jobs?${params.toString()}`;
  }, [hours, statusFilter]);

  const cacheKey = `${CK.telemetryJobs(hours)}::${statusFilter}`;
  const { data, error: fetchError, refresh } = useStaleWhileRevalidate<JobsResponse>(
    cacheKey,
    () => api.get<JobsResponse>(url),
    30_000,
  );
  const error = fetchError
    ? fetchError instanceof ApiError
      ? `${fetchError.status}: ${fetchError.message}`
      : fetchError.message
    : null;
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [reLoadKey, setReLoadKey] = useState(0);

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, reLoadKey]);

  // Re-fetch on filter change (the cache key changes too).
  useEffect(() => { refresh(); /* eslint-disable-line */ }, [url]);

  const rows = useMemo(() => data?.jobs ?? [], [data]);
  const rowMaxMs = useMemo(
    () => rows.reduce((acc, r) => Math.max(acc, r.duration_ms ?? r.stage_total_ms), 1),
    [rows],
  );

  // Status counts for the filter chip badges.
  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of rows) {
      counts[r.status] = (counts[r.status] ?? 0) + 1;
    }
    counts.all = rows.length;
    return counts;
  }, [rows]);

  const toggle = (key: string) =>
    setExpanded((s) => {
      const next = new Set(s);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <section className="flex flex-col gap-3">
      <header className="flex flex-col gap-1.5">
        <h2 className="flex items-center gap-2 text-[14px] font-medium tracking-tight">
          <ListChecks className="h-4 w-4 text-muted-foreground" />
          Jobs (control-plane source of truth)
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Every job submitted, sourced from the Firestore <span className="font-mono text-[11px]">jobs</span> collection.
          Sees jobs the Renders tab can{"'"}t — including dispatch failures (gcloud /
          IAM / quota errors before the worker ever ran). Stage waterfall is
          synthesised from the job{"'"}s timeline so historical jobs render the same
          way as ones with structured Cloud Logging events.
        </p>
      </header>

      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-muted-foreground">window</span>
        <Select value={String(hours)} onValueChange={(v) => setHours(Number(v))}>
          <SelectTrigger className="h-7 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="1">last 1 h</SelectItem>
            <SelectItem value="24">last 24 h</SelectItem>
            <SelectItem value="168">last 7 d</SelectItem>
            <SelectItem value="720">last 30 d</SelectItem>
            <SelectItem value="0">all-time</SelectItem>
          </SelectContent>
        </Select>
        <span className="ml-2 font-mono text-[11px] text-muted-foreground">status</span>
        <div className="flex flex-wrap gap-1">
          {STATUS_ORDER.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => { setStatusFilter(s); setReLoadKey((k) => k + 1); }}
              className={cn(
                "rounded border px-2 py-0.5 text-[11px] font-mono transition",
                s === statusFilter
                  ? "border-sky-300 bg-sky-300/10 text-sky-200"
                  : "border-border text-muted-foreground hover:bg-surface/30",
              )}
            >
              {s}
              {statusCounts[s] != null ? (
                <span className="ml-1 text-muted-foreground/60">({statusCounts[s]})</span>
              ) : null}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load jobs" description={error} />
      ) : data?.error ? (
        <EmptyState icon={AlertOctagon} title="Firestore query failed" description={data.error} />
      ) : !data ? (
        <Skeleton className="h-64 w-full" />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={Package}
          title="No jobs in this window"
          description="Try widening the time range or removing the status filter."
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-left">
            <thead className="bg-surface/20">
              <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                <th className="px-3 py-2.5 font-medium" />
                <th className="px-3 py-2.5 font-medium">Channel · Topic / job_id</th>
                <th className="px-3 py-2.5 font-medium">Kind</th>
                <th className="px-3 py-2.5 font-medium" style={{ width: "32%" }}>
                  Duration · stage waterfall
                </th>
                <th className="px-3 py-2.5 text-right font-medium">Total</th>
                <th className="px-3 py-2.5 text-right font-medium">Status</th>
                <th className="px-3 py-2.5 text-right font-medium">Submitted</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const isExpanded = expanded.has(r.job_id);
                const total = r.duration_ms ?? r.stage_total_ms;
                const badge = STATUS_BADGE[r.status] ?? STATUS_BADGE.pending;
                const Icon = badge.icon;
                return (
                  <Fragment key={r.job_id}>
                    <tr
                      className="cursor-pointer border-t border-border hover:bg-surface/15"
                      onClick={() => toggle(r.job_id)}
                    >
                      <td className="px-3 py-2.5 align-middle text-muted-foreground">
                        {isExpanded ? (
                          <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5" />
                        )}
                      </td>
                      <td className="px-3 py-2.5 align-middle font-mono text-[12px] text-foreground">
                        <div>{r.channel ?? "?"}</div>
                        <div className="truncate text-[11px] text-muted-foreground" style={{ maxWidth: "26rem" }}>
                          {r.topic ?? r.job_id}
                        </div>
                      </td>
                      <td className="px-3 py-2.5 align-middle font-mono text-[11px] text-muted-foreground">
                        {r.render_kind ?? "—"}
                      </td>
                      <td className="px-3 py-2.5 align-middle">
                        <StageWaterfall
                          stages={r.stages}
                          totalMs={total}
                          rowMaxMs={rowMaxMs}
                        />
                      </td>
                      <td className="px-3 py-2.5 text-right align-middle font-mono text-[12px] text-foreground">
                        {formatMs(total)}
                      </td>
                      <td className="px-3 py-2.5 text-right align-middle">
                        <span
                          className={cn(
                            "inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px]",
                            badge.color,
                          )}
                        >
                          <Icon
                            className={cn(
                              "h-3 w-3",
                              (r.status === "rendering" || r.status === "uploading") && "animate-spin",
                            )}
                          />
                          {badge.label}
                        </span>
                      </td>
                      <td className="px-3 py-2.5 text-right align-middle font-mono text-[11px] text-muted-foreground">
                        {formatRelative(r.created_at)}
                      </td>
                    </tr>
                    {isExpanded ? (
                      <tr className="border-t border-border bg-surface/10">
                        <td />
                        <td colSpan={6} className="px-4 py-3">
                          {r.error ? (
                            <div className="mb-3 rounded border border-rose-300/30 bg-rose-300/5 px-3 py-2 font-mono text-[11px] text-rose-200">
                              <div className="mb-0.5 text-[10px] uppercase tracking-[0.16em] text-rose-300/70">
                                Error
                              </div>
                              <pre className="whitespace-pre-wrap text-rose-200">{r.error}</pre>
                            </div>
                          ) : null}
                          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 pb-3 font-mono text-[11px] text-muted-foreground">
                            <div>
                              <span className="text-muted-foreground/70">job_id:</span>{" "}
                              <span className="text-foreground">{r.job_id}</span>
                            </div>
                            <div>
                              <span className="text-muted-foreground/70">stage at finish:</span>{" "}
                              <span className="text-foreground">{r.stage ?? "—"}</span>
                            </div>
                            <div>
                              <span className="text-muted-foreground/70">submitted:</span>{" "}
                              <span className="text-foreground">{r.created_at ?? "—"}</span>
                            </div>
                            <div>
                              <span className="text-muted-foreground/70">last update:</span>{" "}
                              <span className="text-foreground">{r.updated_at ?? "—"}</span>
                            </div>
                            {r.cloud_execution ? (
                              <div className="col-span-2">
                                <span className="text-muted-foreground/70">cloud execution:</span>{" "}
                                <span className="text-foreground">{r.cloud_execution}</span>
                              </div>
                            ) : null}
                            {r.youtube_url ? (
                              <div className="col-span-2">
                                <span className="text-muted-foreground/70">YouTube:</span>{" "}
                                <a
                                  href={r.youtube_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-sky-300 hover:underline"
                                >
                                  {r.youtube_url}
                                </a>
                              </div>
                            ) : null}
                          </div>
                          {r.stages.length > 0 ? (
                            <div className="overflow-hidden rounded border border-border">
                              <table className="w-full text-left">
                                <thead className="bg-surface/15">
                                  <tr className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
                                    <th className="px-3 py-1.5 font-medium">Stage</th>
                                    <th className="px-3 py-1.5 text-right font-medium">Duration</th>
                                    <th className="px-3 py-1.5 text-right font-medium">% of render</th>
                                    <th className="px-3 py-1.5 text-right font-medium">Status</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {r.stages.map((s, i) => {
                                    const pct =
                                      total > 0 && s.duration_ms != null
                                        ? ((s.duration_ms / total) * 100).toFixed(1)
                                        : "—";
                                    return (
                                      <tr key={`${s.name}-${i}`} className="border-t border-border">
                                        <td className="px-3 py-1.5 font-mono text-[11.5px] text-foreground">
                                          <span
                                            className="mr-2 inline-block h-2 w-2 rounded-sm align-middle"
                                            style={{ backgroundColor: colorForStage(s) }}
                                          />
                                          {s.name}
                                        </td>
                                        <td className="px-3 py-1.5 text-right font-mono text-[11.5px] text-muted-foreground">
                                          {formatMs(s.duration_ms)}
                                        </td>
                                        <td className="px-3 py-1.5 text-right font-mono text-[11px] text-muted-foreground">
                                          {pct === "—" ? "—" : `${pct}%`}
                                        </td>
                                        <td
                                          className={cn(
                                            "px-3 py-1.5 text-right font-mono text-[11px]",
                                            s.success ? "text-emerald-300" : "text-rose-300",
                                          )}
                                        >
                                          {s.success ? "OK" : "FAILED"}
                                        </td>
                                      </tr>
                                    );
                                  })}
                                </tbody>
                              </table>
                            </div>
                          ) : (
                            <div className="rounded border border-border bg-surface/10 px-3 py-2 font-mono text-[11px] text-muted-foreground">
                              No stage timeline recorded for this job.{" "}
                              <Clock className="inline h-3 w-3" />{" "}
                              Most likely the worker failed during dispatch / bootstrap before
                              the first stage transition was written to Firestore.
                            </div>
                          )}
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
