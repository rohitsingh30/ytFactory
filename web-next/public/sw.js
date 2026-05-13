// SW self-destruct (2026-05-13).
//
// Replaces the previous stale-while-revalidate SW that trapped users
// on a stuck checking session spinner after deploys. install() does
// skipWaiting; activate() takes control of every open tab, deletes
// every cache, unregisters this registration, and forces every tab
// to hard-navigate so they pick up the live HTML directly. fetch()
// is pass-through. The registration call site was removed in the
// same commit so new visits never install a SW.

self.addEventListener("install", (event) => { self.skipWaiting(); });

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    try { await self.clients.claim(); } catch {}
    try {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
    } catch {}
    try { await self.registration.unregister(); } catch {}
    try {
      const list = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const c of list) {
        try { await c.navigate(c.url); }
        catch { try { c.postMessage({ type: "YTFACTORY_SW_REPLACED" }); } catch {} }
      }
    } catch {}
  })());
});

// Pass-through fetch: NO event.respondWith - browser default applies.
self.addEventListener("fetch", (event) => {});

self.addEventListener("message", (event) => {
  const t = event && event.data && event.data.type;
  if (t === "BUST_CACHE" || t === "FORCE_UNREGISTER") {
    event.waitUntil((async () => {
      try {
        const names = await caches.keys();
        await Promise.all(names.map((n) => caches.delete(n)));
      } catch {}
      try { await self.registration.unregister(); } catch {}
    })());
  }
});
