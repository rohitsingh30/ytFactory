"use client";

import { useEffect, useState } from "react";
import { Activity, AlertOctagon, CheckCircle2, Clock, Layers } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useVisiblePoll } from "@/lib/use-visible-poll";
import { cn } from "@/lib/utils";

interface OverviewResponse {
  hours: number;
  total_events: number;
  successes: number;
  failures: number;
  success_rate: number | null;
  p50_ms: number;
  p95_ms: number;
  by_category: Record<string, { total: number; success: number; failed: number }>;
  by_channel: Record<string, { total: number; success: number; failed: number }>;
  exporter: string;
}

interface InitStatus {
  initialised: boolean;
  exporter: string;
  gcp_project: string | null;
  has_gcp_exporter: boolean;
}

const HOURS_OPTIONS = [
  { value: 1, label: "1 h" },
  { value: 24, label: "24 h" },
  { value: 168, label: "7 d" },
];

export function TelemetryOverviewSection({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [status, setStatus] = useState<InitStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hours, setHours] = useState<number>(24);

  async function load() {
    try {
      const [ov, st] = await Promise.all([
        api.get<OverviewResponse>(`/api/telemetry/overview?hours=${hours}`),
        api.get<InitStatus>("/api/telemetry/init_status"),
      ]);
      setData(ov);
      setStatus(st);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  useEffect(() => {
    load();
  }, [refreshKey, hours]);

  useVisiblePoll(load, 30_000, [hours]);

  return (
    <section className="flex flex-col gap-4">
      <header className="flex flex-col gap-2.5">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="text-[14px] font-medium tracking-tight">Overview</h2>
          <HourPicker value={hours} onChange={setHours} />
        </div>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Coarse totals + success rate. Sourced from the active OTel exporter
          (<code className="font-mono text-[11px]">{status?.exporter ?? "…"}</code>).
          Polls every 30 s while this tab is open.
        </p>
        {status && !status.has_gcp_exporter && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11.5px] text-amber-300/90">
            Exporter is <code className="font-mono">{status.exporter}</code>.
            Configure <code className="font-mono">OTEL_EXPORTER=gcp</code> +{" "}
            <code className="font-mono">GOOGLE_CLOUD_PROJECT</code> to push spans
            to Cloud Trace and unlock the deep-link buttons.
          </div>
        )}
      </header>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load overview" description={error} />
      ) : !data ? (
        <Skeleton className="h-32 w-full" />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Stat
              icon={Activity}
              label="Events"
              value={data.total_events.toLocaleString()}
              tone="neutral"
            />
            <Stat
              icon={CheckCircle2}
              label="Success rate"
              value={data.success_rate == null ? "—" : `${data.success_rate}%`}
              tone={
                data.success_rate == null
                  ? "neutral"
                  : data.success_rate >= 95
                  ? "good"
                  : data.success_rate >= 80
                  ? "warn"
                  : "bad"
              }
            />
            <Stat
              icon={AlertOctagon}
              label="Failures"
              value={data.failures.toLocaleString()}
              tone={data.failures > 0 ? "bad" : "good"}
            />
            <Stat
              icon={Clock}
              label="p50 latency"
              value={`${data.p50_ms} ms`}
              tone="neutral"
            />
            <Stat
              icon={Clock}
              label="p95 latency"
              value={`${data.p95_ms} ms`}
              tone="neutral"
            />
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <CountTable
              title="By category"
              icon={Layers}
              rows={data.by_category}
            />
            <CountTable
              title="By channel"
              icon={Layers}
              rows={data.by_channel}
            />
          </div>
        </>
      )}
    </section>
  );
}

function HourPicker({
  value,
  onChange,
}: {
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="flex gap-1.5">
      {HOURS_OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={cn(
            "rounded-md border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
            value === opt.value
              ? "border-border-strong bg-surface-2 text-foreground"
              : "border-border bg-surface text-muted-foreground hover:text-foreground",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function Stat({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: typeof Activity;
  label: string;
  value: string;
  tone: "good" | "bad" | "warn" | "neutral";
}) {
  const toneCls =
    tone === "good"
      ? "text-emerald-300"
      : tone === "warn"
      ? "text-amber-300"
      : tone === "bad"
      ? "text-rose-300"
      : "text-foreground";
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="flex items-center gap-2 text-[10.5px] uppercase tracking-[0.18em] text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className={cn("mt-1.5 font-mono text-[18px] font-medium", toneCls)}>
        {value}
      </div>
    </div>
  );
}

function CountTable({
  title,
  icon: Icon,
  rows,
}: {
  title: string;
  icon: typeof Activity;
  rows: Record<string, { total: number; success: number; failed: number }>;
}) {
  const entries = Object.entries(rows).sort((a, b) => b[1].total - a[1].total);
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="flex items-center justify-between border-b border-border bg-surface/40 px-4 py-2.5">
        <span className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          <Icon className="h-3.5 w-3.5" />
          {title}
        </span>
        <span className="font-mono text-[10px] text-muted-foreground">
          {entries.length} rows
        </span>
      </div>
      {entries.length === 0 ? (
        <div className="px-4 py-6 text-center text-[12px] text-muted-foreground">
          No events in window
        </div>
      ) : (
        <table className="w-full text-left">
          <thead className="bg-surface/20">
            <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
              <th className="px-4 py-2.5 font-medium">Name</th>
              <th className="px-4 py-2.5 text-right font-medium">Total</th>
              <th className="px-4 py-2.5 text-right font-medium">OK</th>
              <th className="px-4 py-2.5 text-right font-medium">Failed</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([k, v]) => (
              <tr key={k} className="border-t border-border">
                <td className="px-4 py-2.5 font-mono text-[12px] text-foreground">{k}</td>
                <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                  {v.total.toLocaleString()}
                </td>
                <td className="px-4 py-2.5 text-right font-mono text-[12px] text-emerald-300">
                  {v.success.toLocaleString()}
                </td>
                <td
                  className={cn(
                    "px-4 py-2.5 text-right font-mono text-[12px]",
                    v.failed > 0 ? "text-rose-300" : "text-muted-foreground/60",
                  )}
                >
                  {v.failed.toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
