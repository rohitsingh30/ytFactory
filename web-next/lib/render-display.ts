/**
 * Pure helpers that translate a backend RenderSpec dict (or the
 * fall-through pre-spec form pick) into the shape PlayerCard needs to
 * render the preview frame correctly.
 *
 * Why this is pulled out of the React component:
 *   1. Pre-2026-05-12 PlayerCard hard-coded "9:16 · auto-loop" with
 *      `aspect-[9/16]` regardless of what the user picked. Long-form
 *      renders (which produce a 16:9 mp4) ended up squashed into a
 *      portrait letterbox and the user (rightly) called it out.
 *   2. The fix is straightforward, but the derivation has 5+ branches
 *      (aspect from spec | from form fallback, kind label, Tailwind
 *      class). Pinning each branch with a unit test prevents the next
 *      "spec field rename" PR from silently re-introducing the bug.
 *   3. Web-next has no React testing infra (no jest/vitest installed).
 *      Pure helpers are testable via Node 20+'s built-in `node --test`
 *      runner — zero deps, zero config.
 *
 * Source of truth for the spec dict shape:
 *   pipeline/render/spec.py::RenderSpec.to_dict
 */

export type Aspect = "16:9" | "9:16" | "1:1" | "4:5";

export type RenderKindLabel = "long-form" | "short" | null;

const VALID_ASPECTS: ReadonlySet<string> = new Set(["16:9", "9:16", "1:1", "4:5"]);

/**
 * Threshold (seconds) above which an un-tagged proposal is treated
 * as long-form for preview purposes. Mirrors
 * pipeline/render/spec.py::_LONG_FORM_THRESHOLD_S.
 */
const LONG_FORM_THRESHOLD_S = 120;

/**
 * Resolve the preview aspect ratio. Priority:
 *   1. spec.aspect_ratio (the worker-resolved RenderSpec).
 *   2. proposal.channel_overrides.length_kind (form fallback) —
 *      "long" → 16:9, "short" → 9:16. Used in the
 *      pending → dispatching window before the spec lands.
 *   3. proposal.length_s threshold (defense in depth — if a future
 *      form drop loses `length_kind` again, the seconds field on the
 *      typed proposal still drives the right preview).
 *   4. Default 9:16 (the historical Shorts default).
 */
export function deriveAspect(
  spec: Record<string, unknown> | null | undefined,
  proposal: Record<string, unknown> | null | undefined,
): Aspect {
  const raw = (spec?.aspect_ratio as string | undefined)?.trim();
  if (raw && VALID_ASPECTS.has(raw)) return raw as Aspect;

  const overrides = proposal?.channel_overrides as Record<string, unknown> | undefined;
  const lk = (overrides?.length_kind as string | undefined)?.trim();
  if (lk === "long") return "16:9";
  if (lk === "short") return "9:16";

  const lenRaw = proposal?.length_s ?? overrides?.length_s;
  const len = typeof lenRaw === "number" ? lenRaw : Number(lenRaw);
  if (Number.isFinite(len) && len > LONG_FORM_THRESHOLD_S) return "16:9";

  return "9:16";
}

/**
 * Friendly kind label for the preview header. Returns null when the
 * spec hasn't been resolved yet so the caller can omit the chip.
 */
export function deriveKindLabel(
  spec: Record<string, unknown> | null | undefined,
): RenderKindLabel {
  const k = (spec?.kind as string | undefined)?.trim();
  if (k === "long_form") return "long-form";
  if (k === "short") return "short";
  return null;
}

/**
 * Map an aspect to the Tailwind classes the preview frame needs to
 * letterbox correctly. Pre-fix this was hard-coded as
 * `aspect-[9/16] w-full max-w-xs` for every render.
 */
export function aspectToFrameClass(aspect: Aspect): string {
  switch (aspect) {
    case "16:9":
      return "aspect-video w-full max-w-md";
    case "1:1":
      return "aspect-square w-full max-w-xs";
    case "4:5":
      return "aspect-[4/5] w-full max-w-xs";
    case "9:16":
    default:
      return "aspect-[9/16] w-full max-w-xs";
  }
}

/**
 * One-shot composer for PlayerCard. Returns everything the card needs.
 */
export function derivePreviewDisplay(
  spec: Record<string, unknown> | null | undefined,
  proposal: Record<string, unknown> | null | undefined,
): {
  aspect: Aspect;
  kindLabel: RenderKindLabel;
  frameClass: string;
  headerLabel: string;
} {
  const aspect = deriveAspect(spec, proposal);
  const kindLabel = deriveKindLabel(spec);
  const frameClass = aspectToFrameClass(aspect);
  const headerLabel = `${aspect} · ${kindLabel ? `${kindLabel} · ` : ""}auto-loop`;
  return { aspect, kindLabel, frameClass, headerLabel };
}
