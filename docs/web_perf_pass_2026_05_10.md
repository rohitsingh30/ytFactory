# Web perf pass — dashboard latency 2026-05-10

User report: "every page on the live site takes 5-10 seconds." Landing
page TTFB was healthy (~200 ms) but every authenticated `/app/*` page
sat in skeleton-state because the polled API endpoints were slow.

Root causes + fixes — all rules below now stand for **every new
dashboard-class endpoint** and **every new polled page**.

## TL;DR — dashboard hot-path is now O(1) on warm polls

| layer                        | before                                  | after                                   |
|------------------------------|-----------------------------------------|-----------------------------------------|
| `list_upload_records()`      | sequential `download_as_bytes` per record (~50 ms × N) | 60 s TTL cache + 16-worker parallel fan-out |
| `_iter_all_uploads()` (web)  | per-channel `list_blobs` + per-record sequential downloads | 60 s TTL cache + 8-worker channel fan-out + 16-worker per-channel fan-out |
| `_load_cache()` (yt research)| `blob.reload()` HEAD on every call (~30 ms × 8 channels per poll) | 60 s TTL fast-path skips HEAD entirely on warm hits |
| `customization.list_channels`| 8 sequential GCS round-trips           | ThreadPoolExecutor (16 workers)         |
| `customization.get_channel`  | called `list_channels()` for ALL 8     | builds only the requested channel       |
| `/api/dashboard` top_performer | re-ran full `dashboard_videos` handler | direct compute from cached uploads + `_STATS_CACHE` |
| `/api/cloud/health`          | uncached, full sweep × every poll      | 5 s in-process cache                    |
| browser cache headers        | `Cache-Control` absent on read endpoints | `private, max-age=10, stale-while-revalidate=60` on dashboard-class GETs |
| dashboard poll cadence       | 10 s                                    | 30 s                                    |
| navigation repaint           | skeletons → spinner → content (every nav) | sessionStorage SWR — paint instantly with stale, revalidate in background |
| per-endpoint timing visibility | none                                  | `Server-Timing: total;dur=<ms>` on every response |

## Where each rule lives in code

### Server side (`ytfactory-web` — `web/server.py`, `control/`, `pipeline/`)

- **TTL cache + ThreadPoolExecutor for ANY GCS-walking enumeration**:
  - `control/core/storage.py::list_upload_records` — 60 s TTL keyed by
    bucket name; 16-worker parallel `download_bytes`.
    `bust_upload_records_cache()` is the public hook writers call.
  - `web/server.py::_iter_all_uploads` + `_list_uploads_gcs` — same
    pattern, different bucket (`YTFACTORY_STATE_BUCKET` /
    `<channel>/uploads/...` layout vs. `YTFACTORY_BUCKET` /
    `upload-records/<channel>/...`). 60 s TTL; 8-worker channel fan-out
    on top of 16-worker per-channel record fan-out.

- **TTL fast-path that skips the GCS HEAD round-trip for content-cached
  GETs**:
  - `pipeline/research/youtube.py::_load_cache` — when an in-process
    cache entry exists AND was verified within `_READ_CACHE_TTL_S`
    (60 s), return it immediately; only fall through to `blob.reload()`
    on miss/stale. Writers (`_save_cache`) clear `_READ_CACHE_VERIFIED_AT`
    so fresh writes re-validate on the next call.

- **Cross-module cache invalidation** when writer + reader are in
  parallel modules:
  - `pipeline/upload/upload.py::_mirror_record_to_gcs` writes via
    `from control import storage as _gcs`. The reader (dashboard) lives
    in `control.core.storage`. After every successful mirror the writer
    explicitly imports `control.core.storage` and calls
    `bust_upload_records_cache()`. Best-effort with a try/except for
    older deploys without the hook.

- **Per-channel parallelism for any handler that loops over the
  channel registry**:
  - `pipeline/schemas/customization.py::list_channels` —
    `ThreadPoolExecutor` over `CHANNEL_REGISTRY`.
  - `get_channel(key)` — resolves one entry instead of building all 8
    and filtering.

- **Don't call full handlers when one tile only needs one tuple**:
  - `web/server.py::dashboard_summary` — `top_performer` no longer
    awaits `control.routes.dashboard_routes.dashboard_videos`; it
    imports `_enumerate_uploads` + `_ensure_fresh` + `_STATS_CACHE`
    directly, scans the cached records, picks the single `(views,
    rec, account, vid)` tuple. Cuts ~30 lines of unrelated per-channel
    aggregation off the hot path.

- **In-process TTL cache for "no-cache" endpoints whose work is too
  expensive to repeat per-tab**:
  - `control/routes/cloud_routes.py::cloud_health` — 5 s cache.
    Health is volatile, but multi-tab × 30 s poll cadence multiplies
    the work. 5 s is shorter than any meaningful service-state change
    and collapses N-tab × M-poll work to one sweep.

- **Performance middleware** — `web/server.py::_perf_headers_middleware`:
  - `Server-Timing: total;dur=<ms>` on every response (handlers may
    pre-populate sub-stages and the middleware appends `total` last).
  - `Cache-Control: private, max-age=10, stale-while-revalidate=60`
    on the `_CACHEABLE_GET_PREFIXES` set: `/api/dashboard`,
    `/api/channels`, `/api/cloud/health`, `/api/cloud/cost`,
    `/api/cloud/deploys`, `/api/cloud/services`, `/api/queue`,
    `/api/voices`, `/api/niches`. **`private`** (not `public`) so
    auth-gated payloads can't be served to a different user by an
    intermediate proxy. **`Vary: Cookie`** is appended so per-user
    scoping is explicit.

### Client side (`ytfactory-web-next`)

- **`web-next/lib/use-swr-cache.ts::useStaleWhileRevalidate`** — drop-in
  for `useState + useVisiblePoll(setData(await api.get...), N)`. Lazy-
  hydrates from `sessionStorage` on mount so navigation repaints are
  instant. Visibility-aware poll (uses `useVisiblePoll` under the
  hood). Errors never clear the stale payload — caller renders the
  cached data with an error chip.

- **Pages migrated** (so far): `/app` (dashboard), `/app/channels`.
  Other polled pages should migrate when next touched — see "How to
  add a new dashboard endpoint" below.

- **30 s poll cadence** for dashboard-class data. The dashboard is a
  snapshot, not a live console; cron jobs ship every few minutes;
  sub-second freshness isn't worth the GCS waterfall it triggers.
  Queue-class data (in-flight render state) keeps shorter cadences.

## How to add a new dashboard endpoint (the new contract)

When adding `GET /api/foo` that the dashboard polls:

1. **Backend**: cache the heavy bit in-process with a TTL:
   - In-memory dict `{cache_key: (timestamp, payload)}`.
   - Module-level `threading.Lock`.
   - 30–60 s TTL for read-only enumerations; 5–10 s for "live"
     probes (health, queue depth).
   - Writer-side bust hook for paths where new data MUST appear within
     one poll cycle (uploads, completed jobs).

2. **Backend**: parallelize any I/O fan-out (GCS, Firestore,
   downstream HTTP) with a `ThreadPoolExecutor` (8–16 workers). A
   `for x in remote_iter:` loop over more than ~5 items is the smell.

3. **Backend**: add the path to
   `web/server.py::_CACHEABLE_GET_PREFIXES` so the middleware emits
   `Cache-Control` + `Vary: Cookie`.

4. **Frontend**: wire with `useStaleWhileRevalidate` (NOT raw
   `useVisiblePoll`):
   ```tsx
   const { data, error } = useStaleWhileRevalidate<MyType>(
     "page:foo",                         // sessionStorage key
     () => api.get<MyType>("/api/foo"),
     30_000,                              // poll cadence (ms)
   );
   ```

5. **Verify in DevTools**:
   - Network row → Headers tab → see `Cache-Control: private,
     max-age=10, stale-while-revalidate=60` and
     `Server-Timing: total;dur=<ms>`.
   - Navigate away and back → page paints from sessionStorage instantly,
     then revalidates.

## Anti-patterns (don't repeat)

- **Sequential `download_as_bytes` over a list**. Always pool.
- **Calling a full handler to extract one tuple**. `dashboard_summary`
  used to call `dashboard_videos` for `top_performer` — the entire
  channel-grouped payload was built (per-channel totals, subscribers,
  fetched_at) just so we could pull `(title, thumb, views)`. Fetch the
  primitives, build only what the caller needs.
- **`blob.reload()` (or HEAD) on every cache lookup just to check
  generation**. If the in-process entry is fresh enough (use a TTL),
  serve it without round-tripping. Writers bust the entry explicitly.
- **`Cache-Control: no-store` on read endpoints**. Browsers re-fetch
  on every back/forward + tab-switch. Dashboard-class GETs can safely
  carry `private, max-age=10, stale-while-revalidate=60` because the
  dashboard already polls for freshness.
- **Public `Cache-Control` on auth-gated endpoints**. Use `private`
  + `Vary: Cookie` so an intermediate proxy can't cross-pollinate
  users.
- **`useState` + `useVisiblePoll` directly on a polled page**. Use
  `useStaleWhileRevalidate` so the user gets instant repaint on every
  navigation instead of skeletons every visit.
- **Polling cadence `< 30s` for snapshot data**. Reserve <30 s polls
  for in-flight render state (queue, render-detail). The dashboard
  doesn't need it.

## Verification

DevTools → Network → any `/api/*` row → Timing tab will show the
`total;dur=<ms>` value the server reported, plus the `Cache-Control`
header. Repeat-navigate the same page in the same tab — sessionStorage
SWR repaints instantly while the next poll runs in the background.

## Cross-references

- `docs/dashboard_gcs_records.md` — upload-records mirror layout (the
  data this hot path enumerates).
- `docs/youtube_stats_refresh.md` — per-account YT research cache (now
  has the 60 s `_load_cache` fast-path).
- `docs/cloudrun_admin_panel.md` — `/app/cloud` page that consumes
  `/api/cloud/health` (now 5 s server-cached).
- `pipeline/cloud/health.py::sweep` — already parallel; the new layer
  is the request-level cache around it.
- `web-next/lib/use-swr-cache.ts` — the SWR hook contract.
- `web-next/lib/use-visible-poll.ts` — underlying visibility-aware
  poller (still the primitive; SWR wraps it).
