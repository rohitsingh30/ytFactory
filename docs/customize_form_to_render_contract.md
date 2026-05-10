# Customize-form → render contract (3-layer wiring rule)

> **Established 2026-05-10** during the Voice/Song flip + Background-visuals
> deploy. Two CLASS-OF-BUGs surfaced, both caused by an incomplete
> understanding of how a "new field on the create page" actually reaches
> the renderer in production.

## The rule

Any new user-facing knob on the Customize step (`web-next/app/app/create/page.tsx`)
must be wired through **all three** of these layers, in the same change.
Skipping any one silently no-ops the knob in production:

| Layer | File | What it does |
|---|---|---|
| 1. **Schema** | `pipeline/schemas/customization.py` | Adds the field to `get_customization_schema(...).fields` so the frontend renders the control with sensible per-channel defaults. Also populates a corresponding column on `ChannelSummary` if the field's default depends on a channel YAML key. |
| 2. **Form submit** | `web-next/app/app/create/page.tsx::submit()` | Pulls the value out of `values[k]` and packs it into the `channel_overrides` dict on the `/api/render` request. Each new key needs an entry in the local `passthrough` array. |
| 3. **Worker subprocess** | `cloud/render-worker-v2/entrypoint.py::_run_renderer_subprocess` | Translates `proposal.channel_overrides` from Firestore into repeated `--override KEY=VALUE` flags on the `python -m pipeline.render.shorts` command line. Without this, the field round-trips through Firestore and dies at the worker boundary. |

The renderer side is already generic — `pipeline/render/shorts.py::_apply_form_overrides`
maps each known key to a cfg slot, and `--override` accepts any `KEY=VALUE`.
Adding a new field there is one line in `_apply_form_overrides` plus the
schema entry above; the worker forward is **already wired** as a pass-everything
loop, so step 3 is automatic for any future field.

## Worked example: 2026-05-10 Voice/Song flip + Background visuals

5 new schema fields shipped: `audio_mode`, `song_style`, `song_vocal_gender`,
`song_model`, `visual_source`. The first cloud render after deploy was
the verification — the Customize form's defaults landed on the production
schema endpoint:

```json
// rhymetimejunction (audio_provider: external_song)
audio_mode default = "song"
visual_source default = "ai"
song_style default = "cheerful upbeat children's nursery rhyme..."

// mystoriesanimated (audio_provider not set → "tts")
audio_mode default = "voice"
visual_source default = "ai"
```

## Anti-pattern that this rule prevents

Pre-2026-05-10, the worker had no `--override` translation. New customization
fields could be added to `pipeline/schemas/customization.py` and rendered
correctly in the UI, but the value was silently dropped at the worker
subprocess. Symptom: the form picked "Song" but the cloud render still
synthesized TTS narration because the channel YAML's `audio_provider: tts`
default never got overridden. No error, just wrong output.

The 3-layer rule above is mechanical — write the schema field, add it to
the submit `passthrough` array, deploy. The worker auto-forwards.

## Sibling rule: never hardcode `<channel>/<path>` in cloud Dockerfiles

Same root cause (out-of-band channel paths in cloud images), different
symptom. Pre-2026-05-10 the render-worker Dockerfile had:

```dockerfile
COPY mystoriesanimated/config.yaml mystoriesanimated/variants/ /workspace/mystoriesanimated/
COPY rhymetimejunction/config.yaml /workspace/rhymetimejunction/
# ... 6 more lines like this
```

…all referencing the pre-2026-05-05 layout where channel YAMLs lived at
`<slug>/config.yaml`. The 2026-05-05 reorg moved them to
`pipeline/channels/<slug>.yaml` (per `pipeline.channels.CHANNELS_CONFIG_DIR`),
which broke `gcloud builds submit` with `file not found in build context`
on every deploy attempt — but nobody had tried to deploy this image since
the reorg.

**Rule:** any cloud image that needs channel YAMLs gets them via the
wholesale `COPY pipeline/ /workspace/pipeline/` (which already covers
`pipeline/channels/`, `pipeline/variants/`, and `pipeline/channels.py`).
The runtime resolver in `pipeline.channels._channel_yaml_path(slug)` is
the single source of truth — Dockerfiles and worker code should call it,
never duplicate the path layout.

## Files touched 2026-05-10

- `pipeline/schemas/customization.py` — added the 5 fields + `audio_provider` on ChannelSummary
- `pipeline/render/shorts.py` — `make_short(cfg_overrides=...)` + `_apply_form_overrides()` + `--override` CLI flag
- `web-next/app/app/create/page.tsx` — `<AudioSection>` flip + `<VisualSourceCard>` + submit `passthrough`
- `web-next/components/app/song-picker.tsx` — new
- `cloud/render-worker-v2/entrypoint.py` — `--override` forward + `_channel_yaml_for` defers to canonical resolver
- `cloud/render-worker-v2/Dockerfile` — dropped 8 stale per-channel COPY lines

## Reference

- 3-layer contract source-of-truth: this doc.
- Voice/Song + visual_source plan: `~/.copilot/session-state/<id>/plan.md` (transient).
- Render worker runbook: [`docs/cloudrun_render_worker.md`](./cloudrun_render_worker.md).
- Channel layout (note: stale 2026-05-10, see `pipeline.channels.CHANNELS_CONFIG_DIR` for canonical paths): [`docs/channel_layout.md`](./channel_layout.md).
