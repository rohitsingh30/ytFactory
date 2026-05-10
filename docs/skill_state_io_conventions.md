# Skill state I/O conventions (cloud-first, schema-first)

Two contracts skills follow:

1. **State I/O** — all channel JSON state lives in
   `gs://ytfactory-prod-v2-state/<channel>/<kind>/<slug>.json`,
   accessed via `pipeline.state_client`. Laptop holds zero channel
   data.
2. **NicheVideo schema** — every narration JSON conforms to the
   universal `NicheVideo` Pydantic model in `pipeline/niche_schema.py`.
   Same shape across all 22 niches; only values differ. State API
   rejects non-conformant narrations with HTTP 422.

This doc is the contract. Every `/make-*`, `/critique-*`, and
authoring skill follows it.

---

## What changed

**Before (pre-cutover):**

```bash
# Skills used Claude Code's local Read/Edit/Write on channel files.
Write historyrecapped/narrations/<slug>.json     ← local disk write
Read  historyrecapped/narrations/<slug>.json     ← local disk read
Edit  historyrecapped/cast/<slug>.json           ← local disk edit
```

The launchd `com.ytfactory.state-sync.plist` mirrored those local
files to GCS so the cloud render-worker could see them.

**After (this contract):**

```bash
# Skills use pipeline.state_client. All I/O goes straight to GCS.
echo '{...}' | python -m pipeline.state_client put historyrecapped narrations <slug>
python -m pipeline.state_client get historyrecapped narrations <slug>
python -m pipeline.state_client list historyrecapped narrations
python -m pipeline.state_client delete historyrecapped narrations <slug>
```

State-sync is retired. No laptop file is created.

---

## Path → state_client translation

When a skill body refers to a file path like
`<channel>/<kind>/<slug>.json` (in any of its forms — output schema
docs, quality gate checks, "wrote …" report-back lines), it means a
**GCS object**, not a laptop file. Translate the path one of these
ways:

| Skill body says | State client invocation |
|---|---|
| Write `<channel>/<kind>/<slug>.json` | `cat <<'JSON' \| python -m pipeline.state_client put <channel> <kind> <slug>` |
| Read `<channel>/<kind>/<slug>.json` | `python -m pipeline.state_client get <channel> <kind> <slug>` |
| Edit `<channel>/<kind>/<slug>.json` | `get` → modify in memory → `put` (no in-place edit; PUT overwrites) |
| `ls <channel>/<kind>/` | `python -m pipeline.state_client list <channel> <kind>` |
| `rm <channel>/<kind>/<slug>.json` | `python -m pipeline.state_client delete <channel> <kind> <slug>` |

For multi-line JSON bodies, prefer a heredoc:

```bash
python -m pipeline.state_client put historyrecapped narrations apollo-11-1969 <<'JSON'
{
  "slug": "apollo-11-1969",
  "narration": "...",
  "title_options": [...]
}
JSON
```

Or write the JSON to a temp file first and pass `--file`:

```bash
cat > /tmp/apollo.json <<'JSON'
{...}
JSON
python -m pipeline.state_client put historyrecapped narrations apollo-11-1969 --file /tmp/apollo.json
```

---

## What stays on laptop (NOT moved by this contract)

The state API is for **per-render dynamic JSON** authored by skills.
The following stay on laptop as immutable code/config:

- `<channel>/learnings/*.md` — channel-specific instruction docs
- `<channel>/config.yaml` — channel render config
- `<channel>/scripts/*.py` — channel-specific Python entrypoints
  (also baked into the cloud render-worker image)
- `<channel>/branding/`, `<channel>/music/`, `<channel>/footage/` —
  asset libraries (kept in the render-worker image; laptop copies
  may be retired in Phase 5)
- `pipeline/`, `cloud/`, `docs/`, `tests/`, `.claude/`, `CLAUDE.md`

If a skill needs to read a channel's `learnings/channel.md` or
`config.yaml`, it still uses Claude Code's `Read` tool against the
local file — those are read-mostly config the skill consumes as
context.

---

## NicheVideo schema (narrations only, for now)

Every narration JSON PUT to `kind=narrations` must conform to
`pipeline.niche_schema.NicheVideo`. The shape:

```python
{
  "schema_version": "1",
  "channel": "<channel-slug>",        # e.g. "historyrecapped"
  "niche": "<niche-key>",              # e.g. "history-short" — see NICHE_REGISTRY
  "slug": "<event-year>",              # alnum + dash + dot + underscore
  "title": {
    "options": ["...", "...", "..."],   # 1-10 candidate titles
    "selected_index": 0
  },
  "hook": "<one-line pitch>",
  "narration": {
    "text": "<full spoken narration>",
    "word_count": 134,                  # producer fills in (optional but recommended)
    "duration_target_s": 56.0           # optional
  },
  "beats": [                             # ordered narrative units
    {
      "index": 0,                        # must be 0..N-1 in order
      "role": "<niche-specific>",        # e.g. "place_date" / "comment" / "rank_5" / "chapter_1"
      "text": "<spoken line>",
      "key_visual": "...?",               # optional — animated paths
      "scene": "...?",                    # optional — animated paths
      "footage_window": {                 # optional — archival paths
        "source_url": "https://www.youtube.com/watch?v=...",
        "in_s": 20.0,
        "out_s": 28.5,
        "match_text": "..."
      },
      "extras": {}                        # niche-specific data (e.g. {author: "u/foo"} for reddit)
    },
    /* ... */
  ],
  "closer": {
    "spoken": "<literal closer text — must match validation.closer_literal_options if set>",
    "style": "<panel-style: military|general|aita-vote|brain-rot|mythological>"
  },
  "render": {
    "pipeline": "footage_only|shorts|long_form|long_form_doc|split_screen|tweet_reaction|rivalry_recap",
    "aspect": "9:16|16:9",
    "tts_provider": "cloudrun_chatterbox|cloudrun_indicf5|...",
    "tts_voice": "sarah.wav|hindi-female-iitm-anchor|...",
    "image_provider": "cloudrun_flux2_klein|null"
  },
  "metadata": {
    "sources": [{"url": "...", "type": "wikipedia"}],
    "pronunciation_notes": "<optional>",
    "research_notes": "<optional>",
    "tags": []
  },
  "validation": {                          # niche-specific gates as data
    "word_count_band": [130, 138],
    "min_numbers_in_narration": 3,
    "max_duration_s": 60,
    "closer_literal_options": ["LIKE to honor those who served. SUBSCRIBE for more such stories."],
    "banned_phrases": ["smash that subscribe button"],
    "required_beat_roles": ["place_date", "stakes", ..., "closer"]
  },
  "extras": {}                             # niche-specific overflow
}
```

Niches differ in *values*, not *structure*:

| Niche | beats[].role examples | render.pipeline |
|---|---|---|
| history-short | place_date / stakes / subject / plan / complication / action / twist / resolution / cost / meaning / closer | footage_only |
| aita-animated | hook / context / incident / conflict / decision / twist / closer | shorts |
| reddit-thread | post / comment / comment / comment / closer | split_screen |
| hindutava-katha | chapter_1 / chapter_2 / ... / closer | long_form |
| sports-last5 | hook / rank_5 / rank_4 / rank_3 / rank_2 / rank_1 / closer | footage_only |

**Producer pre-fills:** call `pipeline.niche_schema.niche_defaults("<niche>")`
to get a dict of `{default_render: {...}, default_validation: {...}}` to
seed your payload. Skill bodies still describe niche-specific
authoring rules (10-beat arc, banned phrases, exemplar references) —
the schema enforces structure, the skill body enforces craft.

**Validation runs at PUT time.** A non-conformant payload returns
HTTP 422 with the structured Pydantic error list (which fields are
missing / wrong type / out-of-band). Read the error, fix the payload,
retry. No skill needs to do its own pre-validation.

**Schema export:** for tooling / web-form generators,
`pipeline.niche_schema.schema_dict()` returns the JSON Schema.

## Allowed kinds

The state API whitelists these `kind` values. Anything else returns
HTTP 400.

| Kind | Purpose |
|---|---|
| `narrations` | The authored narration JSON — the spine of every render |
| `cast` | Per-story character description (sportsrecapped, mystoriesanimated) |
| `shotlist` | Footage windows (archival channels) or shot list |
| `uploads` | Post-render upload record (YouTube ID, title, description) |
| `raw` | Source dossier (cosmosdecoded `raw/<slug>.json`, AITA scrape) |
| `footage_plan` | Long-form sports doc footage plan |
| `chapters` | Long-form chapter plan (hindutavaanimated long-form) |
| `critiques` | `/critique-video` markdown sidecars |
| `scripts` | Pre-narration script JSON (older /make-script outputs) |
| `_holds` | Per-channel holds registry (one slug = one channel) |

Slug must match `^[A-Za-z0-9_][A-Za-z0-9_.\-]*$` — alnum + underscore
+ dot + dash, can't start with a dot. `<slug>-short` and
`<slug>-short.pronounce` both pass (they're treated as the slug).

---

## Niche-nested layout

Some channels (mystoriesanimated, sportsrecapped) use the niche-nested
layout: `<channel>/<niche>/<kind>/<slug>.json`. **State API v1 supports
flat layout only.** For niches, encode the niche into the slug:

| Old layout | State client call |
|---|---|
| `mystoriesanimated/reddit_amitheasshole/narrations/<slug>.json` | `put mystoriesanimated reddit_amitheasshole.narrations.<slug>` is NOT supported — use a niche-aware skill setup that picks a flat alternative slug, OR wait for state API v2. |

For now, niched channels can write to a flat `<channel>/narrations/<slug>.json` with the niche prefix in the slug (e.g. `aita-cooking-<slug>`). The render-worker has been updated to accept either layout.

If your skill is heavily niche-dependent and this is awkward, flag it
in the skill body and proceed with the flat layout — niche routing on
the state API is a Phase 2.5 follow-up.

---

## Auth

The state API requires either:
- Cloud Run trust-IAM (`K_SERVICE` env set automatically), OR
- Bearer `YTFACTORY_AGENT_TOKEN` for laptop-side calls.

The `pipeline.state_client` reuses `pipeline.skill_dispatch`'s
gcloud-issued ID-token machinery, so a normal `gcloud auth login`
session is sufficient. No skill needs to handle auth explicitly.

---

## Local override (dev iteration)

For dev iteration without hitting the cloud:

```bash
export YTFACTORY_WEBSITE_URL=http://localhost:8765
PYTHONPATH=. .venv/bin/uvicorn control.server_dev:app --host 127.0.0.1 --port 8765
```

The `pipeline.state_client` will hit localhost (skipping ID-token
auth, falling back to `YTFACTORY_AGENT_TOKEN` Bearer). The local
control plane reads/writes the same `gs://ytfactory-prod-v2-state`
bucket — no separate dev bucket exists in v1.

To use a dev bucket: `export YTFACTORY_STATE_BUCKET=my-dev-state`.

---

## Migration status by skill (Phase 3 progress)

Skills in `.claude/skills/` are migrated to this contract one at a
time. A skill is "migrated" when:

1. It has a banner at the top of its `SKILL.md` linking to this doc.
2. Its Write / Read / Edit / list / delete instructions on channel
   JSON files use `pipeline.state_client` invocations, not Claude
   Code's `Write` / `Read` / `Edit` tools on local paths.
3. Its quality gates fetch via state_client when validating against
   already-authored state.

Track migration status in
`docs/skill_state_io_migration_status.md`.

---

## Why this contract exists

Pre-2026-05-09, the laptop accumulated `<channel>/...` directories
forever — every authored Short, every cast.json, every critiques
sidecar. The launchd state-sync plist mirrored them to GCS so the
cloud render-worker could see them, then nothing ever cleaned up the
laptop side.

Post-cutover the goal is: laptop has Claude Code (orchestrator) +
Playwright agent (cross-engagement). Zero data. The state API is the
mechanism: one place for the dynamic JSON, GCS-backed, auth-gated,
schema-validated at the route level.

When you author a new skill, follow this contract from day 1. When
you edit an existing skill, migrate it as you touch it. The migration
status doc tracks the front.
