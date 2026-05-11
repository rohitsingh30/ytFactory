# `/create` wizard (web-next) — knob layout rules

The `/create` page is the canonical "queue a new render" wizard at
`web-next/app/app/create/page.tsx`. Step 2 (`CustomizeReviewStep`) is
where the user sees their picked channel + niche bubbles + form / topic
/ audio / advanced controls. This doc captures durable layout rules so
we stop regressing them.

## Two hard rules (added 2026-05-11)

### 1. No manual "Source" `<CardShell>` next to Form

The Form / Source side-by-side grid is **gone**. Form spans the full
width of its row.

Why: the auto-generate Topic flow (the `<Sparkles>` button on
`TopicCard`) already populates `values.source_kind` and
`values.source_ref` via `onPullSource` whenever it returns a
DiscoverItem. The manual Source `Select` was a parallel control for
the same two fields, which:

- duplicated UI surface for one logical concern,
- looked like the niche picker the user actually wanted (multiple
  rounds of "what is Source, I told you to remove it"),
- never won when the auto-generate flow ran (auto-generate overwrites
  on every click).

`submit()` reads `String(values.source_kind ?? "auto")` and
`String(values.source_ref ?? "").trim() || null`, so removing the
card does not break submissions where the user never auto-generates —
the renderer falls through to its own niche-aware defaults.

If a future feature genuinely needs a manual override (e.g. paste a
specific Reddit URL), add it as a collapsed inline control inside
`TopicCard` (next to the Auto-generate button), not as a top-level
peer card.

### 2. Niche bubble row is always a flat 2-col grid

`VariantList` renders the niche pool as a single `grid sm:grid-cols-2`
of `<VariantCard>` buttons. **Never** auto-group by family, never
render group titles + count badges.

Why: family-grouping (the `groupVariants` + `FAMILIES` heuristic that
was deleted in commit `bb87749`) made the picker read like a tier
list — sections labelled "AITA Cliffhanger / 1", "AITA / 1", "Other /
9" — which obscured the fact that every bubble is a peer option. The
length-kind filter on `NicheBubbleRow` (Short vs Long) plus the
existing card hint already supply all the structure the user needs.

If the authored pool genuinely grows past comfortable scroll length,
the fix is to author tighter `NicheDoc`s via
`scripts/seed_channel_niches.py` — not to add visual grouping.

## Plumbing that must stay intact

The picked niche bubble drives every downstream context call:

- **Render submission:** `renderApi.enqueue({format: variant, ...})`.
  The `variant` state is the niche key; this becomes the renderer's
  format selector.
- **Auto-generate brainstorm:** `discoverApi.pickOne(channel, {
  variant, length_kind, language, niche_key: variant, values, avoid })`.
  `control/routes/discover_routes.py:_llm_topic_items` resolves the
  `NicheDoc` by `niche_key` and threads
  `label / description / prompt_style_guide / hook_template` into the
  LLM brainstorm system prompt so suggested topics respect the niche.
- **Length-kind seeding:** picking a niche whose `length_kind` differs
  from the current `values.length_kind` re-seeds the form-level
  Short/Long toggle (`useEffect` on `[variant, niches]`). This keeps
  the renderer-side aspect ratio in lockstep with what the niche was
  authored for.

Both calls take `length_kind` directly so the Short/Long pools are
correctly scoped on every fetch.

## Where bubbles come from

Bubbles are sourced per-channel from `NicheDoc` JSONs at
`gs://ytfactory-prod-v2-state/<channel>/niches/<key>.json`, fetched by
`nichesApi.list(channel)` once per channel into module state, then
filtered by `length_kind` in `NicheBubbleRow`. Empty pool falls back
to `schema.variants` so un-backfilled channels still render. See
[`docs/channel_niche_pool.md`](./channel_niche_pool.md).

## Where this is enforced

- `web-next/app/app/create/page.tsx` — `VariantList`,
  `NicheBubbleRow`, `CustomizeReviewStep`.
- `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_create_wizard_no_source_card_no_family_groups.md`
  — agent memory pointer.

## Change log

- **2026-05-11 (`bb87749`)** — removed manual Source CardShell, removed
  family-grouping in `VariantList`. Form now spans full row. User had
  asked three times for the Source card to go.
- **2026-05-11 (`689c23d`)** — added `NicheBubbleRow` (replaced
  orphaned `VariantPicker` that was defined but never rendered after
  the wizard rebuild in `dcb6d07`).
