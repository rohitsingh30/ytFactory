"use client";

/**
 * Stale Next.js shell guard.
 *
 * After a deploy, browsers caching a previous HTML shell will
 * request chunk URLs (`/_next/static/chunks/...js`) that 404
 * because the chunk hashes have rotated. Without a guard, the
 * page partially loads, hydration fails silently, and any client-
 * driven UI (e.g. the /login spinner clearing on whoami response)
 * never runs.
 *
 * This component listens for chunk-load errors at the browser's
 * event surface (capture-phase `error` event on `window` plus
 * `unhandledrejection`) and, on the FIRST chunk-load failure,
 * forces a single hard reload (`location.reload()` with cache
 * bust). The reload re-fetches the up-to-date HTML shell which
 * carries the correct chunk hashes.
 *
 * Idempotent: the in-page `__yt_reload_attempted` flag prevents an
 * infinite reload loop if the chunk genuinely doesn't exist on the
 * server (e.g. operator rolled back mid-session — second failure
 * surfaces as a normal Next.js error boundary).
 *
 * Why client-side detection is sufficient: the underlying issue is
 * always "browser HTML references chunks that no longer exist." A
 * single forced reload re-syncs the browser to the live deploy.
 * Server-side mitigations (force-dynamic on the login layout,
 * cache-control no-store) prevent NEW visits from hitting the
 * trap; this guard recovers EXISTING tabs.
 */
import { useEffect } from "react";

declare global {
  interface Window {
    __yt_reload_attempted?: boolean;
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

export function StaleShellGuard() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (window.__yt_reload_attempted) return;

    const tryReload = (reason: string) => {
      if (window.__yt_reload_attempted) return;
      window.__yt_reload_attempted = true;
      // Use sessionStorage so a reload-after-reload is detectable on
      // the next mount and we don't re-trigger.
      try {
        sessionStorage.setItem("yt:stale-shell-reload", reason);
      } catch {
        // sessionStorage may be unavailable (private mode in some
        // browsers); the in-memory flag still prevents the loop
        // within this tab.
      }
      console.warn(
        `[stale-shell] chunk load failed (${reason}); forcing reload`,
      );
      // Bust HTTP cache by appending a timestamp the same way Next
      // does for its own chunk-load retries.
      const url = new URL(window.location.href);
      url.searchParams.set("__yt_reload", String(Date.now()));
      window.location.replace(url.toString());
    };

    // Bail out if we just reloaded — second failure is real, not a
    // stale-shell symptom.
    let alreadyReloaded = false;
    try {
      alreadyReloaded = sessionStorage.getItem("yt:stale-shell-reload") !== null;
    } catch {
      alreadyReloaded = false;
    }
    if (alreadyReloaded) {
      // Clear the breadcrumb so a future genuine stale-shell event
      // (after a fresh deploy) can still recover.
      try {
        sessionStorage.removeItem("yt:stale-shell-reload");
      } catch {
        // ignore
      }
      window.__yt_reload_attempted = true;
      return;
    }

    const onError = (ev: ErrorEvent) => {
      // Script-element load failures fire on the capture phase with
      // ev.target === <script>; chunk-load promise rejections
      // surface via the message string.
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
