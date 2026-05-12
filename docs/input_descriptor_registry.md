# Input descriptor registry — single declaration per form input

**Established 2026-05-12** in response to a multi-hour debugging session
where the user's "input parameters not respected" report turned out to
be **dead code on the SHORT path** (`_apply_form_overrides` was a
parameter-bound function with zero callers — AST-confirmed) plus a
**completely missing override path on long-form** (`video.render_long_form`
forwarded only `target_duration_s`; 9 of 11 spec fields silently
dropped). Every fix attempt before the registry was a per-knob
band-aid that drifted again the next time someone added an input.

## The rule

Adding (or removing) a form input is a **single declarative edit** in
`pipeline/schemas/customization.py`. Declare the
[`CustomizationField`][CF] with `cfg_targets` / `spec_field` /
`apply_handler` / `prompt_patch_fn` metadata, and every consumer
below picks it up automatically:

- **Form UI** — already reads the schema via
  `/api/channels/<ch>/customization_schema`. The form's `submit()`
  handler runs the picks through
  `web-next/lib/render-payload.ts::buildChannelOverrides(values, schema)`,
  which walks `schema.fields` and includes every key in the request's
  `channel_overrides` automatically — no hand-maintained passthrough.
  Reserved at the top level: `topic`, `notes`, `source_kind`,
  `source_ref`, `format`, `length_s` (typed `ShortProposal` fields).
  Form-internal scaffolding: `length_minutes` (computed into `length_s`
  and dropped). Adding a new `CustomizationField` flows through to
  `channel_overrides` automatically. Pinned by
  `web-next/tests/render-payload.test.mjs` (19 tests including an
  EXTENSIBILITY case that asserts a synthetic NEW descriptor flows).
  Skipping this helper and hand-rolling a passthrough list was the
  2026-05-12 long-form-preview-shows-9:16 regression — see
  [[form-passthrough-schema-driven]].
- **SHORT path** — `pipeline/render/shorts.py:_apply_form_overrides`
  delegates to `pipeline.render.input_registry.apply_overrides`,
  which walks the descriptor list and writes each `cfg_target`.
- **LONG-FORM path** — `pipeline/render/video.py:render_long_form`
  builds a per-render YAML overlay via
  `input_registry.long_form_overlay_from_spec(spec)` and passes it
  to `pipeline/render/long_form.py` via the new `--config` flag,
  which deep-merges into the channel YAML before any cfg-driven
  branching.
- **Long-form rewriter prompt** —
  `pipeline.llm.rewrite_long_form.rewrite_long_form()` collects
  `input_registry.prompt_patches_for(spec)` and injects each
  descriptor-registered `PromptPatch` (context_lines, craft_rules,
  schema_mode for mode-changing patches like `audio_mode=song` →
  lyrics output).

## The four patterns (Patterns A–D)

Every form input wires through exactly one of these. Inline if/elif
branches anywhere else are **banned by `/tune-ai-extraction`** — the
descriptor registry is the only allowed surface for new inputs.

### Pattern A — input that affects a prompt → register a `PromptPatch`

```python
# pipeline/render/input_registry.py
@register_prompt_patch("audio_mode_branch")
def _audio_mode_patch(value, spec) -> PromptPatch:
    if value == "song":
        return PromptPatch(
            context_lines=["Audio mode is SONG — output will be sung by Suno"],
            craft_rules=["Author LYRICS (chorus + verse), not prose"],
            schema_mode="lyrics",
        )
    return PromptPatch()
```

Then on the descriptor:
```python
prompt_patch_fn="audio_mode_branch"
```

### Pattern B — input that affects a cfg dict → declare `cfg_targets`

```python
def _voice_field(default, language):
    return CustomizationField(
        ...,
        spec_field="voice_id",
        cfg_targets=[
            CfgTarget(path=["tts_voice"]),
            CfgTarget(path=["long_form", "tts_voice"]),  # both paths!
        ],
        apply_handler="apply_voice_with_cloud_carveout",
    )
```

Multiple `cfg_targets` mean one knob writes to multiple cfg
locations (typical: top-level for SHORT compose + `long_form:`
nested for the long-form renderer). The registry walks them all.

### Pattern C — LLM call site that lacks retry → wrap in a Contract

Mirror `pipeline/llm/contracts/rewrite_contract.py`:

```python
class LongFormRewriteContract(StageContract):
    def gather_constraints(self, ctx): ...
    def build_prompt(self, ctx, constraints): ...
    def output_schema(self): ...
    def validate(self, ctx, raw): ...
    def regen_prompt(self, ctx, raw, violations): ...
```

Then call site uses `run_stage(LongFormRewriteContract(), ctx,
llm_call=llm.call_claude_cli)`. Free retry + validation + regen.

### Pattern D — extraction lever (image neg-prompt, TTS pronunciation, ASR alignment) → bump fingerprint

```python
NEGATIVE_PROMPTS = "extra fingers, mutated hands, ..."
_PROMPT_VERSION = 4  # was 3 — invalidate every cached image
```

The fingerprint bump is **mandatory**: an extraction-lever change
that doesn't bust cache will silently NOT improve any cached
render.

## Bug classes the registry SOLVES STRUCTURALLY

- **Inline dead code** — descriptor metadata is the single declaration;
  no parallel hand-coded translator can drift away from it (the
  2026-05-12 `_apply_form_overrides` dead-code AST-bug).
- **Cross-path inconsistency** (knob honored on Short but dropped on
  long-form) — `cfg_targets` lists writes for BOTH paths in one
  descriptor; `long_form_overlay_from_spec` walks the same metadata.
- **Silent extraction loss** — extraction-lever changes register as
  named handlers / transforms; `/tune-ai-extraction` enforces a
  cache-fingerprint bump alongside any extraction-lever change.
- **Channel-specific knob drift** — descriptors are per-channel via
  `get_customization_schema(channel_key)`; channel-specific overlays
  picked up automatically.

## Validation

`tests/test_input_registry.py` — 22 tests covering:

- Per-factory descriptor metadata pinning.
- Single + multi-target writes; transform application; off→empty
  semantics for `music_bed_to_filename`.
- Apply-handler delegation (voice cloud carve-out path-style vs bare).
- Per-channel schema lookup end-to-end (the
  `mystoriesanimated` integration test that exercises every wired
  knob: audio_mode/song_*, voice, music_bed, captions_density,
  visual_source, length_s — all 9 cfg writes (top + long_form
  nested) green).
- Prompt-patch merging (`audio_mode_song + visual_source_footage`
  → both contribute, last `schema_mode` wins).
- The **EXTENSIBILITY** contract — register a synthetic transform
  + patch + descriptor; assert it flows through every consumer
  (apply_overrides, long_form_overlay_from_spec,
  prompt_patches_for) without other code changes.

`tests/test_long_form_overlay_integration.py` — 6 tests pinning the
deep-merge semantics so a partial `long_form:` overlay doesn't wipe
sibling keys, and AST-pinning the `--config` flag stays registered
on `pipeline/render/long_form.py` argparse.

## Files

- `pipeline/render/input_registry.py` — `PromptPatch`, `apply_overrides`,
  `long_form_overlay_from_spec`, `prompt_patches_for`,
  `register_transform`, `register_apply_handler`,
  `register_prompt_patch`. Built-in transforms (`identity`,
  `music_bed_to_filename`, `audio_mode_to_provider`) and handlers
  (`apply_voice_with_cloud_carveout`, `apply_song_style`).
- `pipeline/schemas/customization.py` — `CustomizationField` extended
  with `spec_field`, `cfg_targets`, `apply_handler`, `prompt_patch_fn`,
  `consumers`. `CfgTarget` Pydantic model. Each `_*_field()` factory
  populates the new metadata.
- `pipeline/render/shorts.py:_apply_form_overrides` — body replaced
  with delegation to `input_registry.apply_overrides`. Legacy
  hand-coded chain kept as `_legacy_apply_form_overrides` fall-back
  if the registry import fails.
- `pipeline/render/video.py:render_long_form` — writes
  `work_dir/long_form_overlay.yaml` from
  `input_registry.long_form_overlay_from_spec(spec)`; passes
  `--config` to subprocess; passes `spec=spec` to
  `rewrite_long_form()` so prompt patches fire.
- `pipeline/render/long_form.py` — `--config <yaml>` flag +
  `_deep_merge_dict()` helper (separate file: see
  `docs/long_form_config_overlay.md`).
- `pipeline/llm/rewrite_long_form.py:rewrite_long_form()` —
  optional `spec=spec` kwarg; collects + injects descriptor-driven
  prompt patches.

## Adding a new input — concrete recipe

```python
# pipeline/schemas/customization.py
def _new_input_field():
    return CustomizationField(
        key="new_input",
        label="New input",
        kind="select",
        options=[FieldOption(value="a", label="A"),
                 FieldOption(value="b", label="B")],
        default="a",

        # Renderer-side metadata — SINGLE source of truth.
        spec_field="new_input_canonical",      # mirrors onto RenderSpec
        cfg_targets=[
            CfgTarget(path=["cfg_key_short"]),
            CfgTarget(path=["long_form", "cfg_key_lf"], transform="my_transform"),
        ],
        apply_handler="optional_complex_logic",   # for business logic
        prompt_patch_fn="optional_prompt_branch", # for LLM mode changes
        consumers=["rewrite", "tts"],             # informational
    )
```

If `apply_handler` or `prompt_patch_fn` references a name not yet
registered, register it in `pipeline/render/input_registry.py` with
the matching decorator. Then add a line to the channel's
`compose_schema` builder so the field surfaces.

That's it. The form UI auto-renders the field. The SHORT renderer
honors it. The long-form renderer honors it. The long-form rewriter
prompt biases on it. No other code change needed.

## Removing an input — concrete recipe

Delete the descriptor declaration. Done.

If a `transform` / `apply_handler` / `prompt_patch_fn` was unique to
the removed descriptor, also remove its `register_*` decorator. The
form UI stops surfacing the field; the registry stops walking it; no
other consumer sees it.

## See also

- [`docs/long_form_config_overlay.md`](long_form_config_overlay.md) —
  per-render YAML overlay transport for the long-form renderer.
- [`docs/post_upload_analysis.md`](post_upload_analysis.md) — the
  classification taxonomy that fed every finding in the 2026-05-12
  rewrite.
- `.claude/skills/tune-ai-extraction/SKILL.md` — the standing AI
  quality audit + patch loop that USES this registry as its only
  allowed patch surface for new inputs / new prompt branches /
  new extraction levers.
- Memory:
  `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_descriptor_registry_replaces_dead_apply_overrides.md`.

[CF]: ../pipeline/schemas/customization.py
