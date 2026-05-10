# Cloud Run local-fallback pattern — `_local_fallback_or_raise`

**Established 2026-05-10** after the post-laptop-nuclear-cleanup
venv (no `mlx`, no `torch`, no `kokoro`) made the existing
"local fallback on CloudRunUnavailable" pattern actively harmful:
the fallback raised `ModuleNotFoundError` which masked the original
`CloudRunUnavailable`, leaving callers unable to distinguish "cloud
is sick, retry later" from "your venv can't even attempt the
fallback".

## The rule

Every cloud-provider wrapper that has a local-fallback path MUST
route the fallback through a single helper that:

1. Honours `<PROVIDER>_DISABLE_FALLBACK=1` for canary/test runs
   (re-raises the cloud error immediately).
2. Catches `ImportError` / `ModuleNotFoundError` from the local
   fallback's lazy imports and **re-raises the original
   CloudRunUnavailable** instead, with a warning log. The caller
   gets a single, consistent failure mode — "cloud unavailable AND
   no working local provider in this venv" — rather than a confusing
   `ModuleNotFoundError: No module named 'mlx'` that suggests an
   install problem when the actual problem is the cloud service.

The canonical helper is in `pipeline/tts/cloudrun.py`:

```python
def _local_fallback_or_raise(cloud_err, fallback_label, fallback_call):
    if _fallback_disabled():
        raise cloud_err
    try:
        return fallback_call()
    except (ImportError, ModuleNotFoundError) as ie:
        logger.warning(
            "%s local fallback unavailable in this venv (%s); "
            "re-raising CloudRunUnavailable", fallback_label, ie,
        )
        raise cloud_err
```

## Pattern at every call site

```python
def _synth_cloudrun_<model>(text, ref_audio_path, ref_audio_text,
                            out_path, speed, seed=None):
    try:
        return _synth_cloudrun(model="<model>", ...)
    except CloudRunUnavailable as e:
        logger.warning("cloudrun_<model> unavailable (%s); falling back to %s", e, "<local>")
        def _fb():
            from pipeline.tts.<local> import _synth_<local>  # lazy
            return _synth_<local>(...)
        return _local_fallback_or_raise(e, "cloudrun_<model>", _fb)
```

The closure (`_fb`) defers the import until the fallback is actually
attempted. The helper catches the import failure cleanly. The
`_fallback_disabled()` env gate stays at the helper boundary so the
canary path (`CLOUDRUN_TTS_DISABLE_FALLBACK=1`) doesn't suddenly
swallow `ModuleNotFoundError`.

## Why not just `if MODEL_AVAILABLE: ...` at module load?

Tried first; rejected because:

- The local-provider import is the heaviest in the venv (`mlx`,
  `torch`, `chatterbox`). A module-level `try: import mlx` adds
  several seconds of cold-start to every renderer process even when
  the cloud path is healthy and the import is wasted.
- `MODEL_AVAILABLE` flags accumulate per-provider and don't compose
  with new providers without code changes.
- The lazy + helper pattern is one function the next provider
  inherits for free.

## Audit recipe

```bash
grep -nE "if _fallback_disabled\(\)|raise CloudRunUnavailable" \
  pipeline/tts/cloudrun.py pipeline/images/images_cloudrun.py \
  | grep -v "_local_fallback_or_raise"
```

Any match is a fallback site that doesn't go through the helper —
either rewrite to use it OR document why (e.g., the local fallback
has zero new imports and is guaranteed available in the venv).

## Where this lives

- `pipeline/tts/cloudrun.py::_local_fallback_or_raise` — canonical impl.
- `pipeline/tts/cloudrun.py::_synth_cloudrun_{f5,higgs,cosyvoice,
  chatterbox,indicparler,indicf5}` — every TTS provider wires through it.
- `pipeline/images/images_cloudrun.py` — image providers SHOULD adopt
  the same pattern (currently uses an inline render-level circuit
  breaker; lower priority because all image providers have a uniform
  local fallback to `mflux z_image_turbo`).

## See also

- `feedback_cloudrun_local_fallback_pattern.md` — memory entry mirroring this.
- `docs/cloudrun_tts.md` — cloud TTS architecture + per-provider fallback
  policy (Hindi → Kokoro hf_alpha; English → F5-TTS).
- `docs/cloud_tts_only_2026_05_07.md` — the 2026-05-07 cloud-cutover
  decision that made the local fallback an "edge-case for laptop
  dev" rather than a primary path.
