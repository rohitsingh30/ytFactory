"use client";

import { useEffect, useState } from "react";
import {
  ExternalLink,
  Eye,
  Gauge,
  GitBranch,
  ScrollText,
} from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

interface LinksResponse {
  available: boolean;
  reason?: string;
  project?: string;
  links?: {
    trace: string;
    logging: string;
    monitoring: string;
  };
}

const TARGETS = [
  {
    key: "trace" as const,
    label: "Cloud Trace",
    desc: "Per-request span waterfalls — trace a chat request end-to-end.",
    icon: GitBranch,
    color: "text-violet-300",
  },
  {
    key: "logging" as const,
    label: "Cloud Logging",
    desc: "Search structured logs by ytfactory.channel / slug / job_id.",
    icon: ScrollText,
    color: "text-sky-300",
  },
  {
    key: "monitoring" as const,
    label: "Cloud Monitoring",
    desc: "p50 / p95 / error-rate over time, alert policies, SLOs.",
    icon: Gauge,
    color: "text-emerald-300",
  },
];

export function TelemetryLinksSection({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<LinksResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [channel, setChannel] = useState("");
  const [slug, setSlug] = useState("");

  async function load() {
    const qp = new URLSearchParams();
    if (channel.trim()) qp.set("channel", channel.trim());
    if (slug.trim()) qp.set("slug", slug.trim());
    const url = "/api/telemetry/links" + (qp.toString() ? `?${qp}` : "");
    try {
      const r = await api.get<LinksResponse>(url);
      setData(r);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  useEffect(() => {
    load();
  }, [refreshKey, channel, slug]);

  return (
    <section className="flex flex-col gap-3">
      <header className="flex flex-col gap-1.5">
        <h2 className="flex items-center gap-2 text-[14px] font-medium tracking-tight">
          <Eye className="h-4 w-4 text-muted-foreground" />
          Open in GCP
        </h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Deep-link straight into the GCP consoles, pre-filtered to your project
          and (optionally) a channel + slug. The full power of Cloud Trace /
          Logging / Monitoring without re-implementing it.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <FilterInput label="channel" value={channel} onChange={setChannel} />
        <FilterInput label="slug" value={slug} onChange={setSlug} />
      </div>

      {error ? (
        <EmptyState icon={ExternalLink} title="Failed to load links" description={error} />
      ) : !data ? (
        <Skeleton className="h-32 w-full" />
      ) : !data.available ? (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-3 text-[12px] text-amber-300/90">
          Deep-links unavailable: {data.reason ?? "unknown"}.
          Set <code className="font-mono">GOOGLE_CLOUD_PROJECT</code> on the
          server to enable.
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          {TARGETS.map((t) => {
            const url = data.links?.[t.key];
            return (
              <a
                key={t.key}
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex flex-col gap-2 rounded-lg border border-border bg-surface px-4 py-3 transition-colors hover:border-border-strong hover:bg-surface-2"
              >
                <div className="flex items-center justify-between">
                  <span className={cn("flex items-center gap-2 text-[13px] font-medium tracking-tight", t.color)}>
                    <t.icon className="h-4 w-4" />
                    {t.label}
                  </span>
                  <ExternalLink className="h-3.5 w-3.5 text-muted-foreground group-hover:text-foreground" />
                </div>
                <p className="text-[11.5px] leading-relaxed text-muted-foreground">
                  {t.desc}
                </p>
              </a>
            );
          })}
        </div>
      )}

      {data?.project && (
        <div className="font-mono text-[10.5px] text-muted-foreground">
          project = <span className="text-foreground/80">{data.project}</span>
        </div>
      )}
    </section>
  );
}

function FilterInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 focus-within:border-border-strong">
      <span className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="(any)"
        className="w-full bg-transparent font-mono text-[12px] text-foreground placeholder:text-muted-foreground/40 focus:outline-none"
      />
    </label>
  );
}
