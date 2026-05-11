// Stale-while-revalidate primitive for the studio.
//
// One module-level cache backs every page. Three storage tiers, in
// priority order on every read:
//
//   1. In-memory Map         — instant, no JSON parse, request-deduped
//   2. localStorage          — survives reload + new tab + browser close
//                              (was sessionStorage in v1; v2 promotes
//                              persistence so the FIRST repaint after a
//                              hard refresh is instant too)
//   3. The fetcher           — only fired if the cached entry is older
//                              than `intervalMs / 2` OR missing
//
// Cross-cutting features:
//
//   - **In-flight dedup**: if two components mount with the same key in
//     the same tick, only one network call fires.
//   - **Cross-tab sync via the `storage` event**: when tab A refetches,
//     tab B's hook updates instantly without its own fetch.
//   - **`prime(key, fetcher)`**: imperative warm-fetch, used by the app
//     shell (mount-time prime of all sidebar destinations) and by the
//     sidebar Link hover prefetcher. Idempotent — if the cache is fresh
//     (< maxAgeMs) it skips the network entirely.
//   - **`getCached(key)`**: synchronous snapshot read, no subscription.
//   - **Subscribers**: the hook subscribes to its key. Any setCached()
//     from any source (poll, refresh, prime, cross-tab) notifies all
//     mounted hooks so they re-render with fresh data.
//
// Failure model unchanged from v1: a network error never clears the
// stale payload — callers see (data: <stale>, error: <Error>) and decide
// whether to flag the staleness in the UI.
//
// Versioning: cached entries carry `v: 2`. v1 sessionStorage entries
// from the previous deploy are silently ignored (not migrated — the
// keys rebuild on the first poll cycle).

import { useEffect, useRef, useState } from "react";
import { useVisiblePoll } from "./use-visible-poll";

interface CachedEntry<T> {
  v: 2;
  ts: number;
  payload: T;
}

const NAMESPACE = "ytfactory:swr:";
const STORAGE_VERSION = 2;

// Module-level state shared across all hook instances + imperative
// prime() / getCached() callers.
const memoryCache = new Map<string, CachedEntry<unknown>>();
const inflight = new Map<string, Promise<unknown>>();
const subscribers = new Map<string, Set<(payload: unknown) => void>>();

let storageListenerInstalled = false;

function installStorageListener(): void {
  if (storageListenerInstalled || typeof window === "undefined") return;
  storageListenerInstalled = true;
  window.addEventListener("storage", (ev) => {
    if (!ev.key || !ev.key.startsWith(NAMESPACE)) return;
    const key = ev.key.slice(NAMESPACE.length);
    if (ev.newValue == null) {
      memoryCache.delete(key);
      notify(key, null);
      return;
    }
    try {
      const parsed = JSON.parse(ev.newValue) as CachedEntry<unknown>;
      if (parsed?.v !== STORAGE_VERSION) return;
      memoryCache.set(key, parsed);
      notify(key, parsed.payload);
    } catch {
      // Corrupt entry — ignore. Next local fetch will overwrite.
    }
  });
}

function readLocalStorage<T>(key: string): CachedEntry<T> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(NAMESPACE + key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedEntry<T>;
    if (parsed?.v !== STORAGE_VERSION) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeLocalStorage<T>(key: string, entry: CachedEntry<T>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(NAMESPACE + key, JSON.stringify(entry));
  } catch {
    // localStorage full / disabled / quota exceeded. The in-memory
    // cache is still authoritative for this tab.
  }
}

function notify(key: string, payload: unknown): void {
  const set = subscribers.get(key);
  if (!set) return;
  for (const cb of set) {
    try {
      cb(payload);
    } catch {
      // Subscriber threw — keep notifying the rest.
    }
  }
}

/** Read the cached payload for a key, in-memory first, then localStorage. */
export function getCached<T>(key: string): T | null {
  const mem = memoryCache.get(key) as CachedEntry<T> | undefined;
  if (mem) return mem.payload;
  const ls = readLocalStorage<T>(key);
  if (ls) {
    memoryCache.set(key, ls as CachedEntry<unknown>);
    return ls.payload;
  }
  return null;
}

/** Read the timestamp (ms epoch) the cached entry was written, or null. */
export function getCachedTs(key: string): number | null {
  const mem = memoryCache.get(key);
  if (mem) return mem.ts;
  const ls = readLocalStorage(key);
  if (ls) {
    memoryCache.set(key, ls);
    return ls.ts;
  }
  return null;
}

/**
 * Write a payload into the cache. Updates all three tiers AND notifies
 * mounted hook subscribers. Used by the hook's poll callback and by
 * `prime()`. Callers may use it directly to seed cache from a known
 * source (e.g. an SSR initial render).
 */
export function setCached<T>(key: string, payload: T): void {
  const entry: CachedEntry<T> = { v: STORAGE_VERSION, ts: Date.now(), payload };
  memoryCache.set(key, entry as CachedEntry<unknown>);
  writeLocalStorage(key, entry);
  notify(key, payload);
}

/**
 * Imperatively warm-fetch a key. If the cache is fresher than
 * `maxAgeMs`, returns the cached payload without firing the network.
 * If a fetch is already in flight for this key, returns the in-flight
 * promise (request dedup). Otherwise fires `fetcher`, populates the
 * cache, returns the payload.
 *
 * Used by:
 *   - the app-shell warm-fetch (mounts → primes all sidebar dests)
 *   - the sidebar Link hover prefetcher
 *   - any caller that wants to ensure a key is hot
 */
export async function prime<T>(
  key: string,
  fetcher: () => Promise<T>,
  maxAgeMs = 30_000,
): Promise<T> {
  const ts = getCachedTs(key);
  if (ts !== null && Date.now() - ts < maxAgeMs) {
    return getCached<T>(key) as T;
  }
  const existing = inflight.get(key);
  if (existing) return existing as Promise<T>;
  const promise = (async () => {
    try {
      const payload = await fetcher();
      setCached(key, payload);
      return payload;
    } finally {
      inflight.delete(key);
    }
  })();
  inflight.set(key, promise);
  return promise;
}

/** Subscribe to cache updates for a key. Returns an unsubscribe fn. */
function subscribe<T>(key: string, cb: (payload: T) => void): () => void {
  installStorageListener();
  let set = subscribers.get(key);
  if (!set) {
    set = new Set();
    subscribers.set(key, set);
  }
  set.add(cb as (payload: unknown) => void);
  return () => {
    const s = subscribers.get(key);
    if (!s) return;
    s.delete(cb as (payload: unknown) => void);
    if (s.size === 0) subscribers.delete(key);
  };
}

export interface UseStaleWhileRevalidateResult<T> {
  data: T | null;
  error: Error | null;
  isLoading: boolean;
  refresh: () => void;
  /** ms epoch when data was last successfully fetched, or null. */
  lastUpdatedAt: number | null;
}

export interface UseStaleWhileRevalidateOptions {
  /**
   * If the cached entry is fresher than this, skip the immediate
   * fetch on mount. Defaults to `intervalMs / 2`. Set to `0` to always
   * refetch on mount (the v1 behaviour).
   */
  freshForMs?: number;
  /**
   * If false, the poll never fires. The hook still hydrates from cache
   * on mount (so a returning user sees instant data) and still
   * subscribes to cache updates from other sources (other components,
   * cross-tab `storage` events, manual setCached() calls). Used by
   * pages that have a precondition before they should query — e.g.
   * /app/admin only fetches admin endpoints once the whoami response
   * confirms is_admin.
   */
  enabled?: boolean;
}

export function useStaleWhileRevalidate<T>(
  key: string,
  fetcher: () => Promise<T>,
  intervalMs: number,
  options: UseStaleWhileRevalidateOptions = {},
): UseStaleWhileRevalidateResult<T> {
  // Hydrate from cache on the FIRST render so the page paints stale
  // data instantly. Lazy init so the read only runs once per mount.
  const [data, setData] = useState<T | null>(() => getCached<T>(key));
  const [error, setError] = useState<Error | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(() => getCachedTs(key));
  const [isLoading, setIsLoading] = useState<boolean>(() => getCached<T>(key) === null);

  // Stable fetcher ref so changing closures don't restart polling.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  // Subscribe to cache updates — covers cross-tab `storage` events,
  // sibling-component setCached() calls, and prime() warm-fetches
  // resolved by the app shell or hover prefetcher.
  useEffect(() => {
    const unsub = subscribe<T>(key, (payload) => {
      setData(payload);
      setLastUpdatedAt(getCachedTs(key));
      setError(null);
      setIsLoading(false);
    });
    return unsub;
  }, [key]);

  // Visibility-aware poll. The poll's job is to keep `data` fresh; the
  // intervalMs / 2 freshness gate avoids a redundant fetch on mount
  // when the cache is already hot (e.g. app-shell warm-fetch just ran).
  const freshForMs = options.freshForMs ?? Math.max(1_000, Math.floor(intervalMs / 2));
  const enabled = options.enabled ?? true;
  useVisiblePoll(async () => {
    if (!enabled) return;
    const ts = getCachedTs(key);
    if (ts !== null && Date.now() - ts < freshForMs) {
      // Cache is still hot; skip this tick. The hook is already showing
      // the fresh value via the subscribe() path.
      setIsLoading(false);
      return;
    }
    try {
      const next = await prime(key, fetcherRef.current, 0);
      setData(next);
      setLastUpdatedAt(getCachedTs(key));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setIsLoading(false);
    }
  }, intervalMs, [key, freshForMs, enabled]);

  // Manual refresh (e.g. for a "Refresh" button) — always fires the
  // network, ignores the freshness gate.
  const refreshFn = useRef<() => void>(() => undefined);
  refreshFn.current = () => {
    setIsLoading(true);
    fetcherRef
      .current()
      .then((next) => {
        setCached(key, next);
        setData(next);
        setLastUpdatedAt(Date.now());
        setError(null);
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err : new Error(String(err)));
      })
      .finally(() => setIsLoading(false));
  };

  return {
    data,
    error,
    isLoading,
    refresh: () => refreshFn.current(),
    lastUpdatedAt,
  };
}
