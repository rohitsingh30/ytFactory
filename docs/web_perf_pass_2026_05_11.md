# Web perf overhaul — Wave A + B (2026-05-11)

Follow-up to `docs/web_perf_pass_2026_05_10.md`. The 2026-05-10 pass
shipped TTL caches + the `useStaleWhileRevalidate` hook on 2 of 7
dashboard pages and called it a day. User feedback the next morning:
"every page is still slow, every nav still flashes skeletons."

This pass closes that gap end-to-end. Three layers, every layer
strictly opt-in for legacy callers (no API breaks).

## TL;DR — every navigation should now paint instantly

| layer                                          | before                                 | after                                                                 |
|------------------------------------------------|----------------------------------------|-----------------------------------------------------------------------|
| `useStaleWhileRevalidate` (v1)                 | sessionStorage; per-tab; per-key state | localStorage + in-memory + cross-tab `storage` event subscription     |
| Pages on `useStaleWhileRevalidate`             | 2 of 7 (`/app`, `/app/channels`)       | **all 7** — queue, burner-channels, admin, cloud, telemetry × 4      |
| Sidebar `<Link>`                               | JS chunk prefetch only                 | JS chunk **plus** API data prefetch on hover/focus/pointerdown        |
| App-shell warm-fetch                           | none                                   | parallel prime of dashboard + queue (+ cloud/* if admin) on first mount |
| In-flight request dedup                        | none — sibling components both fetched | module-level Map; second caller awaits the first's promise            |
| Cross-tab cache sync                           | none                                   | `storage` event listener pushes updates between tabs for free         |
| Service worker for `/api/*` GET                | none                                   | versioned cache, stale-while-revalidate, kill switch via `?nosw=1`    |
| `/app/create` First Load                       | 217 kB                                 | 210 kB (CloneVoiceDialog + SongPicker dynamic-imported)               |
| `/app/cloud` First Load                        | 215 kB                                 | **118 kB** (recharts split via `./cost-bar-chart`)                    |
| `/app/telemetry` First Load                    | 218 kB                                 | **117 kB** (recharts split via `./timeline-line-chart`)               |

## Where the new primitives live

### Foundation

- **`web-next/lib/use-swr-cache.ts`** — `useStaleWhileRevalidate` v2.
  Three storage tiers (in-memory → localStorage → fetcher), in-flight
  request dedup via a module Map, cross-tab updates via the `storage`
  event, public `prime()` / `getCached()` / `setCached()` for
  imperative use. Backward-compatible with v1 callers — same
  positional signature, same return shape (plus new `lastUpdatedAt` +
  `enabled` option).
- **`web-next/lib/cache-keys.ts`** — single registry of cache keys +
  per-route fetcher tables. **Discipline**: every dashboard-class page
  MUST import its key from `CK` here. Inline string keys break the
  warmer + the prefetcher silently.

### Mount-time fan-out

- **`web-next/components/app/app-shell-warmer.tsx`** — mounted from
  `/app/layout.tsx`. On first mount: primes whoami, then dashboard +
  queue in parallel; if `is_admin`, also primes cloud/health +
  cloud/cost + cloud/deploys. Renders nothing.

### Hover/focus prefetch

- **`web-next/components/app/prefetch-link.tsx`** — drop-in
  `next/link` replacement that, on `onMouseEnter` / `onFocus` /
  `onPointerDown`, fires `prime()` for every `(key, fetcher)` pair
  registered for the destination route in `ROUTE_PREFETCHES`. JS
  prefetch comes for free from `next/link`; this adds the data layer.
- The sidebar (`web-next/components/nav/sidebar.tsx`) now uses
  `<PrefetchLink>` instead of `<Link>` for every nav item.

### Network-layer SWR

- **`web-next/public/sw.js`** — versioned cache (`ytfactory-api-v2`,
  bumped from v1 on 2026-05-11 to drop the cache populated by a
  broken handler — see
  [`docs/service_worker_handler_scope.md`](./service_worker_handler_scope.md)
  for the rule on why every SW change must bump the version).
  Intercepts only same-origin `/api/*` GETs; skips
  `/api/auth/*`, `/api/admin/*`, `/api/oauth/*`, `/api/jobs/*`,
  `/api/critiques/*` (correctness > speed for those). Stale-while-
  revalidate with 1 hr TTL + 200-entry cap. Stamps a `x-sw-cached-at`
  header on each cached response so the SW reader can age out.
- **`web-next/components/app/studio-sw-register.tsx`** — registers
  the SW after `window.load`. **Kill switch**: any `/app/*` URL with
  `?nosw=1` unregisters every SW + clears every `ytfactory-api-*`
  cache. Use this when triaging "stale data" reports before assuming
  the bug is server-side.
- Imperative wipe: `bustServiceWorkerCache()` from the same module.
  Wire into the logout flow if you ever surface multi-account switch.

### Code-split heavy components

- **`web-next/app/app/create/page.tsx`** — `CloneVoiceDialog` (590
  LOC) and `SongPicker` (150 LOC) are now `next/dynamic`-imported.
  They only ship to the client when the user opens the dialog /
  reaches the song-picker affordance.
- **`web-next/app/app/cloud/cost-bar-chart.tsx`** — extracted from
  `cost-section.tsx`; recharts (~50 KB gz) now lives in its own
  chunk. Skeleton placeholder of matching height during chunk load.
- **`web-next/app/app/telemetry/timeline-line-chart.tsx`** — same
  pattern. Together these two splits cut **~100 kB off both pages**.

## How to add a new dashboard endpoint (the new contract)

When adding `GET /api/foo` that the dashboard polls or any new
`/app/*` page that consumes one:

1. **Backend** — same as the 2026-05-10 contract: TTL cache the heavy
   bit + parallelize the fan-out + add the path to
   `web/server.py::_CACHEABLE_GET_PREFIXES`. (Unchanged.)

2. **Register the cache key + fetcher** in `web-next/lib/cache-keys.ts`:
   ```ts
   export const CK = {
     // ...existing
     myThing: "my:thing",
   } as const;

   export const ROUTE_PREFETCHES = {
     // ...existing
     "/app/my-thing": [
       { key: CK.myThing, fetcher: () => api.get<MyThing>("/api/foo") },
     ],
   };
   ```
   If the page should also be primed at app-shell mount (lightweight,
   high-traffic), add the entry to `APP_SHELL_WARM_KEYS`.

3. **Page** — use the hook with the registry constant:
   ```tsx
   import { useStaleWhileRevalidate } from "@/lib/use-swr-cache";
   import { CK } from "@/lib/cache-keys";

   const { data, error, refresh } = useStaleWhileRevalidate<MyThing>(
     CK.myThing,
     () => api.get<MyThing>("/api/foo"),
     30_000,
   );
   ```
   Optional: `{ enabled: somePrecondition }` if the fetcher should
   gate (e.g. admin-only); `{ freshForMs: 0 }` to disable the
   skip-on-mount-if-fresh gate (use for live data like `/api/queue`).

4. **Verify in DevTools**:
   - Network tab → `/api/foo` row should appear once on first visit,
     then NOT on subsequent navigation to / from the page within the
     freshness window.
   - Application → Local Storage → `ytfactory:swr:my:thing` — payload
     persists across reload.
   - Application → Service Workers → `/sw.js` shows "activated".
     Network requests show `(ServiceWorker)` source for cache hits.

## Anti-patterns (don't repeat)

All 2026-05-10 anti-patterns still apply, plus:

- **`useState + useVisiblePoll` in a NEW page**. The legacy 5
  migrations are the last; new pages MUST start on
  `useStaleWhileRevalidate`. (`useVisiblePoll` itself stays — the SWR
  hook uses it under the hood — but consumers should not hand-roll.)
- **Inline cache key string** like
  `useStaleWhileRevalidate("dashboard:summary", ...)`. Always
  `CK.dashboardSummary`. Inline strings make the warmer + the
  prefetcher unable to find the key, and the page gets cold cache.
- **Charts (recharts) imported at the top of the section file**.
  Wrap the chart JSX in a `./{slug}-chart.tsx` companion and
  `next/dynamic` it with a Skeleton loader of matching height. The
  100 kB savings on /app/cloud + /app/telemetry came from this rule.
- **Heavy dialog/picker imported eagerly**. Use `next/dynamic` so the
  bundle only ships when the affordance opens. Reference: the
  `CloneVoiceDialog` import in `app/app/create/page.tsx`.

## What's NOT done (deferred, with reason)

These were on the original 10-item plan but deferred. Each is a
bigger lift / infra change with weaker evidence-of-need now that
Wave A + B shipped — re-evaluate if the user reports it's still slow.

| # | Item | Why deferred |
|---|------|--------------|
| 5 | RSC for read-only pages (`/app`, `/app/channels`, `/app/telemetry`) | The SWR cache + warmer cover the perceived-speedup gap on REPEAT visits (which dominate). RSC's marginal value is mostly cold-cache first visits. Cookie forwarding through RSC `fetch()` is non-trivial. Re-visit if cold-load metrics still pin TTI > 1.5 s. |
| 8 | Collapse the two-hop API proxy | The route handler caches the Cloud Run ID token for 55 min, so warm requests pay only Node.js function-call overhead (~5–20 ms). Worth measuring before any structural change. Suggested probe: add a `Server-Timing: proxy;dur=<ms>` header in `web-next/app/api/[...path]/route.ts`. |
| 9 | SSE/live for `/api/queue` (currently 2 s poll) | Significant backend change — needs an SSE endpoint on FastAPI + new client subscription. Keep the 2 s poll for now; the SWR cache means tab-switches paint instantly even at that cadence. **Note (2026-05-11):** if the Queue page silently shows "Empty Completed" in prod despite known terminal jobs, the bug is in the BACKEND not the poll cadence — see `docs/data_flows.md` § "composite-index discipline" (the missing-index + too-broad-try/except pattern). |
| 10 | Cloud CDN in front of `web-next` | Real infra change — needs a global LB + Cloud CDN backend bucket + cert. Static `_next/*` assets already ship with content-hashed URLs and are already served with `Cache-Control: public, immutable, max-age=31536000` by Next.js. The remaining gain is geographic (asia-southeast1 → US/EU users). Defer until we have non-APAC users. |

## Rollback

| change | rollback |
|---|---|
| Service worker misbehaving | Hit any `/app/*` URL with `?nosw=1`. Per-user, immediate. |
| SWR cache corrupted | `localStorage.clear()` from DevTools console, or wait for entries to expire (`v: 2` schema rejects v1 entries automatically). |
| Need to bypass the in-memory cache | `import { setCached } from "@/lib/use-swr-cache"; setCached(CK.myKey, freshPayload)` from the writer side. |
| Want to disable a specific page's poll | Pass `{ enabled: false }` to its `useStaleWhileRevalidate` call. The page still hydrates from cache; just no background refetch. |

## Cross-references

- [`docs/web_perf_pass_2026_05_10.md`](./web_perf_pass_2026_05_10.md) — the foundation pass this builds on
- [`docs/two_frontend_topology.md`](./two_frontend_topology.md) — the
  web-next vs web-static topology (still applies — this pass only
  touched web-next)
- [`docs/website_personality.md`](./website_personality.md) — the
  studio's design contract (still applies)
