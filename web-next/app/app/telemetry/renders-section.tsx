"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { AlertOctagon, Clapperboard, ChevronDown, ChevronRight } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";
import { cn } from "@/lib/utils";

interface RenderStage {
  name: string;
  duration_ms: number | null;
  success: boolean;
  ts: number | null;
  provider: string | null;
}

interface RenderRow {
  channel: string;
  slug: string;
  render_kind: string;
  total_ms: number;
  stage_total_ms: number;
  stages: RenderStage[];
  success: boolean;
  started_at: number | null;
  ended_at: number | null;
  has_envelope: boolean;
}

interface RendersResponse {
  hours: number;
  renders: RenderRow[];
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
}

function formatRelative(ts: number | null): string {
  if (!ts) return "—";
  const dt = Date.now() / 1000 - ts;
  if (dt < 60) return `${Math.round(dt)} s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)} m ago`;
  if (dt < 86400) return `${Math.round(dt / 3600)} h ago`;
  return `${Math.round(dt / 86400)} d ago`;
}

// Per-stage palette — reused across all renders so the same stage
// gets the same colour everywhere on the page.
const STAGE_COLORS: Record<string, string> = {
  tts_synth: "#7dd3fc",        // sky
  image_gen: "#a78bfa",        // violet
  asr_transcribe: "#facc15",   // amber
  compose: "#34d399",          // emerald
  upload_short: "#f472b6",     // pink
  youtube_upload: "#f472b6",
  llm_call: "#fbbf24",         // gold
  cast_author: "#fbbf24",
  shotlist_author: "#fbbf24",
  rewrite: "#fbbf24",
  critic: "#fbbf24",
};
const DEFAULT_COLOR = "#94a3b8";   // slate
const FAIL_COLOR = "#fb7185";      // rose

function colorFor(stage: RenderStage): string {
  if (!stage.success) return FAIL_COLOR;
  if (STAGE_COLORS[stage.name]) return STAGE_COLORS[stage.name];
  // Unify all `stage.X` events under their bare names.
  const trimmed = stage.name.startsWith("stage.")
    ? stage.name.slice("stage.".length)
    : stage.name;
  return STAGE_COLORS[trimmed] ?? DEFAULT_COLOR;
}

/**
 * Horizontal stacked-bar showing where time went in this render.
 * Width is proportional to `total_ms` so two slow renders are
 * directly visually comparable.
 */
function StageWaterfall({
  stages,
  totalMs,
  rowMaxTotalMs,
}: {
  stages: RenderStage[];
  totalMs: number;
  rowMaxTotalMs: number;
}) {
  const widthPct = rowMaxTotalMs > 0 ? (totalMs / rowMaxTotalMs) * 100 : 0;
  // Filter out stages with no measurable duration; otherwise the
  // waterfall has invisible 0-width slivers that confuse the visual.
  const visibleStages = stages.filter(
    (s) => s.duration_ms != null && s.duration_ms > 0,
  );
  const stageTotal = visibleStages.reduce(
    (acc, s) => acc + (s.duration_ms ?? 0),
    0,
  );
  return (
    <div
      className="relative flex h-2.5 rounded-sm bg-surface/30"
      style={{ width: `${widthPct}%` }}
      title={`${formatMs(totalMs)} total`}
    >
      {visibleStages.map((s, i) => {
        const pct = stageTotal > 0 ? ((s.duration_ms ?? 0) / stageTotal) * 100 : 0;
        return (
          <div
            key={`${s.name}-${i}`}
            className="h-full"
            style={{ width: `${pct}%`, backgroundColor: colorFor(s) }}
            title={`${s.name}: ${formatMs(s.duration_ms)}${s.provider ? ` (${s.provider})` : ""}${s.success ? "" : " — FAILED"}`}
          />
        );
      })}
    </div>
  );
}

function StageLegend({ stages }: { stages: RenderStage[] }) {
  // Distinct stages preserving first-seen order.
  const seen = new Set<string>();
  const ordered: RenderStage[] = [];
  for (const s of stages) {
    if (s.duration_ms == null || s.duration_ms <= 0) continue;
    if (seen.has(s.name)) continue;
    seen.add(s.name);
    ordered.push(s);
  }
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5 pt-2 text-[11px] font-mono text-muted-foreground">
      {ordered.map((s) => {
        const stage = stages.find((x) => x.name === s.name)!;
        const total = stages
          .filter((x) => x.name === s.name)
          .reduce((acc, x) => acc + (x.duration_ms ?? 0), 0);
        return (
          <div key={s.name} className="inline-flex items-center gap-1.5">
            <span
              className="inline-block h-2 w-2 rounded-sm"
              style={{ backgroundColor: colorFor(stage) }}
            />
            <span className="text-foreground">{s.name}</span>
            <span className="text-muted-foreground/70">{formatMs(total)}</span>
          </div>
        );
      })}
    </div>
  );
}

export function TelemetryRendersSection({
  refreshKey,
}: {
  refreshKey: number;
}) {
  const { data, error: fetchError, refresh } = useStaleWhileRevalidate<RendersResponse>(
    CK.telemetryRenders(24),
    () => api.get<RendersResponse>("/api/telemetry/renders?hours=24&limit=30"),
    60_000,
  );
  const error = fetchError
    ? fetchError instanceof ApiError
      ? `${fetchError.status}: ${fetchError.message}`
      : fetchError.message
    : null;
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  // Common max so all the waterfalls share one scale — the bar for
  // the longest render fills the row, every other render's bar is
  // proportional. That's what makes the visual answer "which renders
  // are dragging?" at a glance.
  const rowMaxTotalMs = useMemo(
    () => (data?.renders ?? []).reduce((acc, r) => Math.max(acc, r.total_ms), 1),
    [data],
  );

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
          <Clapperboard className="h-4 w-4 text-muted-foreground" />
          Recent renders (last 24 h)
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          One row per render (channel + slug + kind). Bar width is wall-clock duration —
          longest render fills the row; the rest are proportional. Bar segments are stage
          colours: <span className="text-sky-300 font-mono">tts</span>,{" "}
          <span className="text-violet-300 font-mono">image</span>,{" "}
          <span className="text-emerald-300 font-mono">compose</span>,{" "}
          <span className="text-pink-300 font-mono">upload</span>,{" "}
          <span className="text-amber-300 font-mono">llm</span>. Click a row to expand the
          per-stage breakdown.
        </p>
      </header>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load renders" description={error} />
      ) : !data ? (
        <Skeleton className="h-64 w-full" />
      ) : data.renders.length === 0 ? (
        <EmptyState
          icon={Clapperboard}
          title="No renders in the last 24 h"
          description="Trigger a render to populate this view."
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-left">
            <thead className="bg-surface/20">
              <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                <th className="px-3 py-2.5 font-medium" />
                <th className="px-3 py-2.5 font-medium">Channel · Slug</th>
                <th className="px-3 py-2.5 font-medium">Kind</th>
                <th className="px-3 py-2.5 font-medium" style={{ width: "40%" }}>
                  Duration · stage breakdown
                </th>
                <th className="px-3 py-2.5 text-right font-medium">Total</th>
                <th className="px-3 py-2.5 text-right font-medium">Status</th>
                <th className="px-3 py-2.5 text-right font-medium">Started</th>
              </tr>
            </thead>
            <tbody>
              {data.renders.map((r) => {
                const key = `${r.channel}::${r.slug}::${r.started_at ?? "?"}`;
                const isExpanded = expanded.has(key);
                return (
                  <Fragment key={key}>
                    <tr
                      className="cursor-pointer border-t border-border hover:bg-surface/15"
                      onClick={() => toggle(key)}
                    >
                      <td className="px-3 py-2.5 align-middle text-muted-foreground">
                        {isExpanded ? (
                          <ChevronDown className="h-3.5 w-3.5" />
                        ) : (
                          <ChevronRight className="h-3.5 w-3.5" />
                        )}
                      </td>
                      <td className="px-3 py-2.5 align-middle font-mono text-[12px] text-foreground">
                        <div>{r.channel}</div>
                        <div className="text-[11px] text-muted-foreground">{r.slug}</div>
                      </td>
                      <td className="px-3 py-2.5 align-middle font-mono text-[11px] text-muted-foreground">
                        {r.render_kind}
                      </td>
                      <td className="px-3 py-2.5 align-middle">
                        <StageWaterfall
                          stages={r.stages}
                          totalMs={r.total_ms}
                          rowMaxTotalMs={rowMaxTotalMs}
                        />
                      </td>
                      <td className="px-3 py-2.5 text-right align-middle font-mono text-[12px] text-foreground">
                        {formatMs(r.total_ms)}
                      </td>
                      <td
                        className={cn(
                          "px-3 py-2.5 text-right align-middle font-mono text-[11px]",
                          r.success ? "text-emerald-300" : "text-rose-300",
                        )}
                      >
                        {r.success ? "OK" : "FAILED"}
                      </td>
                      <td className="px-3 py-2.5 text-right align-middle font-mono text-[11px] text-muted-foreground">
                        {formatRelative(r.started_at)}
                      </td>
                    </tr>
                    {isExpanded ? (
                      <tr className="border-t border-border bg-surface/10">
                        <td />
                        <td colSpan={6} className="px-4 py-3">
                          <StageLegend stages={r.stages} />
                          <div className="mt-3 overflow-hidden rounded border border-border">
                            <table className="w-full text-left">
                              <thead className="bg-surface/15">
                                <tr className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
                                  <th className="px-3 py-1.5 font-medium">Stage</th>
                                  <th className="px-3 py-1.5 font-medium">Provider</th>
                                  <th className="px-3 py-1.5 text-right font-medium">Duration</th>
                                  <th className="px-3 py-1.5 text-right font-medium">% of render</th>
                                  <th className="px-3 py-1.5 text-right font-medium">Status</th>
                                </tr>
                              </thead>
                              <tbody>
                                {r.stages.map((s, i) => {
                                  const pct =
                                    r.total_ms > 0 && s.duration_ms != null
                                      ? ((s.duration_ms / r.total_ms) * 100).toFixed(1)
                                      : "—";
                                  return (
                                    <tr
                                      key={`${s.name}-${s.ts ?? i}`}
                                      className="border-t border-border"
                                    >
                                      <td className="px-3 py-1.5 font-mono text-[11.5px] text-foreground">
                                        <span
                                          className="mr-2 inline-block h-2 w-2 rounded-sm align-middle"
                                          style={{ backgroundColor: colorFor(s) }}
                                        />
                                        {s.name}
                                      </td>
                                      <td className="px-3 py-1.5 font-mono text-[11px] text-muted-foreground">
                                        {s.provider ?? "—"}
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
                                          s.success
                                            ? "text-emerald-300"
                                            : "text-rose-300",
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
