"use client";

/**
 * Stale Next.js shell + service-worker recovery guard.
 *
 * Two recovery jobs that run on every page mount:
 *
 * 1. **Stale chunk reload.** After a deploy, browsers caching a
 *    previous HTML shell will request chunk URLs
 *    (`/_next/static/chunks/...js`) that 404 because the chunk
 *    hashes have rotated. Without a guard, the page partially
 *    loads, hydration fails silently, and any client-driven UI
 *    (e.g. the /login spinner clearing on whoami response) never
 *    runs. We listen for chunk-load errors at the browser's event
 *    surface (capture-phase `error` event on `window` plus
 *    `unhandledrejection`) and, on the FIRST chunk-load failure,
 *    force a single hard reload.
 *
 * 2. **Service-worker self-destruct.** The previous studio SW
 *    (public/sw.js) intercepted /api/* GETs with scope "/" and
 *    survived deploys. In some failure modes it served stale HTML
 *    that no longer matched the currently-deployed chunk hashes,
 *    trapping users on a "stuck checking session" spinner that
 *    even hard-refresh couldn't clear (an active SW intercepts
 *    every navigation; only `unregister()` recovers). We now
 *    proactively iterate every registration and unregister + wipe
 *    every cache entry on every page mount. The replacement
 *    public/sw.js is itself a kill switch — any browser that has
 *    the old SW will fetch the new sw.js on its next navigation
 *    (browsers re-validate the SW script every navigation, with
 *    Cache-Control: no-store enforced server-side), see the
 *    self-destruct activate handler, unregister itself, and force
 *    every controlled tab to navigate. The cleanup here is
 *    belt-and-braces for tabs whose last navigation pre-dates the
 *    kill SW and for browsers that don't auto-re-validate.
 *
 * Idempotent on both axes — `__yt_reload_attempted` prevents
 * infinite reload loops; `__yt_sw_cleanup_done` prevents the
 * unregister sweep from running more than once per tab.
 */
import { useEffect } from "react";

declare global {
  interface Window {
    __yt_reload_attempted?: boolean;
    __yt_sw_cleanup_done?: boolean;
  }
}

const CHUNK_LOAD_PATTERNS = [
  /Loading chunk \d+ failed/i,
  /Loading CSS chunk \d+ failed/i,
  /ChunkLoadError/i,
  /Failed to fetch dynamically imported module/i,
];

function looksLikeChunkLoadError(message: unknown): boolean {
  if (typeof message !== "string") return false;
  return CHUNK_LOAD_PATTERNS.some((re) => re.test(message));
}

async function unregisterAllServiceWorkers(): Promise<void> {
  if (typeof window === "undefined") return;
  if (window.__yt_sw_cleanup_done) return;
  window.__yt_sw_cleanup_done = true;

  if (!("serviceWorker" in navigator)) return;

  // Wipe every cache entry FIRST (any name — accidentally-installed
  // workbox / next-pwa caches survive an unregister otherwise).
  try {
    if (typeof caches !== "undefined") {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
    }
  } catch {
    // best-effort
  }

  // Unregister every active SW. After this, no new requests route
  // through any worker for this origin.
  try {
    const regs = await navigator.serviceWorker.getRegistrations();
    if (regs.length) {
      await Promise.all(regs.map((r) => r.unregister().catch(() => false)));
      // Tell anyone listening that we just nuked the SW; useful for
      // dashboards that want to refetch state.
      try {
        sessionStorage.setItem(
          "yt:sw-uninstalled",
          new Date().toISOString(),
        );
      } catch {
        // best-effort
      }
    }
  } catch {
    // best-effort
  }
}

export function StaleShellGuard() {
  useEffect(() => {
    if (typeof window === "undefined") return;

    // Run SW cleanup once per tab on mount. This is the recovery
    // path for users whose browsers still have the previous SW
    // active. It races with the kill SW the browser will install
    // when it re-validates /sw.js — whichever fires first wins; both
    // are safe to run.
    void unregisterAllServiceWorkers();

    if (window.__yt_reload_attempted) return;

    const tryReload = (reason: string) => {
      if (window.__yt_reload_attempted) return;
      window.__yt_reload_attempted = true;
      try {
        sessionStorage.setItem("yt:stale-shell-reload", reason);
      } catch {
        // sessionStorage may be unavailable (private mode in some
        // browsers); the in-memory flag still prevents the loop.
      }
      console.warn(
        `[stale-shell] chunk load failed (${reason}); forcing reload`,
      );
      const url = new URL(window.location.href);
      url.searchParams.set("__yt_reload", String(Date.now()));
      window.location.replace(url.toString());
    };

    let alreadyReloaded = false;
    try {
      alreadyReloaded = sessionStorage.getItem("yt:stale-shell-reload") !== null;
    } catch {
      alreadyReloaded = false;
    }
    if (alreadyReloaded) {
      try {
        sessionStorage.removeItem("yt:stale-shell-reload");
      } catch {
        // ignore
      }
      window.__yt_reload_attempted = true;
      return;
    }

    const onError = (ev: ErrorEvent) => {
      const target = ev.target as HTMLScriptElement | HTMLLinkElement | null;
      if (target && (target.tagName === "SCRIPT" || target.tagName === "LINK")) {
        const src =
          (target as HTMLScriptElement).src ||
          (target as HTMLLinkElement).href ||
          "";
        if (src.includes("/_next/static/chunks/")) {
          tryReload(`asset ${src}`);
          return;
        }
      }
      if (looksLikeChunkLoadError(ev.message)) {
        tryReload(`window.error: ${ev.message}`);
      }
    };

    const onRejection = (ev: PromiseRejectionEvent) => {
      const reason = ev.reason;
      const message =
        typeof reason === "string"
          ? reason
          : (reason && typeof reason === "object" && "message" in reason
              ? String((reason as { message?: unknown }).message)
              : String(reason));
      if (looksLikeChunkLoadError(message)) {
        tryReload(`unhandledrejection: ${message}`);
      }
    };

    window.addEventListener("error", onError, true);
    window.addEventListener("unhandledrejection", onRejection);

    return () => {
      window.removeEventListener("error", onError, true);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  return null;
}
