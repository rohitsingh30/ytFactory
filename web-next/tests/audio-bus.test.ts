// Audit D3.49 — audio-bus stopAllOthers must snapshot _stoppers
// before iteration so callbacks that mutate the Set don't drop
// or double-fire registered stoppers.
//
// Run with: cd web-next && npm test

import test from "node:test";
import assert from "node:assert/strict";
import { registerStopper, stopAllOthers } from "../components/app/audio-bus.ts";

test("D3.49: stopAllOthers fires every stopper except the caller", () => {
  const calls: string[] = [];
  const sa = () => calls.push("a");
  const sb = () => calls.push("b");
  const sc = () => calls.push("c");
  const ua = registerStopper(sa);
  const ub = registerStopper(sb);
  const uc = registerStopper(sc);
  try {
    stopAllOthers(sb);
    assert.deepEqual(calls.sort(), ["a", "c"], "sb (the caller) must be skipped");
  } finally {
    ua();
    ub();
    uc();
  }
});

test("D3.49: stopAllOthers tolerates callbacks that unregister themselves mid-iteration", () => {
  const calls: string[] = [];
  let ub: () => void = () => {};
  const sa = () => calls.push("a");
  const sb = () => {
    calls.push("b");
    // This is the buggy mutation pattern that pre-fix corrupted the
    // Set iterator: unregister ourselves while still iterating.
    ub();
  };
  const sc = () => calls.push("c");
  const ua = registerStopper(sa);
  ub = registerStopper(sb);
  const uc = registerStopper(sc);
  try {
    // Trigger from the OUTSIDE (no `except` matches any), so all
    // three should fire.
    stopAllOthers(() => {});
    // All three callbacks must have fired exactly once. Pre-fix sc
    // could be skipped on some V8 versions because sb's mid-iter
    // delete shifted the Set's internal cursor.
    assert.deepEqual(calls.sort(), ["a", "b", "c"]);
  } finally {
    ua();
    uc();
  }
});

test("D3.49: stopAllOthers tolerates callbacks that register a NEW stopper mid-iteration", () => {
  const calls: string[] = [];
  let unregNew: (() => void) | null = null;
  const sa = () => calls.push("a");
  const sb = () => {
    calls.push("b");
    // Register a brand-new stopper from inside another stopper. The
    // snapshot taken BEFORE iteration must NOT include this — so
    // it should NOT fire during this stopAllOthers invocation.
    unregNew = registerStopper(() => calls.push("late-added"));
  };
  const sc = () => calls.push("c");
  const ua = registerStopper(sa);
  const ub = registerStopper(sb);
  const uc = registerStopper(sc);
  try {
    stopAllOthers(() => {});
    // a, b, c fired; late-added did NOT (it joined after the snapshot).
    assert.deepEqual(calls.sort(), ["a", "b", "c"]);
  } finally {
    ua(); ub(); uc();
    if (unregNew !== null) (unregNew as () => void)();
  }
});

test("D3.49: registerStopper returns an idempotent unregister", () => {
  const sa = () => {};
  const ua = registerStopper(sa);
  ua();
  // Calling unregister twice must not throw.
  ua();
  // After unregister, stopAllOthers should NOT fire sa.
  let fired = false;
  const sb = () => { fired = true; };
  const ub = registerStopper(sb);
  try {
    stopAllOthers(() => {});
    assert.equal(fired, true);
  } finally {
    ub();
  }
});
