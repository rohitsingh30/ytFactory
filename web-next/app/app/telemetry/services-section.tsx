"use client";

import { useEffect } from "react";
import { AlertOctagon, Server } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";
import { cn } from "@/lib/utils";

interface ServiceRow {
  service: string;
  count: number;
  failed: number;
  error_rate: number;
  p50_ms: number;
  p95_ms: number;
  last_seen_ts: number;
}

interface ServicesResponse {
  hours: number;
  services: ServiceRow[];
}

export function TelemetryServicesSection({ refreshKey }: { refreshKey: number }) {
  const { data, error: fetchError, refresh } = useStaleWhileRevalidate<ServicesResponse>(
    CK.telemetryServices(1),
    () => api.get<ServicesResponse>("/api/telemetry/services?hours=1"),
    30_000,
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
    <section className="flex flex-col gap-3">
      <header className="flex flex-col gap-1.5">
        <h2 className="flex items-center gap-2 text-[14px] font-medium tracking-tight">
          <Server className="h-4 w-4 text-muted-foreground" />
          Services (last hour)
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Per-provider rollup. <span className="font-mono text-[11px]">provider</span> from
          metadata when present, otherwise the event{"'"}s
          <span className="font-mono text-[11px]"> category</span>. Polls every 30 s.
        </p>
      </header>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load services" description={error} />
      ) : !data ? (
        <Skeleton className="h-48 w-full" />
      ) : data.services.length === 0 ? (
        <EmptyState icon={Server} title="No service activity" description="No events in the last hour." />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-left">
            <thead className="bg-surface/20">
              <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                <th className="px-4 py-2.5 font-medium">Service</th>
                <th className="px-4 py-2.5 text-right font-medium">Calls</th>
                <th className="px-4 py-2.5 text-right font-medium">Errors</th>
                <th className="px-4 py-2.5 text-right font-medium">Error %</th>
                <th className="px-4 py-2.5 text-right font-medium">p50</th>
                <th className="px-4 py-2.5 text-right font-medium">p95</th>
                <th className="px-4 py-2.5 text-right font-medium">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {data.services.map((s) => (
                <tr key={s.service} className="border-t border-border">
                  <td className="px-4 py-2.5 font-mono text-[12px] text-foreground">
                    {s.service}
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
                  <td
                    className={cn(
                      "px-4 py-2.5 text-right font-mono text-[12px]",
                      s.error_rate >= 5
                        ? "text-rose-300"
                        : s.error_rate >= 1
                        ? "text-amber-300"
                        : "text-emerald-300",
                    )}
                  >
                    {s.error_rate.toFixed(1)}%
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                    {s.p50_ms} ms
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-[12px] text-muted-foreground">
                    {s.p95_ms} ms
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-[11.5px] text-muted-foreground">
                    {s.last_seen_ts ? formatRelative(s.last_seen_ts) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function formatRelative(ts: number): string {
  const dt = Date.now() / 1000 - ts;
  if (dt < 60) return `${Math.round(dt)} s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)} m ago`;
  return `${Math.round(dt / 3600)} h ago`;
}
