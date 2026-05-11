"use client";

// Lazy-loaded chart wrapper so recharts (~50KB gz) is dropped from the
// /app/cloud first-load JS bundle. Mounted by cost-section.tsx via
// `next/dynamic`, with a Skeleton loading placeholder of the same
// height so the layout doesn't shift.

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface ChartDay {
  day: string;
  [service: string]: string | number;
}

const USD = (n: number | undefined | null) =>
  n == null
    ? "—"
    : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n);

export function CostBarChart({
  chart,
  services,
  palette,
}: {
  chart: ChartDay[];
  services: string[];
  palette: string[];
}) {
  return (
    <ResponsiveContainer>
      <BarChart data={chart} margin={{ left: 0, right: 4, top: 4, bottom: 0 }}>
        <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
        <XAxis
          dataKey="day"
          tick={{ fontSize: 10, fill: "rgba(255,255,255,0.5)" }}
          axisLine={false}
          tickLine={false}
          tickFormatter={(d) => (typeof d === "string" ? d.slice(5) : d)}
        />
        <YAxis
          tick={{ fontSize: 10, fill: "rgba(255,255,255,0.5)" }}
          axisLine={false}
          tickLine={false}
          width={44}
          tickFormatter={(v) => `$${v}`}
        />
        <Tooltip
          cursor={{ fill: "rgba(255,255,255,0.04)" }}
          contentStyle={{
            background: "rgba(15,15,17,0.95)",
            border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 8,
            fontSize: 11,
          }}
          formatter={(v: number, name: string) => [USD(v), name]}
        />
        {services.map((s, i) => (
          <Bar key={s} dataKey={s} stackId="cost" fill={palette[i % palette.length]} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
