"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Check,
  ChevronDown,
  ExternalLink,
  Loader2,
  Play,
  Plus,
  Square,
  Sparkles,
  UserPlus,
  Users,
  X,
  Youtube,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { PageHeader } from "@/components/app/page-header";
import {
  burnerApi,
  ENGAGE_MODES,
  ENGAGE_MODE_LABELS,
  ENGAGE_MODE_DESCRIPTIONS,
  type EngageMode,
} from "@/lib/api";
import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
import { CK } from "@/lib/cache-keys";
import { useVisiblePoll } from "@/lib/use-visible-poll";
import type {
  BurnerChannel,
  BurnerEngagePhase,
  BurnerEngageState,
  BurnerEngageVideo,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface BurnerListResp {
  burners: BurnerChannel[];
  catalog_size: number;
}

// Shape returned by POST /api/burner_channels/subscribe_all_burners.
// Mirrors the route's response (see control/routes/burner_routes.py).
interface SubscribeAllResult {
  enqueued_count: number;
  skipped_count: number;
  enqueued: { slug: string; task_id: string }[];
  skipped: { slug: string; reason: string }[];
  hint?: string;
}

// Drives the bulk drawer's lifecycle: "pending" while the POST is in
// flight (so the drawer opens immediately and the operator gets visible
// feedback that the click registered), then either the parsed result or
// an error string. Without this, Subscribe All only surfaced a toast +
// silently mutated server state — operators reasonably read that as
// "did nothing".
//
// "result" carries a startedAt ISO timestamp captured AT POST time so
// the drawer can distinguish "this burner has freshly-enqueued work
// queued up" from "this burner finished a totally unrelated previous
// run hours ago and still has stale phase=stopped on it" — without
// that gate, the drawer would mislabel still-queued rows as Done the
// moment the page-level poll surfaces the prior state.
type BulkSubscribeState =
  | { kind: "pending" }
  | { kind: "result"; result: SubscribeAllResult; startedAt: string }
  | { kind: "error"; message: string };

export default function BurnerChannelsPage() {
  // Stale-while-revalidate: paint cached burner list instantly on
  // mount + every navigation. 5 s poll cadence preserved from the
  // legacy raw useVisiblePoll for live engage state.
  const { data: list, error: listError, refresh } = useStaleWhileRevalidate<BurnerListResp>(
    CK.burnerList,
    () => burnerApi.list(),
    5_000,
  );
  const burners: BurnerChannel[] | null = list?.burners ?? null;
  const catalogSize = list?.catalog_size ?? 0;
  const [drawerSlug, setDrawerSlug] = useState<string | null>(null);
  // Bulk drawer is independent of the per-slug drawer. When both are
  // requested at once (e.g. the operator clicks an enqueued row inside
  // the bulk drawer), the per-slug drawer takes precedence — see the
  // render order below where {drawerSlug && …} sits after the bulk
  // drawer in the DOM and inherits the higher z-index.
  const [bulkState, setBulkState] = useState<BulkSubscribeState | null>(null);
  const error = listError?.message ?? null;

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio"
        title="Burner Channels"
        description="Channels we have YouTube API access to but aren't part of the production stable. Use them to cross-engage with our catalog (likes, subs, watch-time) without polluting the main accounts."
      />

      <div className="space-y-8 p-6 md:p-8">
        <div className="rounded-xl border border-border bg-surface px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-4">
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
            <BulkActions
              eligibleCount={
                burners?.filter((b) => b.profile_known && !b.running).length ?? 0
              }
              onAfterAction={refresh}
              bulkInFlight={bulkState !== null}
              onBulkSubscribeStart={() => setBulkState({ kind: "pending" })}
              onBulkSubscribeResult={(result) =>
                setBulkState({
                  kind: "result",
                  result,
                  startedAt: new Date().toISOString(),
                })
              }
              onBulkSubscribeError={(message) =>
                setBulkState({ kind: "error", message })
              }
            />
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

      {bulkState && (
        <BulkSubscribeDrawer
          state={bulkState}
          burners={burners ?? []}
          onClose={() => setBulkState(null)}
          onOpenSlug={(slug) => setDrawerSlug(slug)}
          onAfterAction={refresh}
        />
      )}

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

function BulkActions({
  eligibleCount,
  onAfterAction,
  bulkInFlight,
  onBulkSubscribeStart,
  onBulkSubscribeResult,
  onBulkSubscribeError,
}: {
  eligibleCount: number;
  onAfterAction: () => void;
  bulkInFlight: boolean;
  onBulkSubscribeStart: () => void;
  onBulkSubscribeResult: (result: SubscribeAllResult) => void;
  onBulkSubscribeError: (message: string) => void;
}) {
  const [busy, setBusy] = useState<"subscribe" | "create" | null>(null);

  async function subscribeAll() {
    setBusy("subscribe");
    // Open the bulk drawer in "pending" state BEFORE the await so the
    // operator gets immediate visual feedback that the click landed.
    // Without this, the only signal during the request was the spinner
    // on the button — easy to miss against a busy page header — and on
    // a "Nothing to subscribe" outcome the only signal at all was a
    // toast that auto-dismissed in a few seconds.
    onBulkSubscribeStart();
    try {
      const r = await burnerApi.subscribeAllBurners();
      onBulkSubscribeResult(r);
      if (r.enqueued_count > 0) {
        toast.success(
          `Subscribe-only kicked off for ${r.enqueued_count} burner${r.enqueued_count === 1 ? "" : "s"}`,
          {
            description: r.skipped_count
              ? `${r.skipped_count} skipped (already running or no profile mapping). ${r.hint ?? ""}`
              : r.hint ?? "Each burner will subscribe to every catalog channel once and exit.",
          },
        );
      } else {
        toast.message("Nothing to subscribe", {
          description: r.hint ?? `Skipped ${r.skipped_count} burner${r.skipped_count === 1 ? "" : "s"}.`,
        });
      }
      onAfterAction();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      onBulkSubscribeError(message);
      toast.error("Subscribe-all failed", { description: message });
    } finally {
      setBusy(null);
    }
  }

  async function createBulk() {
    const raw = window.prompt(
      "How many new burner channels should the laptop agent create?\n\n" +
        "Default 50. Hard cap 100. Per-account Google rate-limit is ~5-10 successful creates / 24h, so the tail will fail until tomorrow — that's expected.",
      "50",
    );
    if (raw === null) return;
    const count = parseInt(raw, 10);
    if (!Number.isFinite(count) || count < 1) {
      toast.error("Bad count", { description: `'${raw}' isn't a positive integer.` });
      return;
    }
    setBusy("create");
    try {
      const r = await burnerApi.createBulk(count);
      toast.success(
        `Queued ${r.enqueued_count} create-burner task${r.enqueued_count === 1 ? "" : "s"}`,
        {
          description:
            (r.cap_applied ? `(capped from ${count}) ` : "") +
            (r.hint ?? "Laptop agent will pick them up shortly."),
        },
      );
      onAfterAction();
    } catch (e) {
      toast.error("Bulk create failed", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        onClick={subscribeAll}
        // Disabled while:
        //   - the local subprocess is busy with the POST itself, OR
        //   - the bulk drawer is already open (prevents the operator
        //     from impatiently re-firing while tasks are still queued
        //     but not yet running — the eligibility filter on the
        //     server only checks `is_running`, not "is queued", so a
        //     second click WOULD pile on duplicate BURNER_ENGAGE
        //     tasks for the same slugs).
        // We INTENTIONALLY no longer gate on `eligibleCount === 0`:
        // letting the operator click through to the drawer is itself
        // the explanation ("0 enqueued, 49 skipped (already_running)")
        // and matches the per-row Subscribe pattern. The previous
        // disabled-with-tooltip variant was the "did nothing" complaint.
        disabled={busy !== null || bulkInFlight}
        title={
          bulkInFlight
            ? "A bulk subscribe-all is already open — close the drawer to fire a new one."
            : eligibleCount === 0
              ? "No burners look eligible right now — click to see why (already running, missing profile mapping, etc.)."
              : `Subscribe-only across ${eligibleCount} eligible burner${eligibleCount === 1 ? "" : "s"} — each will subscribe to every catalog channel once and exit.`
        }
      >
        {busy === "subscribe" ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Users className="h-3.5 w-3.5" />
        )}
        Subscribe All
        {eligibleCount > 0 && (
          <Badge
            variant="outline"
            className="ml-1 px-1.5 py-0 font-mono text-[9px] uppercase tracking-tight border-border bg-surface-2 text-muted-foreground"
          >
            {eligibleCount}
          </Badge>
        )}
      </Button>
      <Button
        size="sm"
        onClick={createBulk}
        disabled={busy !== null}
        title="Spawn N create_burner_channel runs on the laptop agent. Default 50; per-account Google rate-limit is ~5-10/day so the tail will fail until tomorrow."
      >
        {busy === "create" ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Plus className="h-3.5 w-3.5" />
        )}
        Create 50 channels
      </Button>
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

  async function start(mode: EngageMode) {
    setBusy(true);
    try {
      const r = await burnerApi.start(burner.slug, mode);
      if (r.started) {
        toast.success(
          `Engage worker started for ${burner.slug}`,
          {
            description:
              `Mode: ${ENGAGE_MODE_LABELS[mode]}. ` +
              (r.pid
                ? `pid ${r.pid}. A Chrome window will open shortly.`
                : `Queued for laptop agent (task ${(r as any).task_id?.slice(0, 8) ?? "—"}).`),
          },
        );
      } else {
        toast.message(`${burner.slug} is already running`, {
          description: r.reason ?? "",
        });
      }
      onOpen();
      onAfterAction();
    } catch (e) {
      // Even on error, surface the drawer so the operator can see the
      // last known engage state (often the failure happened *because*
      // a previous run is stuck/queued, and the drawer's the only
      // place that view exists).
      onOpen();
      toast.error("Couldn't start", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    // Pop the drawer immediately so the operator can watch the worker
    // wind down (phase → "stopped", tabs closing, etc.) instead of
    // staring at the row with no feedback. Matches every other action
    // button on this row.
    onOpen();
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
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-5 transition-colors hover:border-border-strong md:flex-row md:items-center">
      {/* Row body opens the drawer when clicked — matches the old
          behavior where clicking anywhere on a row (not just an action
          button) surfaced the engage panel. Implemented as a div (not
          button) so the YouTube link can stay nested as a real <a>;
          the link's stopPropagation keeps clicks on it from also
          opening the drawer. The action buttons on the right have
          their own handlers and aren't inside this clickable area. */}
      <div
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onOpen();
          }
        }}
        className="flex cursor-pointer items-center gap-3 text-left md:flex-1"
        aria-label={`Open engage panel for ${burner.title}`}
      >
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
                  onClick={(e) => e.stopPropagation()}
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
              variant="outline"
              size="sm"
              disabled={busy || !burner.profile_known}
              onClick={() => start("subscribe_only")}
              title={
                burner.profile_known
                  ? "Subscribe to every catalog channel once and exit. No likes, no watch loop."
                  : `Add ${burner.slug} → email mapping in ~/.config/ytfactory/profile_map.json first`
              }
            >
              {busy ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <UserPlus className="h-3.5 w-3.5" />
              )}
              Subscribe
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  size="sm"
                  disabled={busy || !burner.profile_known}
                  title={
                    burner.profile_known
                      ? "Pick the engagement intensity"
                      : `Add ${burner.slug} → email mapping in ~/.config/ytfactory/profile_map.json first`
                  }
                >
                  {busy ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Play className="h-3.5 w-3.5" />
                  )}
                  Cross-engage
                  <ChevronDown className="h-3.5 w-3.5 -mr-1" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-[320px]">
                <DropdownMenuLabel className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                  Engagement mode
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                {ENGAGE_MODES.filter((m) => m !== "subscribe_only").map((m) => (
                  <DropdownMenuItem
                    key={m}
                    onSelect={() => start(m)}
                    className="flex flex-col items-start gap-0.5 py-2"
                  >
                    <div className="flex items-center gap-1.5 text-[13px] font-medium tracking-tight">
                      {ENGAGE_MODE_LABELS[m]}
                      {m === "like_subscribe_view" && (
                        <Badge
                          variant="outline"
                          className="ml-1 px-1.5 py-0 font-mono text-[9px] uppercase tracking-tight border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                        >
                          default
                        </Badge>
                      )}
                      {m === "complete" && (
                        <Badge
                          variant="outline"
                          className="ml-1 px-1.5 py-0 font-mono text-[9px] uppercase tracking-tight border-amber-500/40 bg-amber-500/10 text-amber-200"
                        >
                          shadow-ban risk
                        </Badge>
                      )}
                    </div>
                    <p className="text-[11.5px] leading-snug text-muted-foreground">
                      {ENGAGE_MODE_DESCRIPTIONS[m]}
                    </p>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
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

// Right-side drawer that opens when "Subscribe All" is clicked. Fixes
// two operator-visibility gaps in the original Subscribe All flow:
//
//   1. The button only fired a toast — easy to miss + auto-dismissed —
//      with no per-burner status. Operators reasonably read that as
//      "did nothing", especially because the laptop agent leases tasks
//      one at a time so the first Chrome window can take 30-90 s to
//      appear after the click.
//   2. The per-row Subscribe button opens this same right panel; the
//      page-level Subscribe All didn't, so the visual mental model
//      (click an action → right panel opens) was inconsistent.
//
// The drawer renders three states:
//   - "pending":  spinner while the POST is in flight (drawer opens
//                 BEFORE the await so the click feels responsive even
//                 on a slow Cloud Run hop).
//   - "result":   summary header + Enqueued list (each row pulls live
//                 phase from the page's burner list which is already
//                 polled every 5 s) + Skipped list with reasons.
//   - "error":    surfaces the failure inline so the operator doesn't
//                 have to dig into the toast that may already be gone.
function BulkSubscribeDrawer({
  state,
  burners,
  onClose,
  onOpenSlug,
  onAfterAction,
}: {
  state: BulkSubscribeState;
  burners: BurnerChannel[];
  onClose: () => void;
  onOpenSlug: (slug: string) => void;
  onAfterAction: () => void;
}) {
  const result = state.kind === "result" ? state.result : null;
  // Captured at POST time. Used to suppress stale phase data from a
  // PRIOR engage run on the same burner — without this gate, a burner
  // whose previous run finished hours ago would render with phase=
  // "stopped" the moment the page-level poll surfaces it, and the
  // operator would (rightly) assume the freshly-enqueued task already
  // wrapped up. The convention: trust live phase only when the state
  // file's last_action_at is at-or-after when we hit POST, OR when the
  // burner is currently running (running=true is unambiguous fresh
  // signal regardless of timestamp).
  const startedAtMs = state.kind === "result" ? Date.parse(state.startedAt) : null;
  const isFreshFor = (b: BurnerChannel | undefined) => {
    if (!b) return false;
    if (b.running) return true;
    if (startedAtMs == null) return false;
    if (!b.last_action_at) return false;
    const ts = Date.parse(b.last_action_at);
    return Number.isFinite(ts) && ts >= startedAtMs;
  };

  // Index burners by slug so each enqueued row can pick up its live
  // phase / last_action_msg from the page-level poll without minting
  // its own per-slug poller (49 burners × 2.5 s polls would saturate
  // the control plane's lease backend).
  const bySlug = useMemo(() => {
    const m = new Map<string, BurnerChannel>();
    for (const b of burners) m.set(b.slug, b);
    return m;
  }, [burners]);

  // Aggregate "are any of the enqueued burners actually doing work
  // right now?" for the summary bar. Counts a burner as "running" if
  // the page-level list has flagged it as such — this is the same
  // signal the per-row PhaseBadge renders. "Done" only counts burners
  // whose terminal-phase signal is FRESH (i.e. arrived after we POSTed)
  // so a stale "stopped" from yesterday doesn't inflate the Done tile.
  const liveStats = useMemo(() => {
    if (!result) return { running: 0, queued: 0, done: 0 };
    let running = 0;
    let queued = 0;
    let done = 0;
    for (const e of result.enqueued) {
      const b = bySlug.get(e.slug);
      if (b?.running) {
        running++;
      } else if (isFreshFor(b) && (b?.phase === "stopped" || b?.phase === "failed")) {
        done++;
      } else {
        queued++;
      }
    }
    return { running, queued, done };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, bySlug, startedAtMs]);

  return (
    <div className="fixed inset-0 z-40 flex">
      <button
        type="button"
        className="flex-1 bg-background/60 backdrop-blur-sm"
        aria-label="Close"
        onClick={onClose}
      />
      <aside className="flex h-full w-full max-w-2xl flex-col border-l border-border bg-background">
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <Users className="h-4 w-4 text-foreground/70" />
            <span className="text-[13px] font-medium tracking-tight">Subscribe All · bulk</span>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close drawer">
            <X className="h-4 w-4" />
          </Button>
        </div>

        {state.kind === "pending" && (
          <div className="flex flex-1 items-center justify-center p-6">
            <div className="flex flex-col items-center gap-3 text-center">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              <div className="text-[13px] font-medium tracking-tight">Enqueuing subscribe-only runs…</div>
              <p className="max-w-sm text-[12px] text-muted-foreground">
                Posting to the control plane. The laptop agent will lease the tasks
                next and a Chrome window will pop for each burner in turn.
              </p>
            </div>
          </div>
        )}

        {state.kind === "error" && (
          <div className="flex flex-1 items-center justify-center px-6 text-center">
            <div>
              <AlertTriangle className="mx-auto h-6 w-6 text-rose-300/80" />
              <div className="mt-3 text-[13px] font-medium tracking-tight">Subscribe-all failed</div>
              <p className="mx-auto mt-1.5 max-w-sm text-[12px] text-rose-200/85">{state.message}</p>
            </div>
          </div>
        )}

        {result && (
          <>
            <div className="grid grid-cols-4 gap-3 border-b border-border bg-surface px-5 py-4 text-[12px]">
              <Stat label="Enqueued" value={result.enqueued_count} hint="burners" />
              <Stat label="Running" value={liveStats.running} hint="now" />
              <Stat
                label="Queued"
                value={liveStats.queued}
                hint="waiting for agent"
              />
              <Stat label="Skipped" value={result.skipped_count} hint="see below" />
            </div>

            {result.hint && (
              <div className="border-b border-border bg-amber-500/5 px-5 py-3 text-[12px] text-amber-100/85">
                {result.hint}
              </div>
            )}

            <div className="flex-1 overflow-y-auto">
              {result.enqueued.length > 0 && (
                <section>
                  <h3 className="sticky top-0 border-b border-border bg-surface px-5 py-2 text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                    Enqueued · {result.enqueued.length}
                  </h3>
                  <ul>
                    {result.enqueued.map((e) => {
                      const b = bySlug.get(e.slug);
                      const running = Boolean(b?.running);
                      // Suppress phase badges that pre-date this bulk
                      // run — see isFreshFor / startedAtMs above for
                      // why. Falling through to the "queued" badge is
                      // the correct rendering for a row whose worker
                      // hasn't been leased by the laptop agent yet.
                      const showPhase = isFreshFor(b) && b?.phase;
                      const showLastMsg = isFreshFor(b) && b?.last_action_msg;
                      return (
                        <li
                          key={e.slug}
                          className="flex items-center justify-between gap-3 border-b border-border/60 px-5 py-2.5 transition-colors hover:bg-surface/60"
                        >
                          <button
                            type="button"
                            onClick={() => onOpenSlug(e.slug)}
                            className="flex min-w-0 flex-1 cursor-pointer flex-col items-start text-left"
                            aria-label={`Open engage panel for ${e.slug}`}
                          >
                            <div className="flex items-center gap-2">
                              <span className="font-mono text-[12px] tracking-tight text-foreground">
                                {b?.title ?? e.slug}
                              </span>
                              {showPhase ? (
                                <PhaseBadge phase={b!.phase!} />
                              ) : (
                                <Badge
                                  variant="outline"
                                  className="font-mono text-[9px] uppercase border-border bg-surface-2 text-muted-foreground"
                                >
                                  queued
                                </Badge>
                              )}
                            </div>
                            <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
                              <span className="font-mono">{e.slug}</span>
                              {showLastMsg && (
                                <>
                                  <span>·</span>
                                  <span className="truncate">{b!.last_action_msg}</span>
                                </>
                              )}
                            </div>
                          </button>
                          {running && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={async (ev) => {
                                ev.stopPropagation();
                                try {
                                  await burnerApi.stop(e.slug);
                                  toast.message(`Stop requested for ${e.slug}`);
                                  onAfterAction();
                                } catch (err) {
                                  toast.error("Couldn't stop", {
                                    description: err instanceof Error ? err.message : String(err),
                                  });
                                }
                              }}
                            >
                              <Square className="h-3.5 w-3.5" />
                              Stop
                            </Button>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}

              {result.skipped.length > 0 && (
                <section>
                  <h3 className="sticky top-0 border-b border-border bg-surface px-5 py-2 text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                    Skipped · {result.skipped.length}
                  </h3>
                  <ul>
                    {result.skipped.map((s) => {
                      const b = bySlug.get(s.slug);
                      return (
                        <li
                          key={s.slug}
                          className="flex items-center justify-between gap-3 border-b border-border/60 px-5 py-2.5"
                        >
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="font-mono text-[12px] tracking-tight text-foreground/85">
                                {b?.title ?? s.slug}
                              </span>
                              <Badge
                                variant="outline"
                                className="font-mono text-[9px] uppercase border-border bg-surface-2 text-muted-foreground"
                              >
                                {s.reason}
                              </Badge>
                            </div>
                            <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                              {s.slug}
                            </div>
                          </div>
                          {s.reason === "already_running" && (
                            <Button variant="ghost" size="sm" onClick={() => onOpenSlug(s.slug)}>
                              View live
                            </Button>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}

              {result.enqueued.length === 0 && result.skipped.length === 0 && (
                <div className="flex flex-1 items-center justify-center px-6 py-10 text-center">
                  <div>
                    <AlertTriangle className="mx-auto h-6 w-6 text-amber-300/70" />
                    <div className="mt-3 text-[13px] font-medium tracking-tight">No burners affected</div>
                    <p className="mx-auto mt-1.5 max-w-sm text-[12px] text-muted-foreground">
                      {result.hint ?? "The control plane returned an empty enqueue and skip list. Check that pipeline/burners.yaml has at least one entry with a google_email mapping."}
                    </p>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </aside>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed border-border bg-surface p-10 text-center">
      <Sparkles className="mx-auto h-6 w-6 text-muted-foreground" />
      <div className="mt-3 text-[14px] font-medium tracking-tight">No burner channels yet</div>
      <p className="mx-auto mt-2 max-w-md text-[12.5px] leading-relaxed text-muted-foreground">
        Burners are loaded from
        <code className="mx-1 rounded bg-surface-2 px-1 py-0.5 font-mono text-[11px]">
          pipeline/burners.yaml
        </code>
        — the committed source of truth (slug, title, channel_id, google_email).
        Add an entry there and redeploy, or mint a new one with
        <code className="mx-1 rounded bg-surface-2 px-1 py-0.5 font-mono text-[11px]">
          python -m pipeline.cross_engage.create_burner_channel
        </code>
        and promote it into the YAML.
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

  useVisiblePoll(async () => {
    try {
      const r = await burnerApi.poll(slug);
      setState(r);
    } catch {
      setState("missing");
    }
  }, 2500, [slug]);

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
