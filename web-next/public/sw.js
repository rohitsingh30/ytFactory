// ytFactory studio service worker.
//
// Provides stale-while-revalidate at the network layer for the
// dashboard-class /api/* GETs. Survives reload, browser restart, AND
// offline — complementing the in-process useStaleWhileRevalidate
// hook (which only survives within a single tab session).
//
// Strategy:
//   - Only intercept GETs to /api/*
//   - Skip auth-sensitive endpoints (whoami, admin/*) — they MUST be
//     fresh for security/UX correctness
//   - For everything else: respond from cache immediately if any,
//     fetch in background, update cache on success
//   - Skip non-2xx responses (don't cache 401/403/500/etc.)
//   - Versioned cache name — bump CACHE_VERSION to invalidate
//
// Kill switch: hit any /app/* URL with `?nosw=1` and the registration
// hook in studio-sw-register.tsx will unregister this worker. Useful
// for triaging "stale data" reports.
//
// IMPORTANT: this file lives in web-next/public/ and is served at
// the path /sw.js. It is loaded as a SW, NOT as a Next.js module —
// it cannot import from anywhere. Keep it self-contained.

const CACHE_VERSION = "v2";
const CACHE_NAME = `ytfactory-api-${CACHE_VERSION}`;
const MAX_ENTRIES = 200;
const MAX_AGE_MS = 60 * 60 * 1000; // 1 hour

// Endpoints that MUST always hit the network — no cache, no SWR.
// Auth + admin = correctness > speed.
const SKIP_PREFIXES = [
  "/api/auth/",
  "/api/admin/",
  "/api/oauth/",
  "/api/jobs/", // creating + polling jobs needs freshness
  "/api/critiques/", // POSTs but also fresh GETs
];

self.addEventListener("install", (event) => {
  // Activate the new SW immediately rather than waiting for all old
  // tabs to close — important so a hot fix doesn't sit dormant.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Drop any caches from prior CACHE_VERSION.
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((n) => n.startsWith("ytfactory-api-") && n !== CACHE_NAME)
          .map((n) => caches.delete(n)),
      );
      // Take control of any open studio tabs immediately.
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  // Same-origin only. Cross-origin (yt3.ggpht.com, etc.) bypasses.
  if (url.origin !== self.location.origin) return;
  // Only the API surface — Next.js _next/* assets are already
  // cache-busted via content hashing.
  if (!url.pathname.startsWith("/api/")) return;
  // Skip auth-sensitive paths.
  if (SKIP_PREFIXES.some((p) => url.pathname.startsWith(p))) return;
  // Skip explicit no-store hints (callers that mark themselves uncachable).
  if (req.headers.get("Cache-Control") === "no-store") return;

  event.respondWith(staleWhileRevalidate(event, req));
});

async function staleWhileRevalidate(event, req) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(req);

  // Always kick off the network revalidation in the background.
  const networkPromise = fetch(req)
    .then(async (res) => {
      if (res && res.ok && res.status === 200) {
        // Stamp a timestamp header so the cache reader can age out.
        const clone = res.clone();
        const body = await clone.blob();
        const headers = new Headers(res.headers);
        headers.set("x-sw-cached-at", String(Date.now()));
        const stamped = new Response(body, {
          status: res.status,
          statusText: res.statusText,
          headers,
        });
        await cache.put(req, stamped);
        // Best-effort cache-size cap: keep the most recent MAX_ENTRIES.
        await trimCache(cache, MAX_ENTRIES);
      }
      return res;
    })
    .catch((err) => {
      // Network failed — surface the cached response if we have it,
      // otherwise let the error propagate so the page can show its
      // own offline UX.
      if (cached) return cached;
      throw err;
    });

  if (cached) {
    const ageHeader = cached.headers.get("x-sw-cached-at");
    const age = ageHeader ? Date.now() - Number(ageHeader) : Infinity;
    if (age < MAX_AGE_MS) {
      // Fire-and-forget the revalidation; serve the stale-but-fresh-enough
      // copy now.
      event.waitUntil(networkPromise.catch(() => undefined));
      return cached;
    }
    // Cached but too old — race the network and the stale copy. Whoever
    // wins serves first; if network resolves we use that, else stale.
    return networkPromise.catch(() => cached);
  }

  return networkPromise;
}

async function trimCache(cache, maxEntries) {
  const keys = await cache.keys();
  if (keys.length <= maxEntries) return;
  // Drop the oldest (FIFO — keys() is insertion-order).
  const toDelete = keys.length - maxEntries;
  for (let i = 0; i < toDelete; i++) {
    await cache.delete(keys[i]);
  }
}

// Allow the page to bust the cache imperatively (e.g. after logout).
self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "BUST_CACHE") {
    event.waitUntil(caches.delete(CACHE_NAME));
  }
});
