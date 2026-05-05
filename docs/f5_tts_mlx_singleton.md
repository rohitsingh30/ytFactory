# F5-TTS-MLX must reuse a loaded model, not call `generate()` per-chunk

## Symptom
Long-form sleep render (178 chunks of a ~50k-char narration) takes ~3-5 min
per chunk on M2 Max — total ~9 hours. The narration cache fills slowly even
though MLX inference itself is fast.

## Root cause
The upstream helper `f5_tts_mlx.generate.generate()` instantiates the model
**inside the function body**:

```python
# .venv/lib/python3.14/site-packages/f5_tts_mlx/generate.py:131
def generate(generation_text, ..., quantization_bits=None, output_path=None):
    ...
    f5tts = F5TTS.from_pretrained(model_name, quantization_bits=quantization_bits)
    ...
```

So every call reloads the 1.35 GB checkpoint and re-triggers `mx.compile`
of the ODE step function. On a 178-chunk batch you pay that cost 178x.

## Fix
`pipeline/audio.py` now caches the model + ref audio in module-level
singletons and calls `f5tts.sample(...)` directly, replicating the body of
`generate()` (lines 144-195 of generate.py) without the per-call load:

```python
_F5_MODEL = None
_F5_REF_CACHE: dict[str, tuple] = {}

def _f5_get_model(quantization_bits=None):
    global _F5_MODEL
    if _F5_MODEL is None:
        from f5_tts_mlx.cfm import F5TTS
        _F5_MODEL = F5TTS.from_pretrained("lucasnewman/f5-tts-mlx", quantization_bits=quantization_bits)
    return _F5_MODEL

def _f5_get_ref(ref_audio_path):
    if ref_audio_path in _F5_REF_CACHE:
        return _F5_REF_CACHE[ref_audio_path]
    # ... load, RMS-normalize to TARGET_RMS=0.1, cache as mx.array
```

Per-chunk cost drops from ~3-5 min to ~25-35 s on M2 Max with the default
`rk4` / `steps=8` settings. A 178-chunk job goes from ~9 hr to ~1.5 hr.

## Measured (italian-campaign-1943-1945-sleep, 2026-05-04)
| chunk | chars | audio out | wallclock |
|-------|-------|-----------|-----------|
| 0022 | 379 | 24.0s | 34.0s (first call, includes `mx.compile`) |
| 0023 | 335 | 21.2s | 27.0s |
| 0024 | 316 | 20.0s | 25.9s |
| 0025 | 169 | 10.7s | 15.5s |
| 0026 | 334 | 21.1s | 27.0s |

## Other knobs (not used by default — quality drops are visible on a sleep mix)
- `method="euler"` — 4x fewer transformer evals than rk4 (rk4 = 4 evals/step)
- `steps=6` — 25% fewer evals than steps=8 default
- `quantization_bits=4` — ~1.5-2x faster, ~10% memory

The patched `_synth_f5_tts` accepts `steps`, `method`, `quantization_bits`
kwargs but defaults remain `rk4` / `steps=8` / no quant for output parity
with prior renders. Switch only if you re-render the whole batch — mixing
settings within a job creates audible audio-character shifts at chunk joins.

## Why this isn't applied to Kokoro / Chatterbox
Both already use module-level singletons (`_KOKORO_INSTANCE`,
`_CHATTERBOX_MODEL`) — only F5-TTS was bypassing the pattern by calling the
top-level `generate()` helper instead of the underlying `F5TTS.sample()`.
