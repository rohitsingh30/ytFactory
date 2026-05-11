# Service worker handler scope — `event` does not survive helper extraction

> **Cross-channel rule (CLASS-OF-BUG, 2026-05-11).** Any helper
> extracted from a service-worker `install`/`activate`/`fetch`/`message`
> event handler MUST take `event` as an explicit parameter if it calls
> `event.waitUntil(...)` / `event.respondWith(...)` / accesses any
> `event.*` field. Service workers have NO module-level `event` to
> fall back to — there is no closure, there is no globalThis.event.
> The failure mode is silent in dev and **catastrophic** in prod
> (every cached fetch surfaces as `ERR_FAILED` to the caller).

## What broke (2026-05-11)

`web-next/public/sw.js` shipped with this signature mismatch:

```js
self.addEventListener("fetch", (event) => {
  // ...
  event.respondWith(staleWhileRevalidate(req));   // event not passed
});

async function staleWhileRevalidate(req) {        // no event param
  // ...
  if (cached) {
    if (age < MAX_AGE_MS) {
      event.waitUntil(networkPromise.catch(...)); // ReferenceError
      return cached;
    }
  }
}
```

Every cached `/api/*` GET threw `ReferenceError: event is not defined`
from inside the helper. Because the throw happened during the promise
chain handed to `event.respondWith(...)`, the SW's response promise
rejected — and the browser surfaces a SW response rejection as
`net::ERR_FAILED` for the entire request, NOT a network fallback.

User-visible blast radius (every tab that had loaded the studio at
least once before, since the SW persists across reloads):

```
GET /api/channels                        → ERR_FAILED
GET /api/queue                           → ERR_FAILED
GET /api/dashboard                       → ERR_FAILED
GET /api/channels/<slug>/avatar.jpg      → ERR_FAILED
sw.js:118 Uncaught (in promise) ReferenceError: event is not defined
```

The dashboard appears entirely broken even though the FastAPI backend
is healthy. Network panel shows `(ServiceWorker)` as the source on
every failed row.

## Fix

Two changes, both required:

1. **Pass `event` into the helper signature.** `event` lives on the
   stack of the handler invocation — it does not survive a function
   call without being threaded through.

   ```js
   event.respondWith(staleWhileRevalidate(event, req));

   async function staleWhileRevalidate(event, req) {
     // ...
     event.waitUntil(networkPromise.catch(() => undefined));
   }
   ```

2. **Bump `CACHE_VERSION` (e.g. `v1 → v2`).** The activate handler
   already drops any cache whose name doesn't match the current
   version, so a version bump guarantees the broken SW's poisoned /
   half-populated cache is wiped on next activate. Do this **even if
   the cache schema is unchanged** — the goal is to invalidate any
   state the buggy version may have written.

## Why `event.waitUntil` matters (don't replace it with bare promises)

`event.respondWith(p)` keeps the SW alive only until `p` settles.
Background work that outlives the response (the network revalidation
in stale-while-revalidate) needs `event.waitUntil(bgPromise)` to
extend the SW's lifetime — otherwise Chrome may terminate the SW
before the cache write completes. So you can't fix this by deleting
the `event.waitUntil` call; you must thread `event` through.

## Standing rules for any future SW change

- **`event` flows by parameter, not by capture.** Every helper that
  touches `event.*` takes it as an arg.
- **Bump `CACHE_VERSION` on every SW behavior change.** v1 → v2 → v3
  whenever the handler logic changes. Cheap insurance.
- **Verify the deploy with curl + a fresh tab.**
  ```bash
  curl -s https://<host>/sw.js | grep -nE "CACHE_VERSION|event\.waitUntil"
  ```
  Then open DevTools → Application → Service Workers → confirm the
  new SW is "activated and is running" (not "redundant").
- **Kill switch:** any `/app/*` URL with `?nosw=1` unregisters every
  registered SW + clears every `ytfactory-api-*` cache (see
  `web-next/components/app/studio-sw-register.tsx`). Document this in
  the rollback row of any SW-touching change so a stuck user has a
  one-URL escape hatch.

## Sweep recipe (run before shipping any SW change)

```bash
# Every event.* reference inside sw.js — confirm each one is inside
# its handler's arg scope, NOT inside a helper that doesn't receive it.
grep -nE "event\.(waitUntil|respondWith|request|data|clientId|clients)" \
  web-next/public/sw.js

# Helpers that look like they might have lost event:
grep -nE "^(async )?function " web-next/public/sw.js
```

If any helper-defined-at-top-level uses `event.*` without `event` in
its signature, that's the bug.

## Cross-references

- [`web-next/public/sw.js`](../web-next/public/sw.js) — the SW itself.
- [`web-next/components/app/studio-sw-register.tsx`](../web-next/components/app/studio-sw-register.tsx)
  — the registration + `?nosw=1` kill-switch.
- [`docs/web_perf_pass_2026_05_11.md`](./web_perf_pass_2026_05_11.md)
  § Network-layer SWR — the wider perf pass that introduced the SW.
