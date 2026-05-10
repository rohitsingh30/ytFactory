"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, CircleDashed, CircleOff } from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import type {
  CloudServiceKind,
  HealthResponse,
  HealthRow,
  HealthStatus,
} from "@/lib/cloud-types";
import { useVisiblePoll } from "@/lib/use-visible-poll";
import { cn, relativeTime } from "@/lib/utils";

const STATUS_TONES: Record<HealthStatus, string> = {
  green: "text-emerald-400 bg-emerald-500/10 border-emerald-500/30",
  yellow: "text-amber-400 bg-amber-500/10 border-amber-500/30",
  red: "text-rose-400 bg-rose-500/10 border-rose-500/30",
  unconfigured: "text-muted-foreground bg-surface-2 border-border",
};

const STATUS_ICON: Record<HealthStatus, typeof CheckCircle2> = {
  green: CheckCircle2,
  yellow: AlertCircle,
  red: AlertCircle,
  unconfigured: CircleDashed,
};

const KIND_ORDER: CloudServiceKind[] = ["tts", "image", "video", "infra"];
const KIND_LABEL: Record<CloudServiceKind, string> = {
  tts: "TTS",
  image: "Image",
  video: "Video",
  infra: "Infra",
};

export function CloudHealthSection({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<CloudServiceKind | "all">("all");

  async function load() {
    try {
      const res = await api.get<HealthResponse>("/api/cloud/health");
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  useEffect(() => {
    load();
  }, [refreshKey]);

  useVisiblePoll(load, 30_000, []);

  const rowsByKind = useMemo(() => {
    if (!data) return null;
    const out: Record<CloudServiceKind, HealthRow[]> = {
      tts: [],
      image: [],
      video: [],
      infra: [],
    };
    for (const r of data.rows) out[r.kind].push(r);
    return out;
  }, [data]);

  return (
    <section className="flex flex-col gap-4">
      <header className="flex flex-col gap-1.5">
        <h2 className="text-[14px] font-medium tracking-tight">Health</h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Live <code className="font-mono text-[11px]">/readyz</code> probe of every Cloud Run service.
          Refreshed every 30 s while this tab is open.
        </p>
        {data?.summary && <SummaryStrip summary={data.summary} />}
      </header>

      <div className="flex flex-wrap gap-1.5">
        <FilterChip active={filter === "all"} onClick={() => setFilter("all")}>
          All
        </FilterChip>
        {KIND_ORDER.map((k) => (
          <FilterChip key={k} active={filter === k} onClick={() => setFilter(k)}>
            {KIND_LABEL[k]}
          </FilterChip>
        ))}
      </div>

      {error ? (
        <EmptyState icon={CircleOff} title="Failed to load health" description={error} />
      ) : data === null ? (
        <Skeleton className="h-48 w-full" />
      ) : (
        <div className="flex flex-col gap-6">
          {KIND_ORDER.map((k) => {
            if (filter !== "all" && filter !== k) return null;
            const rows = rowsByKind?.[k] ?? [];
            if (rows.length === 0) return null;
            return <KindGroup key={k} kind={k} rows={rows} />;
          })}
        </div>
      )}
    </section>
  );
}

function SummaryStrip({ summary }: { summary: Record<HealthStatus, number> }) {
  const order: HealthStatus[] = ["green", "yellow", "red", "unconfigured"];
  return (
    <div className="mt-1 flex flex-wrap gap-1.5">
      {order.map((s) => (
        <span
          key={s}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em]",
            STATUS_TONES[s],
          )}
        >
          <Dot status={s} />
          {s} · {summary[s] ?? 0}
        </span>
      ))}
    </div>
  );
}

function Dot({ status }: { status: HealthStatus }) {
  return (
    <span
      className={cn(
        "h-1.5 w-1.5 rounded-full",
        status === "green" && "bg-emerald-400",
        status === "yellow" && "bg-amber-400",
        status === "red" && "bg-rose-400",
        status === "unconfigured" && "bg-muted-foreground/60",
      )}
    />
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-md border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
        active
          ? "border-border-strong bg-surface-2 text-foreground"
          : "border-border bg-surface text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function KindGroup({ kind, rows }: { kind: CloudServiceKind; rows: HealthRow[] }) {
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="flex items-center justify-between border-b border-border bg-surface/40 px-4 py-2.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          {KIND_LABEL[kind]} · {rows.length}
        </span>
      </div>
      <table className="w-full text-left">
        <thead className="bg-surface/20">
          <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            <Th>Service</Th>
            <Th>Status</Th>
            <Th>Latency</Th>
            <Th>Warm</Th>
            <Th>Last check</Th>
            <Th>Detail</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const Icon = STATUS_ICON[r.status];
            return (
              <tr key={r.short} className="border-t border-border">
                <Td>
                  <div className="font-mono text-[12px] tracking-tight text-foreground">{r.short}</div>
                  <div
                    className="truncate font-mono text-[10.5px] text-muted-foreground"
                    title={r.name}
                  >
                    {r.name}
                  </div>
                </Td>
                <Td>
                  <span
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em]",
                      STATUS_TONES[r.status],
                    )}
                  >
                    <Icon className="h-3 w-3" />
                    {r.status}
                  </span>
                </Td>
                <Td className="font-mono text-[12px] text-muted-foreground">
                  {r.latency_ms != null ? `${Math.round(r.latency_ms)} ms` : "—"}
                </Td>
                <Td className="font-mono text-[12px] text-muted-foreground">
                  {r.warm_s != null ? `${r.warm_s.toFixed(1)} s` : "—"}
                </Td>
                <Td className="font-mono text-[11.5px] text-muted-foreground">
                  {r.checked_at
                    ? relativeTime(new Date(r.checked_at * 1000).toISOString())
                    : "—"}
                </Td>
                <Td className="text-[11.5px] text-muted-foreground">
                  {r.error ? (
                    <span className="text-rose-300">{r.error}</span>
                  ) : r.flags?.length ? (
                    r.flags.join(", ")
                  ) : r.cold_loaded ? (
                    "cold"
                  ) : r.model_repo ? (
                    <span className="font-mono text-[10.5px]" title={r.model_repo}>
                      {r.model_repo}
                    </span>
                  ) : (
                    "—"
                  )}
                </Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return <th className={cn("px-4 py-2.5 font-medium", className)}>{children}</th>;
}
function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-4 py-3 align-top", className)}>{children}</td>;
}
