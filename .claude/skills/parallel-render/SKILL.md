---
name: parallel-render
description: >-
  Batch-render N Shorts in parallel across the available cloud GPU capacity (each L4-backed Cloud Run service supports max-instances=2, so we can render 2 Shorts per service in parallel — today we render serial). Wraps existing pipeline/render/{shorts,long_form,footage_only,sports_doc}.py entry points with a concurrent.futures.ThreadPoolExecutor whose pool size is computed from GET /api/cloud/health. Pre-warms required services first via POST /api/cloud/warm, dispatches the batch, then collates per-render success/failure into a single report. Critical-path win: a 5-Short batch that takes ~50 min serial finishes in ~25 min. Use when the user says "parallel render", "/parallel-render", "render N shorts at once", "batch render", "ship the daily 5", "render the queue", "parallel ship", "stop rendering serial", or has a queue of ≥2 ready-to-render shotlists. For single-render use the pipeline/render/*.py entry directly. For pre-warm only hit POST /api/cloud/warm.
---

# /parallel-render — concurrent batch render across cloud GPU capacity

## Why this skill exists

`docs/cloudrun_image.md`:

> Multiple Shorts in parallel: up to 2 (max-instances=2 per L4)

`docs/cloudrun_tts.md`:

> Multiple channels in parallel: up to 5 (one L4 per channel)

We have the parallelism. We don't use it. Today's batch render flow:

```
for slug in queue:
    python -m pipeline.render.shorts --channel cosmosdecoded --slug $slug
```

This renders one Short at a time, holds one cloud TTS slot and one
cloud image slot, and leaves the other slot idle the entire time.
Daily 5-Short queue = ~50 min sequential. With `max-instances=2`
per service, the same queue finishes in ~25 min. With multiple
channels (5 services × 2 instances = 10 concurrent slots in theory),
the same queue across channels finishes in <15 min.

## How to run it

### 1. Resolve the batch

```
/parallel-render                                    # auto-discover ready queue
/parallel-render --channel cosmosdecoded            # only this channel's queue
/parallel-render --slugs slug1 slug2 slug3          # explicit list
/parallel-render --kind shorts                      # filter by render kind
/parallel-render --max-concurrency 4                # override the auto-computed pool size
```

**Auto-discover** = scan every channel's `narrations/` dir, find
`<slug>.json` files where:

- `<channel>/shotlist/<slug>.json` exists (script + shotlist both ready)
- `<channel>/<kind>/<slug>.mp4` does NOT exist (not yet rendered)
- `<channel>/_holds.json` does not list this slug as held (e.g. by
  `/ingest-critiques`)
- `<channel>/critiques/<slug>.fix-instructions.json` does not require
  a re-render that hasn't been attempted yet

Resolved batch printed in one block:

```
parallel-render queue (auto-discovered):
  cosmosdecoded/eddington-1919-short        kind=shorts
  cosmosdecoded/cmb-discovery-short         kind=shorts
  hindutavaanimated/eklavya-archery         kind=shorts
  mystoriesanimated/aita-frozen-pizza       kind=shorts
  scrollpulse/aita-thread-2026-05-09        kind=shorts
total: 5 shorts across 4 channels
```

### 2. Compute the concurrency pool

Per-service capacity comes from `gcloud run services describe
ytfactory-<svc> --format='value(spec.template.metadata.annotations.autoscaling\.knative\.dev/maxScale)'`
(default 2 for image, 5 for TTS). The bottleneck is image:

```
pool_size = min(
    sum(image_max_scale across all required image services),
    sum(tts_max_scale across all required tts services),
    cli_arg_or_default,   # default cap = 4 even if cloud allows more
)
```

Cap at 4 by default to keep `gcloud auth print-identity-token`'s
implicit rate limit from being a bottleneck. `--max-concurrency`
overrides.

### 3. Pre-warm everything

For every distinct `(image_provider, tts_provider)` pair across the
batch, call `POST /api/cloud/warm` (or the Warm button on the `/app/cloud` admin tab) synchronously:

```bash
cloud/warm_tts_services.sh chatterbox indicf5
cloud/warm_image_services.sh flux  # or hit `POST /api/cloud/warm?channel=<slug>` per channel
```

Block until warm. Shorter than the cold-load tax we'd pay inline
on N renders.

### 4. Dispatch

```python
import concurrent.futures, subprocess, json, time

def render_one(item):
    t0 = time.time()
    cp = subprocess.run(
        [
            ".venv/bin/python", "-m",
            f"pipeline.render.{item['kind']}",
            "--channel", item["channel"],
            "--slug", item["slug"],
        ],
        capture_output=True, text=True, timeout=1800,
    )
    return {
        "channel": item["channel"], "slug": item["slug"],
        "ok": cp.returncode == 0,
        "wall_s": time.time() - t0,
        "stdout_tail": cp.stdout[-2000:],
        "stderr_tail": cp.stderr[-2000:],
    }

with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as ex:
    futures = [ex.submit(render_one, item) for item in batch]
    results = []
    for f in concurrent.futures.as_completed(futures):
        r = f.result()
        results.append(r)
        print(f"[{'✓' if r['ok'] else '✗'}] {r['channel']}/{r['slug']}  wall={r['wall_s']:.1f}s")
```

### 5. Collate report

```
parallel-render 2026-05-10  pool=4  pre-warm=18.2s

[✓] cosmosdecoded/eddington-1919-short        wall=412s
[✓] cosmosdecoded/cmb-discovery-short         wall=389s
[✗] hindutavaanimated/eklavya-archery         wall=205s — cloud TTS fallback to local (see stderr)
[✓] mystoriesanimated/aita-frozen-pizza       wall=298s
[✓] scrollpulse/aita-thread-2026-05-09        wall=341s

5 renders / 4 ✓ / 1 ✗
total wall: 723s (vs ~1645s serial — 2.27× speedup)
GPU-min consumed: 18.4 (image) + 6.1 (tts) = ~$0.41

FAILURES (1):
  hindutavaanimated/eklavya-archery
    pipeline/tts/cloudrun.py:142  CloudRunUnavailable: HTTP 504 after 300s
    fallback: local kokoro hf_alpha — wall_s=185 (within budget)
    output: hindutavaanimated/shorts/eklavya-archery.mp4 (rendered, but with local TTS)
    NOTE: indicf5 cloud service degraded mid-batch — see /app/cloud Health row for `indicf5`
```

### 6. Post-batch hooks

- Call `/app/cloud` Health section (or `GET /api/cloud/health`) — surface any service that degraded during the
  batch (it might be why renders failed).
- Call `GET /api/cloud/cost` (or open the `/app/cloud` Cost section) — confirm the batch cost is
  in the expected range (~$0.05-0.10 per Short at current pricing).
- For each successful render, register with the eval system if the
  channel is in the tracked-projects list (per
  `/create-handoff-eval`'s rule: "auto-invoke after any successful
  batch render+upload of ≥3 videos"). This skill counts as the
  "batch" — emit the per-channel handoffs after all renders complete.

### 7. Failure handling

- A single render failure does NOT abort the batch. Other renders
  continue.
- If > 50 % of the batch fails with the same error class (e.g. all
  fail with `CloudRunUnavailable image-flux2-klein`), abort the
  remainder and recommend `/app/cloud` Health section (or `GET /api/cloud/health`) + a re-run.

### 8. Self-learning

Append per-batch summary to `data/_bench/parallel_render.jsonl`:

```json
{"ts":"…", "n":5, "pool":4, "wall_s":723, "speedup":2.27, "failures":[…]}
```

Track speedup over time. If observed speedup drops below 1.5× over a
30-day rolling window, something is contention-bound (probably
`gcloud auth` rate-limiting) — flag and propose a fix.

## Cross-references

- `pipeline/render/{shorts,long_form,footage_only,sports_doc}.py` —
  the per-render entry points this skill orchestrates.
- `POST /api/cloud/warm` (or the Warm button on the `/app/cloud` admin tab), `/app/cloud` Health section (or `GET /api/cloud/health`), `/app/cloud` Cost section (or `GET /api/cloud/cost`) — pre/post-batch
  callees.
- `/create-handoff-eval` — auto-invoked per channel after the batch
  completes.
- `<channel>/_holds.json` — respected by the auto-discover step.
