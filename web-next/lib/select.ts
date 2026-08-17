/**
 * Radix `<Select.Item />` forbids an empty-string `value` prop (an empty
 * value is reserved for clearing the selection / showing the placeholder).
 *
 * Several backend customization fields (`critic_loop`, `tone`, …) use
 * `value === ""` as an intentional "use channel default" sentinel — see
 * `pipeline/schemas/customization.py`. To render those options without
 * tripping Radix's runtime error we swap the empty string for a
 * non-empty sentinel at the UI boundary and translate it back on change.
 *
 * `buildChannelOverrides` (lib/render-payload.ts) already skips
 * empty-string values, so decoding back to `""` preserves the
 * "no override" semantics end-to-end.
 */
export const SELECT_EMPTY_SENTINEL = "__default__";

/** Encode a field/option value for a Radix `<SelectItem value>` / `<Select value>`. */
export function encodeSelectValue(v: unknown): string | undefined {
  if (v === undefined || v === null) return undefined;
  const s = String(v);
  return s === "" ? SELECT_EMPTY_SENTINEL : s;
}

/** Decode a value emitted by Radix `onValueChange` back to its real form. */
export function decodeSelectValue(v: string): string {
  return v === SELECT_EMPTY_SENTINEL ? "" : v;
}
