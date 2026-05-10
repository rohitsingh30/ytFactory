// Stale-while-revalidate hook with sessionStorage backing.
//
// Replaces the common pattern:
//
//   const [data, setData] = useState<T | null>(null);
//   useVisiblePoll(async () => setData(await api.get(...)), 30_000);
//
// with:
//
//   const { data, error } = useStaleWhileRevalidate<T>(
//     "dashboard:summary",                       // sessionStorage key
//     () => api.get<T>("/api/dashboard"),       // fetcher
//     30_000,                                    // refresh cadence (ms)
//   );
//
// Behaviour:
//
//   - On mount, synchronously hydrate state from sessionStorage if a
//     prior render of this key cached anything. The page paints
//     instantly with stale data instead of skeletons every navigation.
//   - Then fire the fetcher in the background. On success, update
//     state + sessionStorage; on failure surface the error but keep
//     the stale data on screen.
//   - Visibility-aware poll cadence (uses useVisiblePoll under the
//     hood) so background tabs don't burn server cycles.
//   - "key" is namespaced per-tab (sessionStorage), not cross-tab, so
//     two open studio tabs don't fight each other.
//
// Failure model: a network error never clears the stale payload —
// callers see (data: <stale>, error: <Error>). The error chip on the
// page surfaces it so the user knows the timestamp is old, but the
// content stays on screen so the studio remains usable through a
// transient backend hiccup.

import { useEffect, useRef, useState } from "react";
import { useVisiblePoll } from "./use-visible-poll";

interface CachedEntry<T> {
  v: 1;
  ts: number;
  payload: T;
}

const NAMESPACE = "ytfactory:swr:";

function readCache<T>(key: string): T | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(NAMESPACE + key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedEntry<T>;
    if (parsed?.v !== 1) return null;
    return parsed.payload ?? null;
  } catch {
    return null;
  }
}

function writeCache<T>(key: string, payload: T): void {
  if (typeof window === "undefined") return;
  try {
    const entry: CachedEntry<T> = { v: 1, ts: Date.now(), payload };
    window.sessionStorage.setItem(NAMESPACE + key, JSON.stringify(entry));
  } catch {
    // sessionStorage full / disabled — silently skip; the in-memory
    // state is still authoritative for this tab.
  }
}

export interface UseStaleWhileRevalidateResult<T> {
  data: T | null;
  error: Error | null;
  isLoading: boolean;
  refresh: () => void;
}

export function useStaleWhileRevalidate<T>(
  key: string,
  fetcher: () => Promise<T>,
  intervalMs: number,
): UseStaleWhileRevalidateResult<T> {
  // Hydrate from sessionStorage on the FIRST render. We use lazy init
  // so the JSON parse only runs once, not on every re-render.
  const [data, setData] = useState<T | null>(() => readCache<T>(key));
  const [error, setError] = useState<Error | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(() => readCache<T>(key) === null);

  // Stable fetcher ref so changing closures don't restart polling.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  // useVisiblePoll handles the cadence + tab-visibility pause/resume.
  useVisiblePoll(async () => {
    try {
      const next = await fetcherRef.current();
      setData(next);
      writeCache(key, next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setIsLoading(false);
    }
  }, intervalMs, [key]);

  // Manual refresh (e.g. for a "Refresh" button) — same semantics as a
  // poll tick but on demand.
  const refreshFn = useRef<() => void>(() => undefined);
  refreshFn.current = () => {
    fetcherRef.current()
      .then((next) => {
        setData(next);
        writeCache(key, next);
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
  };
}
