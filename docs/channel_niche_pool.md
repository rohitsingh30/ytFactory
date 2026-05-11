# Channel niche pool — source of truth + render flow

The /create wizard's **Niche** bubble row (per-channel pills like
`r/AmItheAsshole` · `r/tifu` · `r/TrueCrime` · `Top-5 Countdown`) is a
filtered slice of each channel's niche pool. This doc is the canonical
description of how that pool is stored, served, and seeded — and why
hardcoding it on the front-end is the wrong reflex.

## Data flow (end-to-end)

```
scripts/seed_channel_niches.py     ──┐  edit + re-run to add a niche
                                     │
pipeline.niche_specs.save_niche()  ──┤  pydantic-validated NicheDoc
                                     │
gs://ytfactory-prod-v2-state/        │  canonical store (GCS)
   <channel>/niches/<key>.json     ──┤
                                     │
GET /api/channels/<ch>/niches      ──┤  control/routes/niche_specs_routes.py
                                     │  (mounted on ytfactory-web)
                                     │
nichesApi.list(channel)             ─┤  web-next/lib/api.ts
                                     │
web-next/app/app/create/page.tsx   ──┘  filters by length_kind, renders
   "Niche" CardShell                    bubbles tagged short / long
```

**Mirror to disk.** `save_niche()` ALSO writes
`<channel>/niches/<key>.json` to the local repo so dev workflows
without GCS can still iterate. The mirror is best-effort — read-only
filesystems (Cloud Run containers) skip it gracefully when the env
`YTFACTORY_STATE_BUCKET` is set.

## How to add / rename / remove a niche

The seed script is the **source of truth for default niches**. User-
created niches (via `POST /api/channels/<ch>/niches` from the
"Add new niche" UI on the channel page) coexist; the seeded ones can
be re-overwritten by re-running the script.

```bash
# 1. Edit the dict at the top of scripts/seed_channel_niches.py.
$EDITOR scripts/seed_channel_niches.py

# 2. Push to prod GCS (this writes the JSONs that the UI reads).
YTFACTORY_STATE_BUCKET=ytfactory-prod-v2-state \
    .venv/bin/python scripts/seed_channel_niches.py

# 3. Verify.
gsutil ls gs://ytfactory-prod-v2-state/<channel>/niches/

# 4. Hard-reload /app/create — bubbles update immediately
#    (no service redeploy needed; the API reads GCS on every call).
```

## NicheDoc schema (relevant fields)

The full schema lives in [`docs/niche_schema.md`](./niche_schema.md);
this section is a quick reference. NicheDoc has nested sub-specs:

```python
class NicheDoc(BaseModel):
    # identity / behavior (flat)
    key: str           # globally unique; matches <key>.json filename
    label: str         # bubble text ("r/AmItheAsshole")
    description: str
    length_kind: Literal["short", "long"]   # filters the bubble row
    format: NicheFormat                     # animated|footage|long_form|…

    # composed sub-specs
    source:    NicheSource     # { kind: reddit|wikipedia|manual|x_twitter|youtube|rss, ref }
    routing:   NicheRouting    # { channel, state_subdir, variant_yaml }
    templates: NicheTemplates  # { prompt_style_guide, hook, closer }
    aesthetic: NicheAesthetic  # { image_style, music_bed, thumbnail_style_key }

    created_at: str
    created_by: Literal["backfill", "user", "ai_chat"]
```

`length_kind` is the field the wizard's Form bubbles (Short / Long)
filter on. Keys are globally unique across all channels (the routing
map is keyed by `niche_key` alone). **Voice is deliberately not** a
niche field — voice picks live in the standalone voice catalog
(`web/server.py:VOICES` + `pipeline/voice_refs/`) and are selected
per-render. The pre-2026-05-11 flat shape (`source_kind` / `voice` /
`hook_template` / etc. at the top level) auto-loads via the
`_promote_legacy_flat_shape` validator.

## Why the wizard does NOT hardcode the pool in TypeScript

A previous run added a `FALLBACK_NICHE_POOL` dict in
`web-next/app/app/create/page.tsx` to "fix" empty pools when a channel
had no `NicheDoc`s authored. The user pushed back — correctly:

1. **Drift.** The TS hardcode and the GCS store would diverge silently
   the moment someone added a niche on either side.
2. **Cloud-first principle.** Same as the TTS / image / channel-niche
   data: the cloud bucket is the source of truth, the front-end is a
   thin renderer.
3. **No deploy needed for content edits.** With the seed script flow,
   adding `r/sports` for sportsrecapped is `edit dict + re-run`
   — no `npm run build`, no Cloud Run redeploy. (Schema-shape changes
   to NicheDoc *do* require backend redeploy — see
   [`docs/niche_schema.md`](./niche_schema.md) § "Deploy flow".)

The TS hardcode was reverted on 2026-05-11 in the same session that
introduced it. The seed script + GCS write is the only supported
path now.

## Currently seeded (2026-05-11)

77 NicheDocs across 7 channels (≥5 per cell — enforced by
`_assert_min_niches_per_cell()`):

| channel              | shorts | long | sample short                | sample long              |
|----------------------|-------:|-----:|-----------------------------|--------------------------|
| mystoriesanimated    |  11    |  5   | r/AmItheAsshole, r/tifu     | r/TrueCrime, r/nosleep   |
| scrollpulse          |   6    |  5   | r/AskReddit, r/Showerthoughts | AI/tech daily recap    |
| historyrecapped      |   5    |  5   | Today in History            | Wars & battles           |
| cosmosdecoded        |   5    |  5   | r/space — news              | How We Knew (decoder)    |
| hindutavaanimated    |   5    |  5   | Mahabharat katha            | Mahabharat — full kathaa |
| sportsrecapped       |   5    |  5   | Top-5 Countdown             | Rivalry recap            |
| rhymetimejunction    |   5    |  5   | Hinglish original           | Bedtime story narration  |

## Cross-references

- `pipeline/niche_specs.py` — `NicheDoc` schema + GCS/disk store
- `control/routes/niche_specs_routes.py` — `/api/channels/<ch>/niches`
- `scripts/seed_channel_niches.py` — the seeder (source of truth dict)
- `web-next/app/app/create/page.tsx` § `CustomizeReviewStep` — Niche
  bubble row implementation
- `docs/two_frontend_topology.md` — why edits in `web/static/` don't
  show up on prod `/app/create`
- `docs/discover_topic_generation.md` — the related Brainstorm flow
  that uses these same niches via `DiscoverContext`
