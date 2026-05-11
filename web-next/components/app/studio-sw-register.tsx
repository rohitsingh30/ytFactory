"use client";

// Registers the studio service worker (public/sw.js).
//
// Mounted once from /app/layout.tsx, alongside the AppShellWarmer.
// Renders nothing.
//
// Behaviour:
//   - First load on a supported browser → registers /sw.js at root
//     scope. The SW kicks in for the NEXT navigation onwards
//     (current page already loaded its requests).
//   - URL contains `?nosw=1` → unregisters any active SW and clears
//     the API cache. Useful for triaging stale-data reports.
//   - On user logout → call `bustServiceWorkerCache()` from auth
//     code to wipe the SW cache so the next user doesn't see prior
//     payloads. (Safe no-op when no SW.)

import { useEffect } from "react";

export function StudioSwRegister(): null {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;

    // Kill switch — `?nosw=1` unregisters everything and bails.
    const params = new URLSearchParams(window.location.search);
    if (params.get("nosw") === "1") {
      navigator.serviceWorker.getRegistrations().then((regs) => {
        for (const r of regs) void r.unregister();
      });
      caches?.keys().then((names) => {
        for (const n of names) {
          if (n.startsWith("ytfactory-api-")) void caches.delete(n);
        }
      });
      // eslint-disable-next-line no-console
      console.info("[ytfactory-sw] disabled via ?nosw=1");
      return;
    }

    // Defer registration until the page is interactive so the SW
    // install + activate doesn't compete with the initial page render.
    const onLoad = () => {
      navigator.serviceWorker
        .register("/sw.js", { scope: "/" })
        .catch((err) => {
          // eslint-disable-next-line no-console
          console.warn("[ytfactory-sw] register failed:", err);
        });
    };
    if (document.readyState === "complete") {
      onLoad();
    } else {
      window.addEventListener("load", onLoad, { once: true });
    }
  }, []);
  return null;
}

/**
 * Imperative cache wipe — call after logout / account switch so the
 * next user doesn't inherit stale auth-gated payloads.
 */
export function bustServiceWorkerCache(): void {
  if (typeof window === "undefined") return;
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.controller?.postMessage({ type: "BUST_CACHE" });
}
