"use client";

import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, AlertOctagon } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useVisiblePoll } from "@/lib/use-visible-poll";

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
  const [data, setData] = useState<TimelineResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const r = await api.get<TimelineResponse>(
        "/api/telemetry/timeline?hours=24",
      );
      setData(r);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  useEffect(() => {
    load();
  }, [refreshKey]);

  useVisiblePoll(load, 60_000, []);

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
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
                <CartesianGrid stroke="rgb(255 255 255 / 0.04)" vertical={false} />
                <XAxis
                  dataKey="t"
                  tick={{ fontSize: 10, fill: "rgb(160 160 170)" }}
                  tickLine={false}
                  axisLine={false}
                  interval="preserveStartEnd"
                  minTickGap={32}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: "rgb(160 160 170)" }}
                  tickLine={false}
                  axisLine={false}
                  width={32}
                />
                <Tooltip
                  contentStyle={{
                    background: "rgb(20 20 24)",
                    border: "1px solid rgb(255 255 255 / 0.08)",
                    fontSize: 11,
                  }}
                  labelStyle={{ color: "rgb(220 220 230)" }}
                />
                <Line
                  type="monotone"
                  dataKey="events"
                  stroke="#7dd3fc"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="errors"
                  stroke="#fb7185"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </section>
  );
}
