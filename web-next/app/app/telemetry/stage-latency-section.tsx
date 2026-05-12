"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo } from "react";
import { AlertOctagon, BarChart3 } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";
import { cn } from "@/lib/utils";

// Lazy-load the chart wrapper so recharts (~50 KB gz) lives in its own
// chunk. Skeleton placeholder height matches the rendered chart so the
// section doesn't shift on hydration.
const StageLatencyBarChart = dynamic(
  () => import("./stage-latency-chart").then((m) => m.StageLatencyBarChart),
  {
    ssr: false,
    loading: () => <Skeleton className="h-72 w-full" />,
  },
);

interface StageRow {
  stage: string;
  count: number;
  failed: number;
  mean_ms: number;
  p50_ms: number;
  p95_ms: number;
  max_ms: number;
  total_ms: number;
}

interface StageLatencyResponse {
  hours: number;
  stages: StageRow[];
}

function formatMs(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
}

export function TelemetryStageLatencySection({
  refreshKey,
}: {
  refreshKey: number;
}) {
  const { data, error: fetchError, refresh } = useStaleWhileRevalidate<StageLatencyResponse>(
    CK.telemetryStageLatency(24),
    () => api.get<StageLatencyResponse>("/api/telemetry/stage_latency?hours=24"),
    60_000,
  );
  const error = fetchError
    ? fetchError instanceof ApiError
      ? `${fetchError.status}: ${fetchError.message}`
      : fetchError.message
    : null;

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  // Cap to top-12 by p95 so the chart stays readable on narrow viewports;
  // the operator can drill via Cloud Trace for the long tail.
  const chartData = useMemo(
    () =>
      (data?.stages ?? []).slice(0, 12).map((s) => ({
        stage: s.stage,
        p50: s.p50_ms,
        p95: s.p95_ms,
        count: s.count,
        failed: s.failed,
      })),
    [data],
  );

  return (
    <section className="flex flex-col gap-3">
      <header className="flex flex-col gap-1.5">
        <h2 className="flex items-center gap-2 text-[14px] font-medium tracking-tight">
          <BarChart3 className="h-4 w-4 text-muted-foreground" />
          Stage latency (last 24 h)
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Per-stage p50 (lighter) and p95 (saturated) duration across every render.
          Sorted by p95 — the slowest stages are at the top. Violet bars are full render
          envelopes; sky bars are individual stages; rose means the stage failed at least
          once in the window.
        </p>
      </header>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load stage latency" description={error} />
      ) : !data ? (
        <Skeleton className="h-72 w-full" />
      ) : data.stages.length === 0 ? (
        <EmptyState
          icon={BarChart3}
          title="No render activity in the last 24 h"
          description="Trigger a render to populate the latency chart."
        />
      ) : (
        <>
          <div className="h-72 w-full rounded-lg border border-border bg-surface/10 p-2">
            <StageLatencyBarChart data={chartData} />
          </div>
          <div className="overflow-hidden rounded-lg border border-border">
            <table className="w-full text-left">
              <thead className="bg-surface/20">
                <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  <th className="px-4 py-2.5 font-medium">Stage</th>
                  <th className="px-4 py-2.5 text-right font-medium">Count</th>
                  <th className="px-4 py-2.5 text-right font-medium">Errors</th>
                  <th className="px-4 py-2.5 text-right font-medium">Mean</th>
                  <th className="px-4 py-2.5 text-right font-medium">p50</th>
                  <th className="px-4 py-2.5 text-right font-medium">p95</th>
                  <th className="px-4 py-2.5 text-right font-medium">Max</th>
                  <th className="px-4 py-2.5 text-right font-medium">Total</th>
                </tr>
              </thead>
              <tbody>
                {data.stages.map((s) => (
                  <tr key={s.stage} className="border-t border-border">
                    <td className="px-4 py-2.5 font-mono text-[12px] text-foreground">
                      {s.stage}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {s.count.toLocaleString()}
                    </td>
                    <td
                      className={cn(
                        "px-4 py-2.5 text-right font-mono text-[12px]",
                        s.failed > 0 ? "text-rose-300" : "text-muted-foreground/60",
                      )}
                    >
                      {s.failed.toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatMs(s.mean_ms)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatMs(s.p50_ms)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatMs(s.p95_ms)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatMs(s.max_ms)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatMs(s.total_ms)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
