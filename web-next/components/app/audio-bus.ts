"use client";

/**
 * Single-track audio context — only one AudioSampleButton plays at a
 * time across the page. The button registers a stop callback into a
 * module-global ref; pressing play on any other instance fires every
 * registered stop callback first.
 *
 * Module-global (not React Context) because the wiring is purely
 * imperative — no component cares about the "currently playing"
 * identity, only that "stop everyone else before I play".
 *
 * Audit D3.49 — pre-fix `stopAllOthers` iterated `_stoppers` directly.
 * If a `s()` callback synchronously called `registerStopper` /
 * `_stoppers.delete` (e.g. an unmounting component), the Set would
 * be mutated mid-iteration. ECMAScript spec says Set iteration order
 * is "insertion order", but mutating during iteration is allowed and
 * produces well-defined-but-surprising behaviour: V8 will emit the
 * newly-added entries OR skip the removed ones depending on internal
 * state. To be safe, snapshot via `Array.from(_stoppers)` before
 * iteration so the running list is stable for the duration of the
 * call.
 */

type Stopper = () => void;

const _stoppers = new Set<Stopper>();

export function registerStopper(s: Stopper): () => void {
  _stoppers.add(s);
  return () => _stoppers.delete(s);
}

export function stopAllOthers(except: Stopper): void {
  // Audit D3.49 — snapshot first so mid-iteration mutations from
  // stopper callbacks don't drop OR double-fire any registered
  // stoppers.
  const snapshot = Array.from(_stoppers);
  for (const s of snapshot) {
    if (s !== except) s();
  }
}
