"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  ArrowRight,
  Filter,
  Plus,
  RefreshCw,
  Search,
  Video,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { PageHeader } from "@/components/app/page-header";
import { ChannelIcon, channelLabel } from "@/components/app/channel-icon";
import { StatusPill } from "@/components/app/status-pill";
import { EmptyState } from "@/components/app/empty-state";
import { channelsApi, jobsApi } from "@/lib/api";
import type { ChannelSummary, Job } from "@/lib/types";
import { relativeTime } from "@/lib/utils";

const STATUS_OPTIONS = [
  { value: "all", label: "All statuses" },
  { value: "pending", label: "Queued" },
  { value: "rendering", label: "Rendering" },
  { value: "uploading", label: "Uploading" },
  { value: "done", label: "Done" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

export default function LibraryPage() {
  // Initial filter state honors URL params so other surfaces can deep-link
  // here. Concretely: the Queue page's "Held by critic" card links to
  // /app/library?channel=<channel>&q=<slug> so a held render is one click
  // away from being found in the library grid.
  const params = useSearchParams();
  const initialChannel = params.get("channel") ?? "all";
  const initialQuery = params.get("q") ?? "";
  const initialStatus = params.get("status") ?? "all";

  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [channels, setChannels] = useState<ChannelSummary[] | null>(null);
  const [search, setSearch] = useState(initialQuery);
  const [chFilter, setChFilter] = useState<string>(initialChannel);
  const [statusFilter, setStatusFilter] = useState<string>(initialStatus);
  const [refreshing, setRefreshing] = useState(false);

  async function refresh() {
    setRefreshing(true);
    try {
      const [j, c] = await Promise.all([
        jobsApi.list({
          channel: chFilter === "all" ? undefined : chFilter,
          status: statusFilter === "all" ? undefined : statusFilter,
          limit: 100,
        }),
        channels === null ? channelsApi.list() : Promise.resolve({ channels: channels }),
      ]);
      setJobs(j.jobs);
      if (channels === null) setChannels(c.channels);
    } catch (e) {
      // Transient backend hiccup (server restart, brief 500/502 from the
      // dev rewrite-proxy). Keep current data so the UI doesn't flash
      // empty between polls; the next tick will recover. Logged at warn
      // so devs can still see it in the console.
      console.warn("library refresh failed", e);
    } finally {
      setRefreshing(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chFilter, statusFilter]);

  // Light auto-refresh — every 5s
  useEffect(() => {
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chFilter, statusFilter]);

  const filtered = useMemo(() => {
    if (!jobs) return null;
    const q = search.trim().toLowerCase();
    if (!q) return jobs;
    return jobs.filter((j) =>
      [j.topic, j.channel, j.status, j.stage, j.job_id]
        .filter(Boolean)
        .some((s) => String(s).toLowerCase().includes(q)),
    );
  }, [jobs, search]);

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio · Library"
        title="Library"
        description="Every render across every channel. Filter, search, open."
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={refresh}>
              <RefreshCw className={refreshing ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
              Refresh
            </Button>
            <Button asChild size="sm">
              <Link href="/app/create">
                <Plus className="h-3.5 w-3.5" />
                Create
              </Link>
            </Button>
          </>
        }
      />

      <div className="flex flex-1 flex-col px-6 py-6 md:px-8">
        {/* Filters */}
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-surface px-3 py-2.5">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Search topics, channels, ids…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 border-0 bg-transparent pl-8 text-[12.5px] focus-visible:ring-0"
            />
          </div>
          <div className="flex items-center gap-1.5">
            <Filter className="h-3 w-3 text-muted-foreground" />
            <Select value={chFilter} onValueChange={setChFilter}>
              <SelectTrigger className="h-8 w-44 border-border bg-surface-2 text-[12px]">
                <SelectValue placeholder="All channels" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All channels</SelectItem>
                {(channels ?? []).map((c) => (
                  <SelectItem key={c.key} value={c.key}>
                    {c.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={statusFilter} onValueChange={setStatusFilter}>
              <SelectTrigger className="h-8 w-36 border-border bg-surface-2 text-[12px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATUS_OPTIONS.map((s) => (
                  <SelectItem key={s.value} value={s.value}>
                    {s.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="mt-5 flex-1">
          {filtered === null ? (
            <CardGridSkeleton />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={Video}
              title="No renders match your filters."
              description="Clear filters or kick off a new render — eight channels are ready."
              action={
                <Button asChild size="sm">
                  <Link href="/app/create">Create a Short</Link>
                </Button>
              }
              className="mt-8"
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {filtered.map((j) => (
                <JobCard key={j.job_id} job={j} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function JobCard({ job }: { job: Job }) {
  const previewUrl = job.preview_url ?? jobsApi.previewUrl(job.job_id);
  const showPreview = job.status === "done" || job.status === "uploading";
  return (
    <Link
      href={`/app/render/${job.job_id}`}
      className="group flex flex-col overflow-hidden rounded-xl border border-border bg-surface transition-colors hover:border-border-strong"
    >
      <div className="relative aspect-[9/16] w-full overflow-hidden bg-background">
        {showPreview ? (
          // eslint-disable-next-line jsx-a11y/media-has-caption
          <video
            src={previewUrl}
            muted
            playsInline
            preload="metadata"
            className="h-full w-full object-cover"
            onMouseEnter={(e) => {
              const v = e.currentTarget;
              v.play().catch(() => {});
            }}
            onMouseLeave={(e) => {
              const v = e.currentTarget;
              v.pause();
              v.currentTime = 0;
            }}
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-surface-2">
            <ChannelIcon channel={job.channel ?? ""} size="lg" />
          </div>
        )}
        <div className="absolute left-2 top-2">
          <StatusPill
            status={job.status}
            pulse={job.status === "rendering" || job.status === "uploading"}
          />
        </div>
      </div>
      <div className="flex flex-1 flex-col gap-2 p-4">
        <div className="flex items-center gap-2">
          <ChannelIcon channel={job.channel ?? ""} size="sm" />
          <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
            {channelLabel(job.channel)}
          </span>
        </div>
        <div className="line-clamp-2 text-[13px] font-medium leading-snug tracking-tight">
          {job.topic ?? "(untitled)"}
        </div>
        <div className="mt-auto flex items-center justify-between font-mono text-[10.5px] text-muted-foreground">
          <span>{job.stage ?? "—"}</span>
          <span>{relativeTime(job.updated_at ?? job.created_at)}</span>
        </div>
      </div>
    </Link>
  );
}

function CardGridSkeleton() {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {Array(8)
        .fill(0)
        .map((_, i) => (
          <Skeleton key={i} className="aspect-[3/4]" />
        ))}
    </div>
  );
}
