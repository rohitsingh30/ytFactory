"use client";

import { useEffect } from "react";
import { AlertCircle, AlertOctagon } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";

interface ErrorRow {
  ts: number;
  event: string;
  category: string;
  duration_ms: number | null;
  job_id: string | null;
  metadata: Record<string, unknown>;
}

interface ErrorsResponse {
  hours: number;
  errors: ErrorRow[];
}

export function TelemetryErrorsSection({ refreshKey }: { refreshKey: number }) {
  const { data, error: fetchError, refresh } = useStaleWhileRevalidate<ErrorsResponse>(
    CK.telemetryErrors(24),
    () => api.get<ErrorsResponse>("/api/telemetry/errors?hours=24&limit=20"),
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
    <section className="flex flex-col gap-3">
      <header className="flex flex-col gap-1.5">
        <h2 className="flex items-center gap-2 text-[14px] font-medium tracking-tight">
          <AlertCircle className="h-4 w-4 text-muted-foreground" />
          Recent errors (last 24 h)
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Top 20 most recent failed events. Click into Cloud Logging from the
          {" "}<span className="font-mono text-[11px]">Links</span> section below for full stack traces.
        </p>
      </header>

      {error ? (
        <EmptyState icon={AlertOctagon} title="Failed to load errors" description={error} />
      ) : !data ? (
        <Skeleton className="h-32 w-full" />
      ) : data.errors.length === 0 ? (
        <EmptyState
          icon={AlertCircle}
          title="No errors in window"
          description="The pipeline has been clean for the last 24 hours."
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-left">
            <thead className="bg-surface/20">
              <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                <th className="px-4 py-2.5 font-medium">When</th>
                <th className="px-4 py-2.5 font-medium">Event</th>
                <th className="px-4 py-2.5 font-medium">Channel / Slug</th>
                <th className="px-4 py-2.5 font-medium">Reason</th>
              </tr>
            </thead>
            <tbody>
              {data.errors.map((e, i) => {
                const md = e.metadata ?? {};
                const ch = (md.channel as string) || (md.niche as string) || "—";
                const slug = (md.slug as string) || "";
                const errMsg = (md.error as string) || (md.fail_reason as string) || "—";
                return (
                  <tr key={i} className="border-t border-border align-top">
                    <td className="whitespace-nowrap px-4 py-2.5 font-mono text-[11px] text-muted-foreground">
                      {new Date(e.ts * 1000).toLocaleTimeString()}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-[12px] text-rose-300">
                      {e.event}
                      <div className="font-mono text-[10.5px] text-muted-foreground">
                        {e.category}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-[11.5px] text-muted-foreground">
                      {ch}
                      {slug && <span className="text-foreground/60"> / {slug}</span>}
                    </td>
                    <td className="px-4 py-2.5 text-[11.5px] text-foreground/80">
                      <code className="font-mono text-[11px]">{errMsg}</code>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
