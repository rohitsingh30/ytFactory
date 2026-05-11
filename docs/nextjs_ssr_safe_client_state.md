# Next.js SSR-safe client state — never read `window` in `useState` lazy init

**Established 2026-05-11** after `web-next/lib/use-swr-cache.ts` shipped
React **#418** ("Hydration failed because the initial UI does not
match what was rendered on the server") and **#422** ("There was an
error while hydrating but React was able to recover by instead client
rendering the entire root") on every page load that had a previous
`sessionStorage` cache entry.

## The bug

`useStaleWhileRevalidate` was reading from `sessionStorage` inside
`useState` **lazy initializers**:

```tsx
// ❌ Hydration mismatch — DO NOT DO THIS
const [data, setData] = useState<T | null>(() => readCache<T>(key));
const [isLoading, setIsLoading] = useState<boolean>(() => readCache<T>(key) === null);
```

Lazy initializers run on **both** the server (SSR) and the first
client render. On the server `window` is `undefined` so `readCache`
returns `null` → SSR renders skeletons. On the client `sessionStorage`
has the prior payload → first client render shows real content. The
two HTML trees diverge → React #418/#422.

The error was silent (minified) until a user looked at the console:

```
Uncaught Error: Minified React error #418; visit https://react.dev/errors/418 …
Uncaught Error: Minified React error #422; visit https://react.dev/errors/422 …
```

The page kept rendering (React's recovery path = client-side re-render
of the entire root) so the symptom was "page works but flickers and
loses focus / scroll / form state on every navigation".

## The rule

**Never read `window`, `document`, `localStorage`, `sessionStorage`,
`navigator`, or any other browser-only API inside a `useState` lazy
initializer or directly in the render body of an SSR'd component.**
Defer all browser-only reads to `useEffect` (which only runs on the
client, after hydration completes).

## The fix shape

```tsx
// ✓ SSR-safe
export function useStaleWhileRevalidate<T>(key, fetcher, intervalMs) {
  const [data, setData] = useState<T | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const cached = readCache<T>(key);
    if (cached !== null) {
      setData(cached);
      setIsLoading(false);
    }
  }, [key]);

  // ... rest unchanged
}
```

Cost: **one paint of the loading skeleton** before the cached payload
appears (typically <1 frame on a warm machine). Worth it — the
alternative is broken hydration on every cached visit, which loses
focus / scroll / form input across the page.

## When you really do want zero-flash

Two acceptable workarounds when the skeleton flash is unacceptable
(e.g. above-the-fold critical UI):

1. **Server-render with the cached value too.** If the data lives in
   a cookie or a `searchParams` query, both server and client have
   access — render with the same value on both sides. No mismatch.
2. **Render a "mounted" gate.** Use a `mounted` flag + `useEffect`
   to set it to true; render `null` (or a stable placeholder) until
   then. The first paint is a no-op, the second paint shows the
   client value. No mismatch.

```tsx
// Pattern 2 — client-only gate
const [mounted, setMounted] = useState(false);
useEffect(() => { setMounted(true); }, []);
if (!mounted) return null;  // SSR + first hydration paint
return <div>{readFromWindow()}</div>;  // client paints take over
```

`useSyncExternalStore` is a third option but does NOT magically swap
between server and client snapshots without a mismatch — read the
React docs carefully before reaching for it. `getServerSnapshot` and
`getSnapshot` must return the SAME value during initial hydration.

## How to find existing offenders

```bash
grep -rnE "useState\(\(\)\s*=>.*?(window|document|localStorage|sessionStorage|navigator)" \
    web-next/ --include='*.ts' --include='*.tsx' \
  | grep -v "node_modules\|\.next/"
```

Also worth checking — direct browser-API reads in render bodies of
`"use client"` components (less obviously broken because some are
guarded by `typeof window !== 'undefined'`):

```bash
grep -rnE "(localStorage|sessionStorage|window\.)" \
    web-next/components/ web-next/app/ --include='*.tsx' \
  | grep -v "useEffect\|node_modules\|\.next/"
```

Each match is a candidate — read the surrounding lines to confirm
whether it's inside `useEffect` (safe) or render body (mismatch
hazard).

## Why this rule isn't obvious

The Next.js docs cover SSR/CSR mismatch but bury the `useState` lazy
init pitfall in scattered React-18-strict-mode notes. The pattern is
seductive because:

- It looks like the right place for one-time init ("only runs on
  mount").
- It's faster than `useEffect` (synchronous on the first render).
- TypeScript doesn't flag it.
- The bug is silent until production minification (#418/#422 are
  minified-only error codes; dev mode prints the actual mismatch).

The stack-trace also points at React internals
(`fd9d1056-99c4763920dd157f.js`) not the offending hook, so the
diagnostic loop is "I see a hydration error, where is it coming
from?" → grep for the patterns above.

## Related rules

- [`feedback_browser_swr_for_polled_pages.md`](../memory/feedback_browser_swr_for_polled_pages.md) —
  the dashboard SWR pattern itself (correctness rule). Was previously
  documented as "hydrates synchronously on mount"; corrected after
  this incident to "hydrates in `useEffect` on mount".
- [`web_perf_pass_2026_05_10.md`](./web_perf_pass_2026_05_10.md) §
  "Browser SWR" — same correction.
- [`web_next_dev_stale_build_guard.md`](./web_next_dev_stale_build_guard.md) —
  unrelated dev-mode bug that LOOKS like #418/#422 (both produce
  hydration errors on the client). Triage the two by checking the
  Next dev log first: if you see `_next/static/chunks/*.js 404`,
  it's the dev-stale-build bug. If chunks load 200 OK, it's this
  hydration-mismatch bug.

## Reference

- Fix: `web-next/lib/use-swr-cache.ts` — `useState(null)` + `useEffect`
- React docs: https://react.dev/errors/418, https://react.dev/errors/422
- Memory: `feedback_nextjs_ssr_lazy_init_mismatch.md`
