/**
 * Pure helpers that translate /create wizard form state into the
 * payload shape `renderApi.enqueue` expects.
 *
 * # Why this exists
 *
 * The /create page used to bake the passthrough list inline in its
 * submit() handler — a hand-maintained array of every form-field key
 * the renderer cares about. Each time the team added a new form
 * input, someone had to remember to also add the key to that list,
 * and twice now the team forgot (most recently `length_kind`, which
 * is what surfaced the "long-form preview shows 9:16" complaint).
 *
 * The backend already has a descriptor registry
 * (`pipeline/schemas/customization.py::CustomizationField` with
 * `cfg_targets` / `spec_field` / `apply_handler` / `prompt_patch_fn`)
 * so adding a form input is a one-declaration change. The MISSING
 * piece was the form's exit path: the passthrough never auto-derived
 * from the schema.
 *
 * # The fix
 *
 * `buildChannelOverrides(values, schema)` walks `schema.fields` and
 * pulls every key into `channel_overrides` EXCEPT typed top-level
 * proposal fields (topic / notes / source_kind / source_ref / format /
 * length_s) and form-internal scaffolding (length_minutes). Any new
 * descriptor declared in customization.py flows through automatically.
 *
 * Source of truth on the backend side:
 *   - Top-level keys → control/core/schema.py::ShortProposal
 *   - channel_overrides keys → pipeline/render/spec.py::_TYPED_OVERRIDE_KEYS
 */

import type { CustomizationSchema } from "./types";

export const CHANNEL_OVERRIDE_RESERVED_AT_TOP_LEVEL: ReadonlySet<string> = new Set([
  "topic",
  "notes",
  "source_kind",
  "source_ref",
  "format",
  "length_s",
]);

export const FORM_INTERNAL_KEYS: ReadonlySet<string> = new Set([
  "length_minutes",
]);

export function buildChannelOverrides(
  values: Record<string, unknown>,
  schema: CustomizationSchema | null | undefined,
): Record<string, unknown> {
  const fieldKeys: string[] = schema?.fields?.length
    ? schema.fields.map((f) => f.key)
    : Object.keys(values);

  const out: Record<string, unknown> = {};
  for (const k of fieldKeys) {
    if (CHANNEL_OVERRIDE_RESERVED_AT_TOP_LEVEL.has(k)) continue;
    if (FORM_INTERNAL_KEYS.has(k)) continue;
    const v = values[k];
    if (v === undefined || v === null) continue;
    if (typeof v === "string" && v.trim() === "") continue;
    out[k] = v;
  }
  return out;
}

export function resolveLengthSeconds(values: Record<string, unknown>): number {
  const kind = String(values.length_kind ?? "short");
  const minutes = Number(values.length_minutes ?? 30);
  if (kind === "long") return Math.max(1, minutes) * 60;
  return 55;
}
