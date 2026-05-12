// Pure-helper tests for web-next/lib/render-payload.ts.
//
// Run with:  cd web-next && node --test tests/render-payload.test.mjs
//
// # Why this test file exists
//
// /create's submit handler used to bake its passthrough list inline,
// and the team twice forgot to add a new form key to it (most
// recently `length_kind`, which made the PlayerCard show
// "9:16 · auto-loop" on every long-form render even though the
// worker rendered 16:9 correctly).
//
// `buildChannelOverrides` derives the passthrough from the schema
// instead of a hand-rolled list — these tests pin the rule so the
// next descriptor someone adds to customization.py automatically
// flows through, and the next reserved-vs-overrides decision can't
// regress silently.

import test from "node:test";
import assert from "node:assert/strict";
import {
  CHANNEL_OVERRIDE_RESERVED_AT_TOP_LEVEL,
  FORM_INTERNAL_KEYS,
  buildChannelOverrides,
  resolveLengthSeconds,
} from "../lib/render-payload.ts";

function schemaWith(keys) {
  return {
    channel: "test",
    label: "Test",
    tagline: "",
    language: "en",
    variants: [],
    fields: keys.map((k) => ({ key: k, label: k, kind: "select" })),
  };
}

test("buildChannelOverrides — schema-declared knobs flow through", () => {
  const schema = schemaWith(["voice", "music_bed", "captions_density"]);
  const values = {
    voice: "sarah",
    music_bed: "ambient_low",
    captions_density: "dense",
  };
  assert.deepEqual(buildChannelOverrides(values, schema), {
    voice: "sarah",
    music_bed: "ambient_low",
    captions_density: "dense",
  });
});

test("buildChannelOverrides — REGRESSION 2026-05-12: length_kind=long lands in overrides", () => {
  // The bug: pre-fix passthrough was hand-rolled and length_kind was
  // never added. The PlayerCard's render-display.ts fallback couldn't
  // see the user's pick → preview showed 9:16 for long-form renders.
  const schema = schemaWith(["length_kind"]);
  const overrides = buildChannelOverrides({ length_kind: "long" }, schema);
  assert.equal(overrides.length_kind, "long");
});

test("buildChannelOverrides — length_kind=short still flows through", () => {
  const schema = schemaWith(["length_kind"]);
  const overrides = buildChannelOverrides({ length_kind: "short" }, schema);
  assert.equal(overrides.length_kind, "short");
});

test("buildChannelOverrides — typed top-level proposal keys are excluded", () => {
  const schema = schemaWith([
    "topic", "notes", "source_kind", "source_ref", "format", "length_s",
    "voice",
  ]);
  const values = {
    topic: "Britney Spears mystery",
    notes: "investigative",
    source_kind: "reddit",
    source_ref: "https://example.com/x",
    format: "unresolved_mysteries",
    length_s: 1800,
    voice: "sarah",
  };
  const out = buildChannelOverrides(values, schema);
  for (const k of ["topic", "notes", "source_kind", "source_ref", "format", "length_s"]) {
    assert.ok(!(k in out), `${k} leaked into channel_overrides`);
  }
  assert.equal(out.voice, "sarah");
});

test("buildChannelOverrides — form-internal keys (length_minutes) are dropped", () => {
  const schema = schemaWith(["length_kind", "length_minutes"]);
  const out = buildChannelOverrides(
    { length_kind: "long", length_minutes: 45 },
    schema,
  );
  assert.equal(out.length_kind, "long");
  assert.ok(!("length_minutes" in out));
});

test("buildChannelOverrides — empty strings, null, undefined are skipped", () => {
  const schema = schemaWith(["voice", "music_bed", "captions_density", "schedule_at"]);
  const out = buildChannelOverrides(
    { voice: "", music_bed: null, captions_density: undefined, schedule_at: "  " },
    schema,
  );
  assert.deepEqual(out, {});
});

test("buildChannelOverrides — falsy-but-meaningful values (0, false) are kept", () => {
  const schema = schemaWith(["captions_enabled", "music_bed_volume"]);
  const out = buildChannelOverrides(
    { captions_enabled: false, music_bed_volume: 0 },
    schema,
  );
  assert.equal(out.captions_enabled, false);
  assert.equal(out.music_bed_volume, 0);
});

test("buildChannelOverrides — non-schema keys in values are ignored", () => {
  const schema = schemaWith(["voice"]);
  const out = buildChannelOverrides(
    { voice: "sarah", __ui_dialog_open: true, _random_state: 42 },
    schema,
  );
  assert.deepEqual(out, { voice: "sarah" });
});

test("buildChannelOverrides — null schema falls back to values keys", () => {
  const out = buildChannelOverrides(
    { voice: "sarah", topic: "skip me", length_minutes: 45 },
    null,
  );
  assert.equal(out.voice, "sarah");
  assert.ok(!("topic" in out));
  assert.ok(!("length_minutes" in out));
});

test("buildChannelOverrides — empty schema also falls back to values keys", () => {
  const emptySchema = { ...schemaWith([]), fields: [] };
  const out = buildChannelOverrides({ voice: "sarah" }, emptySchema);
  assert.equal(out.voice, "sarah");
});

test("buildChannelOverrides — EXTENSIBILITY: a synthetic NEW descriptor flows through automatically", () => {
  // Pre-fix this test would fail — adding a descriptor to the
  // backend wouldn't make it through the form's hand-rolled
  // passthrough. With the schema-driven helper it does.
  const schema = schemaWith(["voice", "narrator_visual_mode"]);
  const out = buildChannelOverrides(
    { voice: "sarah", narrator_visual_mode: "voice_only" },
    schema,
  );
  assert.equal(out.narrator_visual_mode, "voice_only");
});

test("CHANNEL_OVERRIDE_RESERVED_AT_TOP_LEVEL — contains the typed proposal fields", () => {
  for (const k of ["topic", "notes", "source_kind", "source_ref", "format", "length_s"]) {
    assert.ok(
      CHANNEL_OVERRIDE_RESERVED_AT_TOP_LEVEL.has(k),
      `expected ${k} in reserved set`,
    );
  }
});

test("FORM_INTERNAL_KEYS — contains length_minutes", () => {
  assert.ok(FORM_INTERNAL_KEYS.has("length_minutes"));
});

test("CHANNEL_OVERRIDE_RESERVED_AT_TOP_LEVEL — does NOT include length_kind", () => {
  // The whole point of this fix: length_kind must NOT be reserved,
  // it must flow through channel_overrides.
  assert.ok(!CHANNEL_OVERRIDE_RESERVED_AT_TOP_LEVEL.has("length_kind"));
});

test("resolveLengthSeconds — short → 55s", () => {
  assert.equal(resolveLengthSeconds({ length_kind: "short" }), 55);
});

test("resolveLengthSeconds — long defaults to 30 min", () => {
  assert.equal(resolveLengthSeconds({ length_kind: "long" }), 30 * 60);
});

test("resolveLengthSeconds — long honours length_minutes", () => {
  assert.equal(resolveLengthSeconds({ length_kind: "long", length_minutes: 45 }), 45 * 60);
});

test("resolveLengthSeconds — long clamps minutes ≥ 1", () => {
  assert.equal(resolveLengthSeconds({ length_kind: "long", length_minutes: 0 }), 60);
  assert.equal(resolveLengthSeconds({ length_kind: "long", length_minutes: -5 }), 60);
});

test("resolveLengthSeconds — missing length_kind defaults to short", () => {
  assert.equal(resolveLengthSeconds({}), 55);
});
