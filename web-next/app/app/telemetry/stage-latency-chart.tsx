"use client";

// Lazy-loaded chart wrapper for the per-stage latency bar chart.
// recharts (~50 KB gz) is dynamic-imported via the parent section so
// it lands in its own bundle chunk and doesn't bloat the first paint
// of /app/telemetry.

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface StageLatencyBar {
  stage: string;
  p50: number;
  p95: number;
  count: number;
  failed: number;
}

const RENDER_FILL = "#a78bfa";          // violet — render envelopes
const STAGE_FILL = "#7dd3fc";           // sky — generic stage events
const ERROR_FILL = "#fb7185";           // rose — when failed > 0

function colorFor(row: StageLatencyBar): string {
  if (row.failed > 0) return ERROR_FILL;
  if (row.stage.startsWith("render.")) return RENDER_FILL;
  return STAGE_FILL;
}

function formatMs(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} min`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} s`;
  return `${ms} ms`;
}

export function StageLatencyBarChart({ data }: { data: StageLatencyBar[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart
        data={data}
        layout="vertical"
        margin={{ top: 8, right: 24, bottom: 4, left: 8 }}
        barCategoryGap="22%"
      >
        <CartesianGrid stroke="rgb(255 255 255 / 0.04)" horizontal={false} />
        <XAxis
          type="number"
          tick={{ fontSize: 10, fill: "rgb(160 160 170)" }}
          tickLine={false}
          axisLine={false}
          tickFormatter={formatMs}
        />
        <YAxis
          dataKey="stage"
          type="category"
          tick={{ fontSize: 10, fill: "rgb(200 200 210)", fontFamily: "monospace" }}
          tickLine={false}
          axisLine={false}
          width={170}
          interval={0}
        />
        <Tooltip
          contentStyle={{
            background: "rgb(20 20 24)",
            border: "1px solid rgb(255 255 255 / 0.08)",
            fontSize: 11,
          }}
          labelStyle={{ color: "rgb(220 220 230)", fontFamily: "monospace" }}
          formatter={(v: number, name: string) => [
            formatMs(v),
            name === "p50" ? "p50 latency" : "p95 latency",
          ]}
        />
        <Legend
          wrapperStyle={{ fontSize: 11, color: "rgb(160 160 170)" }}
          iconSize={8}
        />
        <Bar dataKey="p50" fill={STAGE_FILL} fillOpacity={0.55}>
          {data.map((d) => (
            <Cell
              key={`p50-${d.stage}`}
              fill={colorFor(d)}
              fillOpacity={0.55}
            />
          ))}
        </Bar>
        <Bar dataKey="p95">
          {data.map((d) => (
            <Cell key={`p95-${d.stage}`} fill={colorFor(d)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
