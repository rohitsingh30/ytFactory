"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo } from "react";
import { Activity, AlertOctagon } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";

// Lazy-load the chart wrapper so recharts (~50 KB gz) is split out of
// the /app/telemetry first-load bundle. Skeleton has matching height
// to avoid layout shift while the chunk loads.
const TimelineLineChart = dynamic(
  () => import("./timeline-line-chart").then((m) => m.TimelineLineChart),
  {
    ssr: false,
    loading: () => <Skeleton className="h-56 w-full" />,
  },
);

interface TimelineBucket {
  bucket_start: number;
  count: number;
  errors: number;
}

interface TimelineResponse {
  hours: number;
  bucket_s: number;
  series: TimelineBucket[];
}

export function TelemetryTimelineSection({
  refreshKey,
}: {
  refreshKey: number;
}) {
  const { data, error: fetchError, refresh } = useStaleWhileRevalidate<TimelineResponse>(
    CK.telemetryTimeline(24),
    () => api.get<TimelineResponse>("/api/telemetry/timeline?hours=24"),
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

  const chartData = useMemo(
    () =>
      (data?.series ?? []).map((b) => ({
        t: new Date(b.bucket_start * 1000).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
        events: b.count,
        errors: b.errors,
      })),
    [data],
  );

  return (
    <section className="flex flex-col gap-3">
      <header className="flex flex-col gap-1.5">
        <h2 className="flex items-center gap-2 text-[14px] font-medium tracking-tight">
          <Activity className="h-4 w-4 text-muted-foreground" />
          Activity (last 24 h)
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Events per 15-min bucket. Errors overlaid in red. Refreshes every minute.
        </p>
      </header>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load timeline" description={error} />
      ) : !data ? (
        <Skeleton className="h-56 w-full" />
      ) : (
        <div className="rounded-lg border border-border bg-surface px-4 py-4">
          <div className="h-56">
            <TimelineLineChart data={chartData} />
          </div>
        </div>
      )}
    </section>
  );
}
