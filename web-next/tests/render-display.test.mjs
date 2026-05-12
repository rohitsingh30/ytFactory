// Pure-helper tests for web-next/lib/render-display.ts.
//
// Run with:  cd web-next && node --test tests/render-display.test.mjs
//
// Why node:test (not jest/vitest):
//   web-next has no JS testing infrastructure today. Adding vitest +
//   @testing-library/react would be appropriate for testing the
//   PlayerCard COMPONENT but is overkill for this pure helper. node 20+
//   ships `node --test` built-in — zero deps, runs in <100ms.
//
// What this pins:
//   The aspect/kind derivation contract that PlayerCard depends on.
//   Pre-2026-05-12 PlayerCard hard-coded "9:16 · auto-loop" with
//   `aspect-[9/16]` regardless of what the user picked — long-form
//   renders got squashed into a portrait letterbox. Each branch of
//   derivePreviewDisplay is covered below so a future "spec field
//   rename" PR can't silently re-introduce the bug.

import test from "node:test";
import assert from "node:assert/strict";
import {
  deriveAspect,
  deriveKindLabel,
  aspectToFrameClass,
  derivePreviewDisplay,
} from "../lib/render-display.ts";

test("deriveAspect — spec.aspect_ratio wins when set", () => {
  for (const ar of ["16:9", "9:16", "1:1", "4:5"]) {
    assert.equal(deriveAspect({ aspect_ratio: ar }, null), ar);
  }
});

test("deriveAspect — whitespace trimmed", () => {
  assert.equal(deriveAspect({ aspect_ratio: "  16:9  " }, null), "16:9");
});

test("deriveAspect — unknown spec aspect falls through to form fallback", () => {
  assert.equal(
    deriveAspect(
      { aspect_ratio: "21:9" },
      { channel_overrides: { length_kind: "long" } },
    ),
    "16:9",
  );
});

test("deriveAspect — form fallback length_kind=long → 16:9", () => {
  assert.equal(
    deriveAspect(null, { channel_overrides: { length_kind: "long" } }),
    "16:9",
  );
});

test("deriveAspect — form fallback length_kind=short → 9:16", () => {
  assert.equal(
    deriveAspect(null, { channel_overrides: { length_kind: "short" } }),
    "9:16",
  );
});

test("deriveAspect — no spec, no form pick → 9:16 default", () => {
  assert.equal(deriveAspect(null, null), "9:16");
  assert.equal(deriveAspect(undefined, undefined), "9:16");
  assert.equal(deriveAspect({}, {}), "9:16");
});

test("deriveAspect — proposal without channel_overrides doesn't crash", () => {
  assert.equal(deriveAspect(null, { topic: "foo" }), "9:16");
});

test("deriveKindLabel — long_form → 'long-form'", () => {
  assert.equal(deriveKindLabel({ kind: "long_form" }), "long-form");
});

test("deriveKindLabel — short → 'short'", () => {
  assert.equal(deriveKindLabel({ kind: "short" }), "short");
});

test("deriveKindLabel — unknown / null → null", () => {
  assert.equal(deriveKindLabel({ kind: "sports_doc" }), null);
  assert.equal(deriveKindLabel({}), null);
  assert.equal(deriveKindLabel(null), null);
  assert.equal(deriveKindLabel(undefined), null);
});

test("deriveKindLabel — whitespace in kind is trimmed", () => {
  assert.equal(deriveKindLabel({ kind: "  long_form  " }), "long-form");
});

test("aspectToFrameClass — every aspect maps to a Tailwind class with a max-w", () => {
  for (const ar of ["16:9", "9:16", "1:1", "4:5"]) {
    const cls = aspectToFrameClass(ar);
    assert.ok(cls.length > 0, `empty class for ${ar}`);
    assert.match(cls, /aspect-/, `no aspect-* in: ${cls}`);
    assert.match(cls, /max-w-/, `no max-w-* in: ${cls}`);
  }
});

test("aspectToFrameClass — 16:9 uses aspect-video + WIDER max-w-md", () => {
  const cls = aspectToFrameClass("16:9");
  assert.match(cls, /aspect-video/);
  assert.match(cls, /max-w-md/);
});

test("aspectToFrameClass — 9:16 uses aspect-[9/16] + max-w-xs", () => {
  const cls = aspectToFrameClass("9:16");
  assert.match(cls, /aspect-\[9\/16\]/);
  assert.match(cls, /max-w-xs/);
});

test("aspectToFrameClass — 1:1 uses aspect-square", () => {
  assert.match(aspectToFrameClass("1:1"), /aspect-square/);
});

test("aspectToFrameClass — 4:5 uses aspect-[4/5]", () => {
  assert.match(aspectToFrameClass("4:5"), /aspect-\[4\/5\]/);
});

test("derivePreviewDisplay — long-form spec lands the right header + 16:9 frame", () => {
  const out = derivePreviewDisplay(
    { aspect_ratio: "16:9", kind: "long_form" },
    null,
  );
  assert.equal(out.aspect, "16:9");
  assert.equal(out.kindLabel, "long-form");
  assert.match(out.frameClass, /aspect-video/);
  assert.equal(out.headerLabel, "16:9 · long-form · auto-loop");
});

test("derivePreviewDisplay — short spec lands 9:16 + 'short' chip", () => {
  const out = derivePreviewDisplay(
    { aspect_ratio: "9:16", kind: "short" },
    null,
  );
  assert.equal(out.aspect, "9:16");
  assert.equal(out.kindLabel, "short");
  assert.match(out.frameClass, /aspect-\[9\/16\]/);
  assert.equal(out.headerLabel, "9:16 · short · auto-loop");
});

test("derivePreviewDisplay — pre-spec window: form says long, no kind chip yet", () => {
  // Mirrors the dispatching → rendering window where the worker has
  // not yet built the spec but the form pick is on the proposal.
  const out = derivePreviewDisplay(
    null,
    { channel_overrides: { length_kind: "long" } },
  );
  assert.equal(out.aspect, "16:9");
  assert.equal(out.kindLabel, null);
  assert.equal(out.headerLabel, "16:9 · auto-loop");
});

test("derivePreviewDisplay — pre-spec window, no form pick: 9:16 default", () => {
  const out = derivePreviewDisplay(null, null);
  assert.equal(out.aspect, "9:16");
  assert.equal(out.kindLabel, null);
  assert.equal(out.headerLabel, "9:16 · auto-loop");
});

test("REGRESSION 2026-05-12: long_form spec must not render 9:16", () => {
  // The user's complaint: "I selected long form - but Preview 9:16 ·
  // auto-loop still showing". Pre-fix PlayerCard ignored
  // render_spec entirely. Pin that the long-form spec produces
  // a 16:9 frame regardless of any other input.
  const out = derivePreviewDisplay(
    { kind: "long_form", aspect_ratio: "16:9" },
    { channel_overrides: { length_kind: "long" } },
  );
  assert.equal(out.aspect, "16:9");
  assert.notEqual(out.aspect, "9:16");
  assert.match(out.frameClass, /aspect-video/);
});
