// Visibility-aware polling hook.
//
// Replaces ad-hoc `setInterval(refresh, N)` patterns sprinkled across
// pages so polls automatically pause when the tab is hidden and
// resume immediately on focus. Without this, every dashboard / queue
// tab keeps hammering the backend forever (~2-10 s polls)
// even when the user is on a different tab — wasted server work,
// wasted battery, and a noticeable cause of perceived sluggishness
// when the user has many ytFactory tabs open.
//
// Behaviour:
//   - Calls `fn` once on mount (so the page renders fast).
//   - Schedules a setTimeout for `intervalMs` AFTER each call resolves
//     (not setInterval, so a slow request doesn't queue up overlapping
//     polls — a real bug we've hit during cloud cold-load windows).
//   - Pauses when document.hidden flips true.
//   - Refreshes immediately AND restarts polling on visibilitychange
//     back to visible, so the user sees fresh data the moment they
//     return to the tab.
//
// Cleanup is automatic via the useEffect return.

import { useEffect, useRef } from "react";

export function useVisiblePoll(
  fn: () => void | Promise<void>,
  intervalMs: number,
  deps: ReadonlyArray<unknown> = [],
): void {
  // Stash the latest fn ref so we don't restart the timer every time
  // the caller's fn closure changes (callers usually pass an inline
  // arrow function, which would otherwise reset on every render and
  // double-poll). The deps array is the explicit re-subscribe knob.
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  }, [fn]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      if (cancelled || typeof document !== "undefined" && document.hidden) {
        return;
      }
      try {
        await fnRef.current();
      } catch {
        // Caller is responsible for its own error handling — we just
        // keep polling so a transient 500 doesn't kill the timer.
      }
      if (cancelled) return;
      timer = setTimeout(tick, intervalMs);
    }

    function onVisibility() {
      if (cancelled) return;
      if (document.hidden) {
        if (timer) {
          clearTimeout(timer);
          timer = null;
        }
      } else if (timer === null) {
        // Tab just came back — fire immediately so the user sees
        // fresh data without waiting a full intervalMs.
        tick();
      }
    }

    tick();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);
}
