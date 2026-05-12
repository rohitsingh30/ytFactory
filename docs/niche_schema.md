# Niche schema canonicalisation (2026-05-11)

> **Single source of truth for niches** is `<channel>/niches/<key>.json`
> (the NicheDoc store, GCS-backed). `pipeline/channels.yaml` is
> channel-only metadata; the legacy `niches:` block was dropped along
> with `pipeline/niches.py:NICHE_CHANNEL`. Every consumer
> (`pipeline.paths`, `pipeline.channels`, `pipeline.llm.imitate`,
> `web.server`, `scripts.pull_stories`, `scripts.update_thumbnails`)
> reads niche routing through `pipeline.niche_specs.niche_channel_map()`,
> which builds the legacy-shape `{niche_key: (state_dir, variant_yaml_path)}`
> map by walking NicheDocs across every channel.

## Why this exists

Pre-2026-05-11 niche metadata was scattered across 6+ stores with
mismatched keys and overlapping fields:

1. `pipeline/channels.yaml` `niches:` block — `niche_key: [state_subdir, variant_yaml_filename]`
2. `<channel>/niches/<key>.json` — rich NicheDoc (label, description, format, voice, source, …)
3. `pipeline/niches.py:NICHE_CHANNEL` — duplicate of #1
4. `web/server.py:_NICHE_UI` — UI presentation (label, color, subreddit)
5. `pipeline/thumbnails.py:_STYLES` — per-niche thumbnail styles
6. `pipeline/llm/imitate.py` enum — hardcoded niche names

Concrete bugs that surfaced (the trigger for this refactor):

- `oddities` (channels.yaml) vs `wiki_oddities` (NicheDoc) — same niche, two keys
- `tih` vs `today_in_history` — same
- `sports_ranked` vs `top5_countdown` — same
- `today_in_history` declared in BOTH mystoriesanimated AND historyrecapped
  → `niche_channel_map()` is keyed globally so one silently won
- `sportsrecapped` channel's `niches:` block had only 1 entry; the wizard
  showed "no short niches authored" even though 6 NicheDoc JSONs existed
  — under the wrong slug (`sportstoriesanimated` directory)
- `voice` was a niche field, but voice is per-render (gender / language /
  persona / energy) and a given niche can be narrated by any voice

## The schema

`pipeline/niche_specs.py:NicheDoc` is the canonical model. As of
2026-05-12 the model is **partially nested** — only the `source`
field is structured; the other "should-be-nested" fields
(`hook_template`, `image_style`, …) are still flat on the model
itself, and the `routing` / `templates` / `aesthetic` keys present in
the persisted GCS JSONs are silently dropped via `extra: ignore`.

```python
class NicheDoc(BaseModel):
    # identity
    key: str                # globally unique, [a-z0-9_]+
    label: str
    description: str

    # behavior
    length_kind: Literal["short", "long"]
    format:      Literal["animated", "text", "cooking", "footage",
                         "split_screen", "rhyme", "footage_only",
                         "long_form", "sports_doc"]

    # composed (the only nested sub-spec actually loaded today)
    source: NicheSource | None  # { kind, ref } — populated from the
                                # nested GCS shape OR synthesised from
                                # the legacy flat source_kind/source_ref
                                # by `_migrate_source_shape`. See
                                # docs/pydantic_flat_to_nested_compat.md.

    # legacy flat fields kept on the model for back-compat. The
    # source_kind/source_ref pair is mirrored from `source` (and
    # vice-versa) by the before-validator. Other flat fields below are
    # the actual storage; the GCS JSONs' nested {templates, aesthetic,
    # routing} keys are silently dropped on load.
    source_kind:        SourceKind = "manual"
    source_ref:         Optional[str]
    prompt_style_guide: str
    hook_template:      str
    closer_template:    str
    image_style:        str
    music_bed:          Optional[str]

    # provenance
    created_at: str
    created_by: Literal["backfill", "user", "ai_chat"]
```

**Why `source` got the nested treatment first (2026-05-12):** the
discover endpoint needed `niche.source.{kind,ref}` to drive
niche-aware topic generation (see
[`docs/discover_topic_generation.md`](discover_topic_generation.md)).
Pre-fix, the flat-only model with `extra: ignore` silently dropped
the nested `source` key from the GCS JSONs, so
`niche_doc.source_kind` always returned the default `"manual"`,
defeating any downstream attempt to dispatch on it. The fix added
`class NicheSource` + a `model_validator(mode="before")` that keeps
nested ↔ flat in sync (nested wins on conflict; unknown source
kinds degrade to LLM-only routing instead of 422-failing the load).

**Still TODO:** `routing`, `templates`, `aesthetic` are documented
above as "should be nested sub-specs" but the model doesn't actually
declare them. The persisted GCS JSONs ship them under those keys; the
loader silently drops them. Adding analogous `model_validator` +
nested classes + read-side properties for those three fields is a
follow-up. See `docs/pydantic_flat_to_nested_compat.md` § "How far
this is shipped" for the per-field status.

**Voice is deliberately not a niche field.** Voice picks live in the
standalone voice catalog (`web/server.py:VOICES` + `pipeline/voice_refs/`)
and are selected per-render. A given niche can be narrated by any voice.
The AI-draft prompt (`control/routes/niche_specs_routes.py`) explicitly
tells the LLM `VOICE IS NOT A FIELD`.

## Hard rules

1. **Niche keys are globally unique.** `niche_channel_map()` returns
   `dict[niche_key, ...]` across all channels — a collision silently
   drops one. The seed script enforces uniqueness at run time
   (`_assert_seeds_valid()`); duplicates raise `SystemExit` before the
   first GCS write. Naming convention for collisions: prefix the
   channel-specific one (e.g. mystoriesanimated has `today_in_history`,
   historyrecapped has `history_today`).
2. **≥5 niches per (channel, length_kind) cell.** The wizard's Niche
   bubble row needs a meaningful pick; an empty cell falls back to the
   "No niches authored" empty state. Enforced by
   `_assert_min_niches_per_cell()` in the seed script.
3. **`routing.channel` must match a registered Channel slug.** No
   aliases. The Channel manifest is `pipeline/channels.yaml`; the
   NicheDoc's `routing.channel` is the join key.
4. **`routing.variant_yaml` is `None` for long-form.** Long-form niches
   render through the channel-level `pipeline/channels/<slug>.yaml`
   directly (no per-niche variant overlay).
5. **AI is always invoked.** There is no "let AI decide" niche. The
   wizard's bubble row only declares WHICH KIND of video to author —
   AI handles writing + visuals downstream regardless.

## Lookup helpers

All in `pipeline.niche_specs`:

| helper | purpose |
|---|---|
| `list_niches(channel_key)` | every NicheDoc under one channel, sorted by label |
| `get_niche(channel_key, key)` | one NicheDoc by (channel, key) |
| `save_niche(channel_key, doc)` | writes to GCS; disk mirror opt-in via `YTFACTORY_NICHE_DISK_MIRROR=1` |
| `delete_niche(channel_key, key)` | removes from both backends |
| `all_niches_across_channels()` | every NicheDoc across every channel |
| `niche_channel_map()` | `{niche_key: (state_dir, variant_yaml_path)}` (drop-in for legacy `NICHE_CHANNEL`) |
| `channel_for_niche_key(niche_key)` | owning channel slug, or None |
| `variant_yaml_path_for(doc)` | repo-relative variant YAML path, or None |
| `state_dir_for(doc)` | `<channel>[/<state_subdir>]` |

Re-exports for backwards compat:

- `pipeline.channels.niche_channel_map()` — delegates to `niche_specs`
- `pipeline.channels.channel_for_niche()` — same
- `pipeline.channels.all_niches()` — same
- `Channel.variant_yaml_path(niche_key)` — instance helper, reads NicheDocs
- `Channel.state_dir(niche_key)` — same
- `Channel.niche_keys()` — sorted list of niche keys for this channel

## Path resolver multi-form match

`pipeline.paths.RenderPaths.from_channel_yaml` accepts three variant
YAML path forms when matching against a NicheDoc's routing:

```
pipeline/variants/<channel>/<file>.yaml   # canonical (post-2026-05-10)
<channel>/variants/<file>.yaml            # legacy
<file>.yaml                               # bare filename
```

This makes the resolver tolerant of historical layouts without
forcing every caller to know the canonical form.

## Backwards compat

On-disk JSONs from before the schema migration had flat top-level
fields (`source_kind`, `hook_template`, `voice`, …) and no `channel`
key (it was implicit from the file path
`<channel>/niches/<key>.json`). **Three** layers of compat keep them
loading AND keep legacy attribute reads working:

1. **`_promote_legacy_flat_shape` model_validator** (NicheDoc itself) —
   hoists every legacy flat field into its sub-spec dict before field
   validation runs. A nested-shape input (already-correct) flows
   through untouched.
2. **`_parse_doc_with_channel_inject` loader** (`niche_specs._gcs_load`,
   `_gcs_list`, `list_niches`, `get_niche`) — injects the
   directory-derived channel into the JSON if the doc has neither
   nested `routing` nor a flat `channel` key.
3. **Read-side `@property` shims on NicheDoc** — paired 1:1 with
   every entry in `_LEGACY_FLAT_FIELDS`. `niche_doc.prompt_style_guide`
   delegates to `niche_doc.templates.prompt_style_guide` (with a safe
   `""` default), `niche_doc.source_kind` to `niche_doc.source.kind`,
   etc. Without these, every legacy reader (e.g. the discover route)
   raises `AttributeError` on the new schema → 502 in production.
   See [`docs/pydantic_flat_to_nested_compat.md`](pydantic_flat_to_nested_compat.md)
   for the general rule + the test pattern that catches this class of
   regression.

`extra="ignore"` in the model config silently drops `voice` and
`target_length_s` (both removed across past migrations).

## How to add / rename / remove a niche

The seed script `scripts/seed_channel_niches.py` is the source of
truth for the **default** niche pool. User-authored niches (created
via the AI-draft endpoint at `/api/channels/<ch>/niches/draft` then
saved via POST) coexist; re-seeding skips any doc whose
`created_by != "backfill"` so user edits aren't clobbered.

```bash
# 1. Edit the CHANNEL_NICHE_SEEDS dict (uses _seed() helper for clarity).
$EDITOR scripts/seed_channel_niches.py

# 2. Push to prod GCS (writes the JSONs the UI reads).
YTFACTORY_STATE_BUCKET=ytfactory-prod-v2-state \
    .venv/bin/python scripts/seed_channel_niches.py

# 3. Verify.
gsutil ls gs://ytfactory-prod-v2-state/<channel>/niches/

# 4. Hard-reload /app/create — bubbles update immediately
#    (no service redeploy; the API reads GCS on every call).
```

Validation gates (block before any GCS write):

- `_assert_min_niches_per_cell()` — ≥5 per (channel, length_kind)
- `_assert_seeds_valid()` — every seed validates against NicheDoc
  AND every key is globally unique

## Migration log (2026-05-11)

Ripped out:

- `pipeline/niches.py` (deleted entirely; was the legacy NICHE_CHANNEL map)
- `pipeline/channels.yaml:niches:` block (channels.yaml is now channel-only)
- `Channel.niches` field (Channel model no longer carries niche routing)
- `voice` field on NicheDoc (per-render concern, not niche-bound)

Added:

- `pipeline.niche_specs.NicheDoc` extended with `source`, `routing`,
  `templates`, `aesthetic` sub-specs + globally-unique key contract
- `pipeline.niche_specs.niche_channel_map / channel_for_niche_key /
  variant_yaml_path_for / state_dir_for / all_niches_across_channels`
- `_promote_legacy_flat_shape` model_validator (write-side backwards compat)
- 11 `@property` read-side shims on NicheDoc (one per `_LEGACY_FLAT_FIELDS` entry — symmetric pair, prevents `AttributeError` 502s; see `docs/pydantic_flat_to_nested_compat.md`)
- `_parse_doc_with_channel_inject` loader (backwards compat)
- `scripts/seed_channel_niches.py:_seed()` helper +
  `_assert_min_niches_per_cell` / `_assert_seeds_valid` gates
- `pipeline.paths.RenderPaths.from_channel_yaml` multi-form matching

Reseeded 77 NicheDocs across 7 channels, all cells satisfy ≥5
per length_kind.

Frontend updated: `web-next/lib/types.ts` NicheDoc → nested shape +
no voice; `web-next/components/app/niche-form-dialog.tsx` reads
nested fields + per-section setters; `web-next/app/app/create/page.tsx`
reads `n.source.kind` / `n.source.ref`; AI-picks bubble dropped
(AI is implicit).

## Form vocabulary impedance — why bubble selection ≠ source_kind tuple

The wizard's customization form has its own `source_kind` field driven
by `pipeline/schemas/customization.py:_source_kinds_for(entry)`. Its
options are render-time **input shapes** the user can supply:

```python
base = ["auto", "user_text"]
if key in {"mystoriesanimated", "scrollpulse"}: base += ["reddit_url"]
if key in {"historyrecapped", "cosmosdecoded", "hindutavaanimated"}: base += ["wikipedia_topic"]
if key in {"sportsrecapped", "historyrecapped"}: base += ["youtube_video"]
```

NicheDoc's `source.kind` uses a **different vocabulary** — the source
PROVIDER, not the input shape:

```python
SourceKind = Literal["reddit", "wikipedia", "manual", "x_twitter", "youtube", "rss"]
```

These overlap conceptually (`reddit` ≈ `reddit_url`, `wikipedia` ≈
`wikipedia_topic`) but `manual`, `x_twitter`, `rss` have no form
counterpart, and `auto` / `user_text` have no NicheDoc counterpart.

**The bug:** the original niche-bubble click handler set
`onChange("source_kind", n.source.kind)` — passing the NicheDoc
vocabulary into the form's strict-option-list field. For the
`sportsrecapped` Top-5 Countdown bubble (whose `source.kind = "manual"`),
the customization form silently rejected the value (not in
`["auto", "user_text", "youtube_video"]`), the bubble's `selected`
check then evaluated to `false`, and the bubble appeared unclickable.

**The fix:** track niche-bubble selection by **variant key** (the niche
identity), NOT by the source_kind/source_ref tuple. The variant is
what the renderer actually keys on — the form's source_kind is a
separate "where to pull from" concern the user customizes after picking
the niche.

```typescript
// web-next/app/app/create/page.tsx — Niche CardShell
const selected = variant === n.key;        // ← single source of truth
onClick={() => onVariantChange(n.key)};    // ← decoupled from form
```

**Hard rule:** any UI control that maps a NicheDoc field to a
customization-form field MUST go through an explicit translator. Direct
passes between the two vocabularies will silently fail the moment a
NicheDoc gets a value the form's enum doesn't list.

## Deploy flow — when a niche change ships

A niche change can require any combination of three deploys depending
on what changed:

| changed                                            | needs                                                                |
|----------------------------------------------------|----------------------------------------------------------------------|
| only the seed dict in `scripts/seed_channel_niches.py` (default niche pool) | Re-run seed against GCS — `YTFACTORY_STATE_BUCKET=ytfactory-prod-v2-state .venv/bin/python scripts/seed_channel_niches.py`. Browser hard-reload picks it up; **no service redeploy**. |
| `pipeline/niche_specs.py` schema (NicheDoc, validators, helpers) | `cloud/web-server/deploy.sh` (the FastAPI backend reads/validates the JSONs). |
| `web-next/app/...` or `web-next/lib/...` UI / type changes | `cd web-next && npm run build && cd .. && cloud/web-next/deploy.sh`. |
| `pipeline/channels.yaml`, `pipeline/channels.py`, render code | `cloud/web-server/deploy.sh` AND the cloud render-worker job (separate). |
| any of the above → both services AND seeds        | All three, in this order: schema deploy (backend) → seeds (GCS) → frontend deploy. |

**Failure mode that triggered this rule (2026-05-11):** new nested-shape
NicheDocs were pushed to GCS while the backend was still running the
old flat-shape pydantic model. The validator silently rejected every
doc → `list_niches()` returned `[]` → `/api/channels/<ch>/niches`
returned `{niches: []}` → wizard showed "No short niches authored for
this channel yet" even though 77 NicheDocs were in GCS. The frontend
had been redeployed with the new copy, so the user saw the *new*
empty-state text — convincingly wrong.

**Diagnostic order when "no niches" persists after a seed push:**

1. `gsutil ls gs://ytfactory-prod-v2-state/<channel>/niches/` — confirm
   the JSONs landed.
2. `gsutil cat gs://.../<niche>.json | head` — confirm shape matches
   what the deployed backend's NicheDoc validator expects.
3. `gcloud run revisions list --service=ytfactory-web` — check the
   active revision was deployed *after* the schema change.
4. If the backend revision is older than the schema commit, redeploy
   it: `cloud/web-server/deploy.sh`.

## Cross-references

- `pipeline/niche_specs.py` — the schema + store + canonical helpers
- `pipeline/channels.py` — Channel model (channel-only) + re-exports
- `pipeline/channels.yaml` — channel manifest (no niches block)
- `pipeline/paths.py` — RenderPaths.from_channel_yaml multi-form match
- `pipeline/schemas/customization.py` — form `_source_kinds_for(entry)` (different vocab from NicheDoc.source.kind — see "Form vocabulary impedance" above)
- `scripts/seed_channel_niches.py` — default niche pool + validation gates
- `control/routes/niche_specs_routes.py` — `/api/channels/<ch>/niches` CRUD + AI-draft
- `web-next/lib/types.ts` — TS NicheDoc mirror (nested)
- `web-next/app/app/create/page.tsx` § Niche CardShell — bubble selection by `variant`, not source_kind
- `docs/channel_layout.md` — channel-side layout rules (links here for niche routing)
- `docs/channel_niche_pool.md` — wizard-side data flow (links here for the schema)
