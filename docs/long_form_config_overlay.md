# Long-form per-render config overlay (`--config` flag transport)

**Established 2026-05-12** as Slice-2.P2 of the unified-renderer
rollout, after the audit found that `pipeline/render/video.py:render_long_form`
shelled out to `pipeline/render/long_form.py` with only `--channel` +
`--slug` and zero override forwarding — every spec field except
`duration_target_s` was silently dropped on the long-form path.

## The rule

`pipeline/render/long_form.py` accepts `--config <yaml_path>`. When
set, the YAML at that path is **deep-merged** onto the channel cfg
before any cfg-driven branching:

```bash
python -m pipeline.render.long_form \
  --channel mystoriesanimated \
  --slug aita-roommate \
  --config /tmp/work/long_form_overlay.yaml
```

The overlay carries form-driven values the user picked. The deep-merge
preserves sibling keys: `{"long_form": {"tts_voice": "x"}}` overlays
`cfg["long_form"]["tts_voice"]` without wiping `cfg["long_form"]["output_resolution"]`
or any other sibling.

## Why deep-merge, not flat replace

The long-form `cfg["long_form"]` block is dense — typically 15-20
keys (TTS provider, voice, atempo, chunk targets, output resolution,
fps, panel max count, image provider/seed/steps, music bed, audio
mix levels, captions enabled, caption style). A flat replace by a
partial overlay would wipe everything the overlay didn't explicitly
set.

`_deep_merge_dict(dst, src)` recurses through `src`, replacing scalar
values + lists, but recursively merging sub-dicts so a partial
`long_form:` overlay leaves the rest of the channel's `long_form:`
block intact. Behavior matches the long-standing variant-overlay
pattern in `RenderPaths.from_channel_yaml` and the cloud-worker's
base+variant cfg merge.

## How `video.render_long_form` builds the overlay

```python
# pipeline/render/video.py::render_long_form
overlay = long_form_overlay_from_spec(spec)
overlay_path = work_dir / "long_form_overlay.yaml"
overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=True))

cmd = [
    sys.executable, "-m", "pipeline.render.long_form",
    "--channel", channel_arg,
    "--slug", env.slug,
    "--config", str(overlay_path),
]
```

`long_form_overlay_from_spec(spec)` lives in
`pipeline/render/input_registry.py` and walks the descriptor registry
(`pipeline/schemas/customization.py:CustomizationField` instances)
projecting each `spec_field` value into the cfg target paths the
descriptor declared. See `docs/input_descriptor_registry.md` for the
descriptor pattern.

## Empty overlay = no-op

When no spec fields are set (laptop CLI workflow with no form
overrides), `long_form_overlay_from_spec` returns `{}`, the overlay
file is empty, and `_main_impl` skips the merge. The legacy `python
-m pipeline.render.long_form --channel X --slug Y` invocation
continues to work without changes — the overlay is purely additive.

## Testing

`tests/test_long_form_overlay_integration.py` — 6 tests:

- Round-trip: `music_bed=cinematic` form pick → overlay carries
  `cinematic.mp3` at top-level + `long_form:` nested → after
  `_deep_merge_dict`, both locations updated, channel YAML siblings
  intact.
- `captions_density=dense` → same round-trip, both locations.
- **Sibling preservation** — overlay touching `long_form.music_bed_default`
  MUST NOT wipe `long_form.tts_provider` / `output_resolution` /
  `image_provider` / `panel_max_count`. (The exact bug a flat-merge
  would produce.)
- **AST guard** — `--config` is a registered argparse arg on
  `long_form.py`. Catches a regression where the parser definition
  drifts and the orchestrator's `--config` arg silently goes
  unparsed.
- `_deep_merge_dict` unit test — overrides existing keys, adds new
  keys, leaves untouched keys alone.

## Files

- `pipeline/render/long_form.py:_deep_merge_dict` — the merge
  primitive.
- `pipeline/render/long_form.py:_main_impl` — reads `args.config`,
  loads the YAML, calls `_deep_merge_dict(config, overlay)`.
- `pipeline/render/long_form.py:main()` argparse — declares
  `--config` flag.
- `pipeline/render/video.py:render_long_form` — writes the overlay
  file + passes `--config` to subprocess.
- `pipeline/render/input_registry.py:long_form_overlay_from_spec` —
  descriptor-driven overlay builder.

## See also

- [`docs/input_descriptor_registry.md`](input_descriptor_registry.md) —
  the descriptor system that produces overlay contents.
- Memory: `feedback_long_form_per_render_overlay.md`.
