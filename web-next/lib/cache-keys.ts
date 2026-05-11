// Cache-key registry + per-route prefetch table.
//
// One source of truth for SWR cache keys + fetchers, used by:
//
//   - `useStaleWhileRevalidate(...)` call sites in pages
//   - `<AppShellWarmer />` for first-mount parallel prime of all
//     sidebar destinations
//   - `<PrefetchLink />` for hover/focus/pointerdown prime of the
//     destination's endpoints before the user actually navigates
//
// Discipline: every page that participates in the SWR cache MUST
// register its keys + fetchers here, and import the constants from
// here (don't duplicate the cache-key string inline). This guarantees
// the warm-fetch + hover-prefetch hit the SAME key the page reads on
// mount — otherwise we get a "primed but page still flashes skeletons"
// bug that's invisible in dev but real in prod.

import {
  api,
  burnerApi,
  queueApi,
} from "@/lib/api";
import type {
  BurnerChannel,
  ChannelSummary,
  DashboardData,
  QueueState,
} from "@/lib/types";

/** Cache keys — keep these in lockstep with the fetchers below. */
export const CK = {
  dashboardSummary: "dashboard:summary",
  channelsList: "dashboard:channels",
  queueState: "queue:state",
  cloudHealth: "cloud:health",
  cloudCost30d: "cloud:cost:30d",
  cloudDeploys: "cloud:deploys",
  burnerList: "burner:list",
  adminRequests: "admin:requests",
  adminUsers: "admin:users",
  whoami: "auth:whoami",
  // Telemetry uses a per-hours suffix so the cache survives window
  // changes (24h vs 7d vs 30d each get their own slot).
  telemetryOverview: (hours: number) => `telemetry:overview:${hours}`,
  telemetryServices: (hours: number) => `telemetry:services:${hours}`,
  telemetryErrors: (hours: number) => `telemetry:errors:${hours}`,
  telemetryTimeline: (hours: number) => `telemetry:timeline:${hours}`,
  telemetryInitStatus: "telemetry:init_status",
} as const;

interface PrimerEntry {
  key: string;
  fetcher: () => Promise<unknown>;
}

interface BurnerListResp {
  burners: BurnerChannel[];
  catalog_size: number;
}

/**
 * Per-route prefetch table.
 *
 * Each route-prefix → set of (key, fetcher) pairs that the route's
 * page consumes. Used by `<PrefetchLink />` (when the user
 * hovers/focuses a sidebar link) and by `<AppShellWarmer />` (which
 * primes the lighter subset on first mount).
 *
 * Use route prefixes (e.g. "/app/cloud" matches "/app/cloud/anything")
 * — the lookup compares with `pathname.startsWith(prefix)`.
 *
 * Telemetry default window is 24h to match
 * `app/app/telemetry/*-section.tsx::useState<number>(24)`.
 */
export const ROUTE_PREFETCHES: Record<string, PrimerEntry[]> = {
  "/app": [
    { key: CK.dashboardSummary, fetcher: () => api.get<DashboardData>("/api/dashboard") },
    { key: CK.channelsList, fetcher: () => api.get<{ channels: ChannelSummary[] }>("/api/channels") },
  ],
  "/app/channels": [
    { key: CK.channelsList, fetcher: () => api.get<{ channels: ChannelSummary[] }>("/api/channels") },
  ],
  "/app/queue": [
    { key: CK.queueState, fetcher: () => queueApi.get() as Promise<QueueState> },
  ],
  "/app/cloud": [
    { key: CK.cloudHealth, fetcher: () => api.get("/api/cloud/health") },
    { key: CK.cloudCost30d, fetcher: () => api.get("/api/cloud/cost?days=30") },
    { key: CK.cloudDeploys, fetcher: () => api.get("/api/cloud/deploys") },
  ],
  "/app/burner-channels": [
    { key: CK.burnerList, fetcher: () => burnerApi.list() as Promise<BurnerListResp> },
  ],
  "/app/admin": [
    { key: CK.adminRequests, fetcher: () => api.get("/api/admin/requests") },
    { key: CK.adminUsers, fetcher: () => api.get("/api/admin/users") },
  ],
  "/app/telemetry": [
    { key: CK.telemetryOverview(24), fetcher: () => api.get("/api/telemetry/overview?hours=24") },
    // Services section uses hours=1 (legacy default — see services-section.tsx).
    { key: CK.telemetryServices(1), fetcher: () => api.get("/api/telemetry/services?hours=1") },
    // Errors section is hours=24 + limit=20 — the limit IS part of the URL, so the
    // cache key tracks the URL identity by hours alone (limit is fixed in code).
    { key: CK.telemetryErrors(24), fetcher: () => api.get("/api/telemetry/errors?hours=24&limit=20") },
    { key: CK.telemetryTimeline(24), fetcher: () => api.get("/api/telemetry/timeline?hours=24") },
    { key: CK.telemetryInitStatus, fetcher: () => api.get("/api/telemetry/init_status") },
  ],
};

/**
 * The keys that the app-shell warmer should prime on first mount.
 * Subset of ROUTE_PREFETCHES, kept narrow on purpose: we only warm the
 * lightest dashboard-class endpoints so the user's first click on any
 * sidebar link is instant. Telemetry / cloud / admin are hover-prime
 * only because they're heavier and admin-only.
 */
export const APP_SHELL_WARM_KEYS: ReadonlyArray<PrimerEntry> = [
  ...ROUTE_PREFETCHES["/app"],
  ...ROUTE_PREFETCHES["/app/queue"],
];

/**
 * Admin-only warmers — primed only when whoami says is_admin.
 */
export const APP_SHELL_WARM_KEYS_ADMIN: ReadonlyArray<PrimerEntry> = [
  ...ROUTE_PREFETCHES["/app/cloud"],
];

/** Look up the prefetch entries for a route prefix (longest match wins). */
export function getRoutePrefetches(href: string): PrimerEntry[] {
  if (ROUTE_PREFETCHES[href]) return ROUTE_PREFETCHES[href];
  const candidates = Object.keys(ROUTE_PREFETCHES)
    .filter((prefix) => href.startsWith(prefix))
    .sort((a, b) => b.length - a.length);
  if (candidates.length > 0) return ROUTE_PREFETCHES[candidates[0]];
  return [];
}
