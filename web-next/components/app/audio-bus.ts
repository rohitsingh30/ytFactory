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
 */

type Stopper = () => void;

const _stoppers = new Set<Stopper>();

export function registerStopper(s: Stopper): () => void {
  _stoppers.add(s);
  return () => _stoppers.delete(s);
}

export function stopAllOthers(except: Stopper): void {
  for (const s of _stoppers) {
    if (s !== except) s();
  }
}
