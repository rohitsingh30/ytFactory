"use client";

import Link from "next/link";
import {
  ArrowRight,
  ArrowUpRight,
  Plus,
  Wand2,
} from "lucide-react";
import { api } from "@/lib/api";
import type { ChannelSummary, DashboardData, Job } from "@/lib/types";
import { relativeTime } from "@/lib/utils";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/app/page-header";
import { StatCard } from "@/components/app/stat-card";
import { ChannelIcon, channelLabel } from "@/components/app/channel-icon";
import { StatusPill } from "@/components/app/status-pill";
import { ChannelHeroCard } from "@/components/app/channel-hero-card";
import { EmptyState } from "@/components/app/empty-state";

export default function DashboardPage() {
  // Stale-while-revalidate: paint the previous response from
  // sessionStorage instantly, then refresh in the background. Cuts the
  // perceived "everything is skeletons for 5+ seconds on every nav"
  // problem to a one-time pay on the first ever visit.
  //
  // 30 s poll cadence (was 10 s) — the dashboard is a snapshot, not a
  // live console. Cron jobs ship every few minutes; subsecond freshness
  // isn't worth the GCS waterfall it triggers on the backend.
  const { data, error: dashError } = useStaleWhileRevalidate<DashboardData>(
    "dashboard:summary",
    () => api.get<DashboardData>("/api/dashboard"),
    30_000,
  );
  const { data: channelsResp, error: channelsError } = useStaleWhileRevalidate<{
    channels: ChannelSummary[];
  }>(
    "dashboard:channels",
    () => api.get<{ channels: ChannelSummary[] }>("/api/channels"),
    30_000,
  );
  const channels = channelsResp?.channels ?? null;
  const error = [dashError, channelsError]
    .filter((e): e is Error => Boolean(e))
    .map((e) => e.message)
    .join("; ") || null;

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio"
        title="Dashboard"
        description="A quick read on what's queued, what shipped, and what needs attention."
        actions={
          <Button asChild size="sm">
            <Link href="/app/create">
              <Plus className="h-3.5 w-3.5" />
              New Short
            </Link>
          </Button>
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

        <section>
          <div className="rounded-xl border border-border bg-surface">
            <div className="flex items-center justify-between border-b border-border px-5 py-3">
              <div className="text-[13px] font-medium tracking-tight">Recent renders</div>
            </div>
            <RecentRenders rows={data?.recent_jobs ?? null} />
          </div>
        </section>

        <section>
          <div className="flex items-center justify-between">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
                Channels
              </div>
              <div className="mt-1 text-[15px] font-medium tracking-tight">
                {channels ? `${channels.length} live` : "Loading…"}
              </div>
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
              : [0, 1, 2, 3, 4, 5, 6].map((i) => (
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
              <div className="flex items-center gap-1.5 text-[13px] font-medium tracking-tight">
                {j.internal_only ? (
                  <span
                    className="shrink-0 rounded-sm bg-amber-500/15 px-1 py-0.5 font-mono text-[9px] uppercase tracking-wider text-amber-300"
                    title="Internal-only preflight / smoke-test render. Not auto-published."
                  >
                    preflight
                  </span>
                ) : null}
                <span className="truncate">{j.topic ?? "Untitled"}</span>
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
