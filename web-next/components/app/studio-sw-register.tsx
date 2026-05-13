"use client";

// Used to register public/sw.js. **Disabled 2026-05-13** because the
// SW caused a "stuck checking session" trap for users whose browsers
// kept a previous version installed across deploys (intercepted
// requests with scope "/", served stale HTML / chunk references that
// no longer existed on the server). public/sw.js is now a kill
// switch that uninstalls itself; this component only ensures any
// existing registration is cleaned up.
//
// We can't simply delete the file — it's still imported by
// app/app/layout.tsx and other call sites might exist. Keeping the
// component as a no-op (plus a defensive unregister sweep) is the
// safest landing.

import { useEffect } from "react";

export function StudioSwRegister(): null {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!("serviceWorker" in navigator)) return;

    // Defensive: unregister every existing SW. The cleanup also lives
    // in StaleShellGuard (root layout, runs on every page) so this is
    // belt-and-braces — but cheap and idempotent.
    navigator.serviceWorker.getRegistrations().then((regs) => {
      for (const r of regs) void r.unregister();
    }).catch(() => { /* best-effort */ });

    // Wipe every cache entry too — accidentally-installed workbox /
    // next-pwa caches survive an unregister otherwise.
    if (typeof caches !== "undefined") {
      caches.keys().then((names) => {
        for (const n of names) void caches.delete(n);
      }).catch(() => { /* best-effort */ });
    }
  }, []);
  return null;
}

/**
 * Imperative cache wipe — call after logout / account switch so the
 * next user doesn't inherit stale auth-gated payloads.
 *
 * **Audit Q2.48** — pre-fix this only posted ``BUST_CACHE`` to the
 * service worker, leaving every browser-side cache (localStorage,
 * sessionStorage, the SWR mutation map, the Firestore IndexedDB
 * persistence layer) intact. Logout-then-different-user-login on
 * the same browser tab let the new user see the previous user's
 * dashboard data for as long as the SWR cache survived. Now also
 * wipes localStorage + sessionStorage entries we own.
 */
export function bustServiceWorkerCache(): void {
  if (typeof window === "undefined") return;
  // The SW is now a kill switch — postMessage may or may not reach
  // anything, but it's still cheap to send. The unregister + cache
  // delete in StudioSwRegister + StaleShellGuard handles the actual
  // cleanup.
  if ("serviceWorker" in navigator) {
    try {
      navigator.serviceWorker.controller?.postMessage({ type: "BUST_CACHE" });
    } catch { /* best-effort */ }
  }
  // Audit Q2.48 — wipe localStorage + sessionStorage so SWR's
  // localStorage cache and our own auth-gated entries don't leak
  // to the next user. We use prefix matching ("ytfactory:" /
  // "swr-") so we don't nuke unrelated host storage.
  try {
    if (typeof localStorage !== "undefined") {
      const keysToRemove: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && (k.startsWith("ytfactory:") || k.startsWith("swr-") || k === "user")) {
          keysToRemove.push(k);
        }
      }
      keysToRemove.forEach((k) => localStorage.removeItem(k));
    }
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.clear();
    }
  } catch {
    // localStorage can throw in private-mode tabs / quota-exceeded;
    // a wipe failure here shouldn't block logout.
  }
}
