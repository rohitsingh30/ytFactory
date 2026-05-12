"use client";

import { useEffect } from "react";
import { AlertOctagon, Coins } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";
import { cn } from "@/lib/utils";

interface CostsRow {
  calls: number;
  failed: number;
  input_tokens: number;
  output_tokens: number;
  mean_latency_ms: number;
  p95_latency_ms: number;
}

interface CostsResponse {
  hours: number;
  totals: CostsRow;
  by_tier: Record<string, CostsRow>;
  by_backend: Record<string, CostsRow>;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)} M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} k`;
  return n.toLocaleString();
}

function formatMs(ms: number): string {
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(2)} s`;
  return `${Math.round(ms)} ms`;
}

const BACKEND_NOTE: Record<string, { color: string; note: string }> = {
  cli:           { color: "text-emerald-300", note: "free (Pro/Max OAuth)" },
  azure_openai:  { color: "text-amber-300",   note: "paid (Azure deployment)" },
  anthropic_sdk: { color: "text-rose-300",    note: "paid (separate billing line)" },
  unknown:       { color: "text-slate-400",   note: "(no backend metadata)" },
};

function CostTable({
  title,
  description,
  rows,
}: {
  title: string;
  description: string;
  rows: Record<string, CostsRow>;
}) {
  const entries = Object.entries(rows).sort(
    ([, a], [, b]) => b.calls - a.calls,
  );
  return (
    <div className="flex flex-col gap-2">
      <div>
        <h3 className="text-[12.5px] font-medium tracking-tight text-foreground">{title}</h3>
        <p className="text-[11.5px] text-muted-foreground">{description}</p>
      </div>
      {entries.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center font-mono text-[11.5px] text-muted-foreground">
          No data in window.
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-left">
            <thead className="bg-surface/20">
              <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                <th className="px-4 py-2.5 font-medium">Key</th>
                <th className="px-4 py-2.5 text-right font-medium">Calls</th>
                <th className="px-4 py-2.5 text-right font-medium">Errors</th>
                <th className="px-4 py-2.5 text-right font-medium">Input tok</th>
                <th className="px-4 py-2.5 text-right font-medium">Output tok</th>
                <th className="px-4 py-2.5 text-right font-medium">Mean lat</th>
                <th className="px-4 py-2.5 text-right font-medium">p95 lat</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([k, row]) => {
                const note = BACKEND_NOTE[k];
                return (
                  <tr key={k} className="border-t border-border">
                    <td className="px-4 py-2.5 font-mono text-[12px] text-foreground">
                      <div>{k}</div>
                      {note ? (
                        <div className={cn("text-[10px]", note.color)}>{note.note}</div>
                      ) : null}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {row.calls.toLocaleString()}
                    </td>
                    <td
                      className={cn(
                        "px-4 py-2.5 text-right font-mono text-[12px]",
                        row.failed > 0 ? "text-rose-300" : "text-muted-foreground/60",
                      )}
                    >
                      {row.failed.toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatTokens(row.input_tokens)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatTokens(row.output_tokens)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatMs(row.mean_latency_ms)}
                    </td>
                    <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                      {formatMs(row.p95_latency_ms)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function TelemetryLLMCostsSection({ refreshKey }: { refreshKey: number }) {
  const { data, error: fetchError, refresh } = useStaleWhileRevalidate<CostsResponse>(
    CK.telemetryLLMCosts(24),
    () => api.get<CostsResponse>("/api/telemetry/llm_costs?hours=24"),
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

  return (
    <section className="flex flex-col gap-5">
      <header className="flex flex-col gap-1.5">
        <h2 className="flex items-center gap-2 text-[14px] font-medium tracking-tight">
          <Coins className="h-4 w-4 text-muted-foreground" />
          LLM cost & usage (last 24 h)
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Aggregated from <span className="font-mono text-[11px]">llm_call</span> events.
          Tracks calls, failures, input + output tokens, and per-call latency grouped by
          tier (small / medium / large) and backend (cli / azure_openai / anthropic_sdk).
          The backend split is the early-warning signal for cost regressions: a sudden spike
          in <span className="font-mono text-[11px]">azure_openai</span> or{" "}
          <span className="font-mono text-[11px]">anthropic_sdk</span> usage means the
          dispatcher is routing to a paid SDK instead of the free CLI.
        </p>
      </header>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load LLM costs" description={error} />
      ) : !data ? (
        <Skeleton className="h-72 w-full" />
      ) : data.totals.calls === 0 ? (
        <EmptyState
          icon={Coins}
          title="No LLM activity in the last 24 h"
          description="The dispatcher is idle, or telemetry isn't reaching the dashboard yet."
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <SummaryCard label="Total calls" value={data.totals.calls.toLocaleString()} />
            <SummaryCard
              label="Failures"
              value={data.totals.failed.toLocaleString()}
              tone={data.totals.failed > 0 ? "danger" : "muted"}
            />
            <SummaryCard label="Input tokens" value={formatTokens(data.totals.input_tokens)} />
            <SummaryCard label="Output tokens" value={formatTokens(data.totals.output_tokens)} />
          </div>
          <CostTable
            title="By tier"
            description="Where do tokens go (small = critic / quick rewrites, large = full-script generation)."
            rows={data.by_tier}
          />
          <CostTable
            title="By backend"
            description="Free CLI vs paid SDKs. Paid spend should be near-zero outside of cloud render-worker fallback."
            rows={data.by_backend}
          />
        </>
      )}
    </section>
  );
}

function SummaryCard({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "default" | "muted" | "danger";
}) {
  const colorClass = tone === "danger"
    ? "text-rose-300"
    : tone === "muted"
    ? "text-muted-foreground/60"
    : "text-foreground";
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border border-border bg-surface/10 px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </div>
      <div className={cn("font-mono text-[18px]", colorClass)}>{value}</div>
    </div>
  );
}
