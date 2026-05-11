"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BadgeDollarSign,
  CalendarDays,
  FileWarning,
  Info,
} from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import type { CostPoint, CostResponse } from "@/lib/cloud-types";
import { cn } from "@/lib/utils";

// Lazy-load the chart wrapper so the recharts dep (~50 KB gz) is split
// out of the /app/cloud first-load bundle. Skeleton loading state has
// the same height as the chart so there's no layout shift.
const CostBarChart = dynamic(
  () => import("./cost-bar-chart").then((m) => m.CostBarChart),
  {
    ssr: false,
    loading: () => <Skeleton className="h-64 w-full" />,
  },
);

const PALETTE = [
  "#a78bfa",
  "#7dd3fc",
  "#fbbf24",
  "#34d399",
  "#f472b6",
  "#60a5fa",
  "#fb7185",
  "#facc15",
  "#22d3ee",
  "#a3e635",
  "#c084fc",
  "#f87171",
  "#94a3b8",
  "#fda4af",
  "#86efac",
];

const USD = (n: number | undefined | null) =>
  n == null
    ? "—"
    : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n);

interface ChartDay {
  day: string;
  [service: string]: string | number;
}

export function CloudCostSection({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<CostResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const res = await api.get<CostResponse>("/api/cloud/cost?days=30");
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  useEffect(() => {
    load();
  }, [refreshKey]);

  const { chart, services } = useMemo(() => {
    if (!data?.points || !data.range_start || !data.range_end) {
      return { chart: [] as ChartDay[], services: [] as string[] };
    }
    const days: string[] = [];
    const start = new Date(data.range_start + "T00:00:00Z");
    const end = new Date(data.range_end + "T00:00:00Z");
    for (let d = new Date(start); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
      days.push(d.toISOString().slice(0, 10));
    }
    const mtd = data.per_service_mtd ?? {};
    const services = Object.keys(mtd).sort((a, b) => (mtd[b] ?? 0) - (mtd[a] ?? 0));
    const byDay = new Map<string, ChartDay>();
    days.forEach((day) => {
      const row: ChartDay = { day };
      services.forEach((s) => (row[s] = 0));
      byDay.set(day, row);
    });
    for (const p of data.points as CostPoint[]) {
      const row = byDay.get(p.day);
      if (row && services.includes(p.service_short)) {
        row[p.service_short] = ((row[p.service_short] as number) ?? 0) + p.cost_usd;
      }
    }
    return { chart: Array.from(byDay.values()), services };
  }, [data]);

  return (
    <section className="flex flex-col gap-4">
      <header className="flex flex-col gap-1.5">
        <h2 className="text-[14px] font-medium tracking-tight">Cost</h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Daily Cloud Run spend by service from the BigQuery billing export. Last 30 days.
          Snapshot refreshed daily by{" "}
          <code className="font-mono text-[11px]">com.ytfactory.cloud-snapshot</code>.
        </p>
      </header>

      {error ? (
        <EmptyState icon={FileWarning} title="Failed to load cost" description={error} />
      ) : data === null ? (
        <Skeleton className="h-72 w-full" />
      ) : !data.available ? (
        <EmptyState
          icon={Info}
          title="Cost data not yet wired"
          description={
            (data.reason ?? "") + " — see docs/cloudrun_admin_panel.md for the one-time setup."
          }
        />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Stat
              label="Today"
              value={USD(data.total_today)}
              hint={`${(data.range_end ?? "").slice(5)} (UTC)`}
              icon={BadgeDollarSign}
            />
            <Stat
              label="MTD"
              value={USD(data.total_mtd)}
              hint={`Since ${(data.range_end ?? "").slice(0, 7)}-01`}
              icon={CalendarDays}
            />
            <Stat
              label="Drift flags"
              value={Object.keys(data.drift_flags ?? {}).length || "0"}
              hint={
                Object.keys(data.drift_flags ?? {}).length
                  ? "Today > 2× 30-day median"
                  : "Nothing flagged"
              }
              icon={AlertTriangle}
              tone={Object.keys(data.drift_flags ?? {}).length ? "warn" : "neutral"}
            />
          </div>

          <div className="rounded-lg border border-border bg-surface p-3">
            <div className="mb-3 flex items-center justify-between px-1">
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                Last 30 days · stacked $/day
              </span>
              <span className="font-mono text-[10px] text-muted-foreground">
                {data.range_start} → {data.range_end}
              </span>
            </div>
            <div className="h-64 w-full">
              <CostBarChart chart={chart} services={services} palette={PALETTE} />
            </div>
          </div>

          <div className="overflow-hidden rounded-lg border border-border">
            <table className="w-full text-left">
              <thead className="bg-surface/40">
                <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  <Th>Service</Th>
                  <Th className="text-right">Today</Th>
                  <Th className="text-right">MTD</Th>
                  <Th className="text-right">30-d median</Th>
                  <Th>Flag</Th>
                </tr>
              </thead>
              <tbody>
                {services.map((s, i) => (
                  <tr key={s} className="border-t border-border">
                    <Td>
                      <div className="flex items-center gap-2">
                        <span
                          className="h-2 w-2 rounded-sm"
                          style={{ background: PALETTE[i % PALETTE.length] }}
                        />
                        <span className="font-mono text-[12px]">{s}</span>
                      </div>
                    </Td>
                    <Td className="text-right font-mono text-[12px] text-muted-foreground">
                      {USD(data.per_service_today?.[s])}
                    </Td>
                    <Td className="text-right font-mono text-[12px] text-muted-foreground">
                      {USD(data.per_service_mtd?.[s])}
                    </Td>
                    <Td className="text-right font-mono text-[12px] text-muted-foreground">
                      {USD(data.per_service_30d_median?.[s])}
                    </Td>
                    <Td className="text-[11.5px] text-amber-300">
                      {data.drift_flags?.[s] ?? <span className="text-muted-foreground">—</span>}
                    </Td>
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

function Stat({
  label,
  value,
  hint,
  icon: Icon,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  hint?: string;
  icon: typeof BadgeDollarSign;
  tone?: "neutral" | "warn";
}) {
  return (
    <div
      className={cn(
        "rounded-xl border bg-surface p-5",
        tone === "warn" ? "border-amber-500/30" : "border-border",
      )}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          {label}
        </span>
        <Icon
          className={cn("h-3.5 w-3.5", tone === "warn" ? "text-amber-400" : "text-muted-foreground")}
        />
      </div>
      <div className="mt-3 text-[22px] font-medium tracking-tight">{value}</div>
      {hint && <div className="mt-1 text-[12px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return <th className={cn("px-4 py-2.5 font-medium", className)}>{children}</th>;
}
function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-4 py-3 align-middle", className)}>{children}</td>;
}
