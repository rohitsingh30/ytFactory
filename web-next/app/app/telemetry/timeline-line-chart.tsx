"use client";

// Lazy-loaded chart wrapper so recharts (~50 KB gz) is dropped from the
// /app/telemetry first-load JS bundle. Mounted by timeline-section.tsx
// via `next/dynamic`, with a Skeleton loading placeholder of the same
// height so the layout doesn't shift.

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface TimelineChartPoint {
  t: string;
  events: number;
  errors: number;
}

export function TimelineLineChart({ data }: { data: TimelineChartPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
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
  );
}
