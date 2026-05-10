"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Check,
  ExternalLink,
  Loader2,
  Play,
  Square,
  Sparkles,
  X,
  Youtube,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/app/page-header";
import { burnerApi } from "@/lib/api";
import type {
  BurnerChannel,
  BurnerEngagePhase,
  BurnerEngageState,
  BurnerEngageVideo,
} from "@/lib/types";
import { cn } from "@/lib/utils";

export default function BurnerChannelsPage() {
  const [burners, setBurners] = useState<BurnerChannel[] | null>(null);
  const [catalogSize, setCatalogSize] = useState<number>(0);
  const [drawerSlug, setDrawerSlug] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const r = await burnerApi.list();
      setBurners(r.burners);
      setCatalogSize(r.catalog_size);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio"
        title="Burner Channels"
        description="Channels we have YouTube API access to but aren't part of the production stable. Use them to cross-engage with our catalog (likes, subs, watch-time) without polluting the main accounts."
      />

      <div className="space-y-8 p-6 md:p-8">
        <div className="rounded-xl border border-border bg-surface px-5 py-4">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[12.5px]">
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <Sparkles className="h-3.5 w-3.5 text-foreground/70" />
              <span className="text-foreground/85">{burners?.length ?? "…"}</span>
              <span>burner{burners?.length === 1 ? "" : "s"}</span>
            </div>
            <div className="flex items-center gap-1.5 text-muted-foreground">
              <Youtube className="h-3.5 w-3.5 text-foreground/70" />
              <span className="text-foreground/85">{catalogSize}</span>
              <span>videos in production catalog</span>
            </div>
          </div>
        </div>

        {error && (
          <div className="rounded-md border border-rose-500/40 bg-rose-500/5 px-4 py-3 text-[12.5px] text-rose-200">
            {error}
          </div>
        )}

        {burners === null ? (
          <div className="space-y-3">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-24 w-full" />
            ))}
          </div>
        ) : burners.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="space-y-3">
            {burners.map((b) => (
              <BurnerRow
                key={b.slug}
                burner={b}
                onOpen={() => setDrawerSlug(b.slug)}
                onAfterAction={refresh}
              />
            ))}
          </div>
        )}
      </div>

      {drawerSlug && (
        <EngageDrawer
          slug={drawerSlug}
          onClose={() => setDrawerSlug(null)}
          onAfterAction={refresh}
        />
      )}
    </div>
  );
}

function BurnerRow({
  burner,
  onOpen,
  onAfterAction,
}: {
  burner: BurnerChannel;
  onOpen: () => void;
  onAfterAction: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const running = Boolean(burner.running);

  async function start() {
    setBusy(true);
    try {
      const r = await burnerApi.start(burner.slug);
      if (r.started) {
        toast.success(`Engage worker started for ${burner.slug}`, {
          description: `pid ${r.pid}. A Chrome window will open shortly.`,
        });
      } else {
        toast.message(`${burner.slug} is already running`, {
          description: r.reason ?? "",
        });
      }
      onOpen();
      onAfterAction();
    } catch (e) {
      toast.error("Couldn't start", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    try {
      await burnerApi.stop(burner.slug);
      toast.message(`Stop requested for ${burner.slug}`);
      onAfterAction();
    } catch (e) {
      toast.error("Couldn't stop", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-5 md:flex-row md:items-center">
      <div className="flex items-center gap-3 md:flex-1">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-md border border-border bg-surface-2 font-mono text-[12px] uppercase tracking-tight text-foreground">
          {burner.slug.slice(0, 2)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[14px] font-medium tracking-tight">{burner.title}</span>
            {running && <PhaseBadge phase={burner.phase ?? "watching"} />}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-muted-foreground">
            <span className="font-mono">{burner.slug}</span>
            <span>·</span>
            <span>{burner.email ?? "no email"}</span>
            {burner.channel_id && (
              <>
                <span>·</span>
                <a
                  href={`https://youtube.com/channel/${burner.channel_id}`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:text-foreground"
                >
                  YouTube <ExternalLink className="h-3 w-3" />
                </a>
              </>
            )}
          </div>
          {burner.last_action_msg && (
            <div className="mt-1.5 truncate text-[11.5px] text-muted-foreground/85">
              <Activity className="mr-1 inline h-3 w-3 text-muted-foreground/60" />
              {burner.last_action_msg}
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2">
        {running ? (
          <>
            <Button variant="outline" size="sm" onClick={onOpen}>
              View live
            </Button>
            <Button variant="destructive" size="sm" onClick={stop} disabled={busy}>
              <Square className="h-3.5 w-3.5" />
              Stop
            </Button>
          </>
        ) : (
          <>
            {burner.phase && (
              <Button variant="ghost" size="sm" onClick={onOpen}>
                Last run
              </Button>
            )}
            <Button
              size="sm"
              onClick={start}
              disabled={busy || !burner.profile_known}
              title={
                burner.profile_known
                  ? undefined
                  : `Add ${burner.slug} → email mapping in ~/.config/ytfactory/profile_map.json first`
              }
            >
              {busy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              Cross-engage
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function PhaseBadge({ phase }: { phase: BurnerEngagePhase }) {
  const tone: Record<BurnerEngagePhase, { label: string; cls: string }> = {
    initializing: { label: "Starting", cls: "border-amber-500/40 bg-amber-500/10 text-amber-200" },
    engaging: { label: "Engaging", cls: "border-amber-500/40 bg-amber-500/10 text-amber-200" },
    watching: { label: "Watching", cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" },
    stopped: { label: "Stopped", cls: "border-border bg-surface-2 text-muted-foreground" },
    failed: { label: "Failed", cls: "border-rose-500/40 bg-rose-500/10 text-rose-200" },
    blocked: { label: "Blocked", cls: "border-rose-500/40 bg-rose-500/10 text-rose-200" },
  };
  const t = tone[phase];
  return (
    <Badge variant="outline" className={cn("font-mono text-[10px] uppercase", t.cls)}>
      {t.label}
    </Badge>
  );
}

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed border-border bg-surface p-10 text-center">
      <Sparkles className="mx-auto h-6 w-6 text-muted-foreground" />
      <div className="mt-3 text-[14px] font-medium tracking-tight">No burner channels yet</div>
      <p className="mx-auto mt-2 max-w-md text-[12.5px] leading-relaxed text-muted-foreground">
        A burner channel has an OAuth token at
        <code className="mx-1 rounded bg-surface-2 px-1 py-0.5 font-mono text-[11px]">
          ~/.config/ytfactory/youtube_token_&lt;slug&gt;.json
        </code>
        AND a row in
        <code className="mx-1 rounded bg-surface-2 px-1 py-0.5 font-mono text-[11px]">
          ~/.config/ytfactory/channel_ids.json
        </code>
        but is NOT in the production registry. Run a fresh OAuth flow under any non-production slug to add one.
      </p>
    </div>
  );
}

function EngageDrawer({
  slug,
  onClose,
  onAfterAction,
}: {
  slug: string;
  onClose: () => void;
  onAfterAction: () => void;
}) {
  const [state, setState] = useState<BurnerEngageState | null | "loading" | "missing">("loading");

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const r = await burnerApi.poll(slug);
        if (!cancelled) setState(r);
      } catch {
        if (!cancelled) setState("missing");
      }
    }
    tick();
    const id = setInterval(tick, 2500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [slug]);

  async function stop() {
    try {
      await burnerApi.stop(slug);
      toast.message(`Stop requested for ${slug}`);
      onAfterAction();
    } catch (e) {
      toast.error("Couldn't stop", { description: e instanceof Error ? e.message : String(e) });
    }
  }

  const isObj = state && state !== "loading" && state !== "missing";
  const phase = isObj ? state.phase : null;
  const videos: BurnerEngageVideo[] = useMemo(
    () => (isObj ? state.videos : []),
    [isObj, state],
  );
  const totals = useMemo(() => {
    const liked = videos.filter((v) => v.liked).length;
    const subs = new Set(videos.filter((v) => v.subscribed).map((v) => v.channel)).size;
    const tabs = videos.filter((v) => v.tab_open).length;
    const watch = videos.reduce((acc, v) => acc + v.watch_seconds, 0);
    return { liked, subs, tabs, watch };
  }, [videos]);

  return (
    <div className="fixed inset-0 z-40 flex">
      {/* Backdrop */}
      <button
        type="button"
        className="flex-1 bg-background/60 backdrop-blur-sm"
        aria-label="Close"
        onClick={onClose}
      />
      {/* Drawer */}
      <aside className="flex h-full w-full max-w-2xl flex-col border-l border-border bg-background">
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <span className="text-[13px] font-medium tracking-tight">Engage · {slug}</span>
            {phase && <PhaseBadge phase={phase} />}
          </div>
          <div className="flex items-center gap-2">
            {phase && phase !== "stopped" && phase !== "failed" && (
              <Button size="sm" variant="destructive" onClick={stop}>
                <Square className="h-3.5 w-3.5" />
                Stop
              </Button>
            )}
            <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close drawer">
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>

        {state === "loading" && (
          <div className="p-6">
            <Skeleton className="h-24 w-full" />
          </div>
        )}

        {state === "missing" && (
          <div className="flex flex-1 items-center justify-center px-6 text-center">
            <div>
              <AlertTriangle className="mx-auto h-6 w-6 text-amber-300/70" />
              <div className="mt-3 text-[13px] font-medium tracking-tight">No engage state yet</div>
              <p className="mx-auto mt-1.5 max-w-sm text-[12px] text-muted-foreground">
                The worker hasn&apos;t written its first state file. Give it a few seconds, or hit
                Cross-engage from the row to start one.
              </p>
            </div>
          </div>
        )}

        {isObj && (
          <>
            <div className="grid grid-cols-4 gap-3 border-b border-border bg-surface px-5 py-4 text-[12px]">
              <Stat label="Liked" value={totals.liked} hint={`/ ${videos.length}`} />
              <Stat label="Subscribed" value={totals.subs} hint="channels" />
              <Stat label="Open tabs" value={totals.tabs} hint="watching" />
              <Stat
                label="Watch-time"
                value={fmtSec(totals.watch)}
                hint="cumulative"
              />
            </div>

            {state.last_action_msg && (
              <div className="border-b border-border px-5 py-3 font-mono text-[11.5px] text-muted-foreground">
                <Activity className="mr-1.5 inline h-3 w-3 text-muted-foreground/70" />
                {state.last_action_msg}
                <span className="ml-2 text-muted-foreground/60">
                  {state.last_action_at ? `· ${ago(state.last_action_at)}` : ""}
                </span>
              </div>
            )}

            <div className="flex-1 overflow-y-auto">
              <table className="w-full text-[12px]">
                <thead className="sticky top-0 bg-surface text-left text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2">Video</th>
                    <th className="px-2 py-2">Channel</th>
                    <th className="px-2 py-2 text-center">Like</th>
                    <th className="px-2 py-2 text-center">Sub</th>
                    <th className="px-2 py-2 text-center">Tab</th>
                    <th className="px-2 py-2 text-right">Watch</th>
                  </tr>
                </thead>
                <tbody>
                  {videos.map((v) => (
                    <tr key={v.video_id} className="border-t border-border/60">
                      <td className="px-4 py-2">
                        <a
                          href={v.url}
                          target="_blank"
                          rel="noreferrer"
                          className="line-clamp-1 hover:text-foreground"
                          title={v.title}
                        >
                          {v.title}
                        </a>
                        {v.error && (
                          <div className="mt-0.5 truncate text-[10.5px] text-rose-300">
                            {v.error}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-2 text-muted-foreground">{v.channel_label}</td>
                      <td className="px-2 py-2 text-center">{v.liked && <Check className="mx-auto h-3.5 w-3.5 text-emerald-300" />}</td>
                      <td className="px-2 py-2 text-center">{v.subscribed && <Check className="mx-auto h-3.5 w-3.5 text-emerald-300" />}</td>
                      <td className="px-2 py-2 text-center">{v.tab_open && <Check className="mx-auto h-3.5 w-3.5 text-emerald-300" />}</td>
                      <td className="px-2 py-2 text-right text-muted-foreground">{fmtSec(v.watch_seconds)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </aside>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: number | string; hint?: string }) {
  return (
    <div>
      <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className="num text-[18px] font-medium tracking-tight">{value}</span>
        {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
      </div>
    </div>
  );
}

function fmtSec(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return `${m}m ${r}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function ago(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const dt = (Date.now() - t) / 1000;
  if (dt < 60) return `${Math.floor(dt)}s ago`;
  if (dt < 3600) return `${Math.floor(dt / 60)}m ago`;
  return `${Math.floor(dt / 3600)}h ago`;
}
