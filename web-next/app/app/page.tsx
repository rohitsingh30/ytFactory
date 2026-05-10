"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import {
  ArrowRight,
  ArrowUpRight,
  CheckCircle2,
  ListChecks,
  Plus,
  Wand2,
} from "lucide-react";
import { api } from "@/lib/api";
import type { ChannelSummary, DashboardData, Job } from "@/lib/types";
import { relativeTime } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/app/page-header";
import { StatCard } from "@/components/app/stat-card";
import { ChannelIcon, channelLabel } from "@/components/app/channel-icon";
import { StatusPill } from "@/components/app/status-pill";
import { ChannelHeroCard } from "@/components/app/channel-hero-card";
import { EmptyState } from "@/components/app/empty-state";

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [channels, setChannels] = useState<ChannelSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      // Promise.allSettled — a single failure (e.g. /api/dashboard
      // 404 on first deploy) must NOT lock the whole page in
      // skeleton-forever. Render whatever resolved and surface the
      // error inline.
      const [d, c] = await Promise.allSettled([
        api.get<DashboardData>("/api/dashboard"),
        api.get<{ channels: ChannelSummary[] }>("/api/channels"),
      ]);
      if (cancelled) return;
      if (d.status === "fulfilled") {
        setData(d.value);
      } else {
        // Default-zero so the stat tiles render instead of spinning.
        // Only applied while we've never had a successful payload —
        // setData callback inspects current state to avoid clobbering
        // a previous good response with zeros on a transient failure.
        setData((prev) =>
          prev ?? { renders_7d: 0, uploads_7d: 0, queued: 0, held: 0 },
        );
      }
      if (c.status === "fulfilled") setChannels(c.value.channels);
      const errs = [d, c].filter((p) => p.status === "rejected") as PromiseRejectedResult[];
      setError(errs.length ? errs.map((p) => (p.reason as Error).message).join("; ") : null);
    }
    load();
    const id = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio"
        title="Dashboard"
        description="A quick read on what's queued, what shipped, and what needs attention."
        actions={
          <>
            <Button asChild variant="outline" size="sm">
              <Link href="/app/library">
                <ListChecks className="h-3.5 w-3.5" />
                Library
              </Link>
            </Button>
            <Button asChild size="sm">
              <Link href="/app/create">
                <Plus className="h-3.5 w-3.5" />
                New Short
              </Link>
            </Button>
          </>
        }
      />

      <div className="space-y-8 p-6 md:p-8">
        <section>
          <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
            {data ? (
              <>
                <StatCard label="Renders · 7d" value={data.renders_7d ?? 0} hint="completed renders" />
                <StatCard label="Uploads · 7d" value={data.uploads_7d ?? 0} hint="published to YouTube" />
                <StatCard label="In progress" value={data.queued ?? 0} hint="queued or rendering" />
                <StatCard label="Held" value={data.held ?? 0} hint="awaiting fix" />
              </>
            ) : (
              [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[120px] w-full rounded-xl" />)
            )}
          </div>
        </section>

        <section className="grid gap-3 lg:grid-cols-3">
          <div className="rounded-xl border border-border bg-surface lg:col-span-2">
            <div className="flex items-center justify-between border-b border-border px-5 py-3">
              <div className="text-[13px] font-medium tracking-tight">Recent renders</div>
              <Link
                href="/app/library"
                className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
              >
                View library <ArrowUpRight className="h-3 w-3" />
              </Link>
            </div>
            <RecentRenders rows={data?.recent_jobs ?? null} />
          </div>

          <CreateCTA />
        </section>

        <section>
          <div className="flex items-center justify-between">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
                Channels
              </div>
              <div className="mt-1 text-[15px] font-medium tracking-tight">All eight live</div>
            </div>
            <Button asChild variant="ghost" size="sm">
              <Link href="/app/channels">
                Manage <ArrowUpRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          </div>
          <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {channels
              ? channels.map((c, i) => (
                  <ChannelHeroCard
                    key={c.key}
                    channel={c}
                    href={`/app/channels/${c.key}`}
                    index={i}
                  />
                ))
              : [0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
                  <Skeleton key={i} className="h-[280px] w-full rounded-xl" />
                ))}
          </div>
        </section>

        {error && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
            Failed to refresh dashboard: {error}
          </div>
        )}
      </div>
    </div>
  );
}

function RecentRenders({ rows }: { rows: Job[] | null }) {
  if (rows === null) {
    return (
      <div className="space-y-2 p-3">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-12 w-full rounded-md" />
        ))}
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Wand2}
        title="No renders yet"
        description="Make your first Short to see it here."
        action={
          <Button asChild>
            <Link href="/app/create">
              Create a Short
              <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        }
        className="m-3"
      />
    );
  }
  return (
    <ul className="divide-y divide-border">
      {rows.map((j) => (
        <li key={j.job_id}>
          <Link
            href={`/app/render/${j.job_id}`}
            className="group flex items-center gap-3 px-5 py-3 transition-colors hover:bg-surface-2"
          >
            <ChannelIcon channel={j.channel ?? ""} size="sm" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-medium tracking-tight">
                {j.topic ?? "Untitled"}
              </div>
              <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                <span>{channelLabel(j.channel)}</span>
                <span>·</span>
                <span>{relativeTime(j.updated_at)}</span>
                {j.stage && (
                  <>
                    <span>·</span>
                    <span className="font-mono">{j.stage}</span>
                  </>
                )}
              </div>
            </div>
            <StatusPill
              status={j.status}
              pulse={j.status === "rendering" || j.status === "uploading"}
            />
            <ArrowUpRight className="h-3.5 w-3.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
          </Link>
        </li>
      ))}
    </ul>
  );
}

function CreateCTA() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="rounded-xl border border-border bg-surface p-5"
    >
      <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
        <Wand2 className="h-3 w-3" />
        Quick create
      </div>
      <div className="mt-4 text-[15px] font-medium leading-snug tracking-tight">
        Pick a channel, customize the knobs, hit render.
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        The whole pipeline runs unattended — script, voice, visuals, captions.
      </p>
      <div className="mt-5 flex items-center gap-2">
        <Button asChild size="sm">
          <Link href="/app/create">
            Create
            <ArrowRight className="h-3.5 w-3.5" />
          </Link>
        </Button>
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          ~30s in dev mode
        </span>
      </div>
      <ul className="mt-6 space-y-2 text-[11px] text-muted-foreground">
        <li className="flex items-center gap-1.5">
          <CheckCircle2 className="h-3 w-3 text-emerald-400" />
          8 channels ready
        </li>
        <li className="flex items-center gap-1.5">
          <CheckCircle2 className="h-3 w-3 text-emerald-400" />
          Per-channel customization knobs
        </li>
      </ul>
    </motion.div>
  );
}
