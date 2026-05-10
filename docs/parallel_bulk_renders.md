# Parallel bulk renders

**Rule (2026-05-07):** When a `make-*` skill produces N renders in a
single invocation (typical: `/make-mystories-short --limit N`), kick
all N in parallel via `Bash` with `run_in_background=true`. Don't
serialize.

## Why

Every production render path in 2026-05-07 reality is cloud-based:

- TTS services (`cloudrun_chatterbox` / `cloudrun_indicf5` /
  `cloudrun_higgs` / `cloudrun_cosyvoice` / `cloudrun_indicparler`)
  — `--max-instances=2`, `concurrency=1`. Two simultaneous synth
  requests fan out to two warm instances.
- Image service (`cloudrun_flux2_klein`) — `--min-instances=1`
  always-warm, `--max-instances=2`. First parallel image request
  reuses the warm instance; second triggers an autoscale-up.

For 2 parallel TIFU renders, the cloud briefly queues at the
bottleneck stage but total wall-time becomes `max(per_render_time)`
rather than `sum(per_render_time)`. A 14-min serial run collapses
to ~7-8 minutes parallel.

## DON'T over-parallelise — cap at cloud `--max-instances`

Each render fires ~30 image-gen calls. With 3+ parallel renders
× 30 images = 90+ in-flight calls fanning out to a 2-instance
service, queue depth exceeds the 15-min read-timeout and calls
start failing. The render-level circuit breaker
(`pipeline/images.py`) then trips to **local mflux fallback** —
which is the disaster mode: 3 concurrent mflux processes thrash
unified memory and crash with `kIOGPUCommandBufferCallbackErrorTimeout`
(observed 2026-05-07 during TIFU 4-way parallel test).

**Rule:** parallel render count ≤ `cloudrun_flux2_klein
--max-instances`. Today that's 2. To run 4-way parallel, redeploy
the image service first:

```bash
# bump max-instances in cloud/image-flux2-klein/deploy.sh,
# then redeploy
./cloud/image-flux2-klein/deploy.sh ytfactory-image-flux2-klein
```

For >2 renders without redeploying, batch in groups of 2:

```bash
# 5 renders, 2-wide parallel
kick(slug1, slug2)  → wait both → kick(slug3, slug4) → wait
                    → kick(slug5)
```

## How to apply

In a skill that batch-renders:

```bash
# In the skill's §Render step — kick all N at once.
for slug in $SLUGS; do
  Bash(
    command="...make_shorts.py --channel ... --script .../$slug.json",
    run_in_background=True,
  )
done
```

Each backgrounded `Bash` returns a `bash_id`. The harness notifies
you when each completes. Process the notifications as they arrive,
report progress to the user.

## When NOT to parallelise

- **Local mflux render path.** If a YAML still has
  `image_provider: mflux` (legacy laptop GPU), parallel runs thrash
  unified memory on M2 Max. As of 2026-05-07 no production YAML uses
  mflux; the migration in CLAUDE.md §"Cloud-first image generation"
  flipped them all to `cloudrun_flux2_klein`.
- **Render-level circuit breaker tripped.** If Cloud Run image is
  down and the circuit breaker in `pipeline/images.py` has fallen
  back to local mflux, treat it like the local case — serialise.
  Detect by tail-checking the first render's log for
  `CloudRunUnavailable` before kicking the rest.
- **Other GPU-bound stages.** Whisper word-timestamp alignment runs
  on the laptop GPU/CPU. If a skill triggers fresh whisper alignment
  on N inputs (cache miss), parallel might contend. In practice
  whisper is fast (≤30s/render) and not the bottleneck.

## History

This rule supersedes the pre-cloud-era guidance in
`memory/feedback_gpu_one_render_at_a_time.md`, which was correct
when image gen ran on the local M2 Max GPU via mflux. With the
2026-05-07 cloud-first migration (CLAUDE.md §"Cloud-first image
generation migration"), the laptop GPU is no longer the
critical-path bottleneck. The original rule still applies to the
local-mflux fallback path.

## Skill mirrors

The rule is enforced in:

- `.claude/skills/make-mystories-short/SKILL.md` §5 "Renderer
  handoff" + §"Important rules"
- `.claude/skills/make-history-short/SKILL.md` §7b "Animated path"

When a new `make-*` skill is authored, it must default to parallel
for bulk renders unless the render path is laptop-GPU-bound.

## 2026-05-08 update — bulk-render-100 (cosmosdecoded) repro

The original rule above was learned from 4-way TIFU; this update
pins the calibration with a 100-short batch.

**What broke:** `cosmosdecoded/scripts/bulk_render_100.py` ran with
`--workers 3` AND each render's internal `_ensure_ai_images` used
`ThreadPoolExecutor(max_workers=4)`. That fans out to **3 × 4 = 12
concurrent `/generate` calls** to a 2-instance Cloud Run service
(`--max-instances=2`). After ~10 successful renders, queue depth
exceeded the 900s timeout and `urllib3.ReadTimeout` cascaded.

**Cascade chain:**
1. Cloud Run /generate starts timing out
2. Render-level circuit breaker in `pipeline/images.py` trips →
   falls back to local mflux Z-Image-Turbo
3. With 3 simultaneous renders ALL falling back to mflux, the
   first one acquires the M2 Max GPU stream, the others crash with
   `RuntimeError: There is no Stream(gpu, 7) in current thread`
4. Bulk task exits after only 11 mp4s of 100

**Recovery knob (added 2026-05-08):**

Set `CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` so a single Cloud Run
failure hard-errors that one render instead of falling back. The
batch keeps going — losing ONE render is far cheaper than the
chained-crash death.

**Calibrated knobs for bulk renders ≥ 50 shorts:**

```bash
export CLOUDRUN_IMAGE_DISABLE_FALLBACK=1
.venv/bin/python -u <channel>/scripts/bulk_render_100.py --workers 2
```

And in `bulk_render_100.py::_ensure_ai_images`:

```python
# 3 internal × 2 outer renders = 6 concurrent /generate calls — within
# Cloud Run L4 capacity. 4 internal × 3 outer crashed Cloud Run with
# 12+ concurrent calls and 900s timeouts on 2026-05-08 bulk run.
with ThreadPoolExecutor(max_workers=3) as ex:
```

**Math:** outer × inner ≤ `cloudrun_flux2_klein --max-instances` ×
1.5 (instances tolerate ~1.5 concurrent calls each before queuing).
With max-instances=2, that's 3-6 total concurrent calls. workers=2
× internal=3 = 6 fits.

**Author rule for any new bulk script:** ALWAYS set
`CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` before kicking, and document the
total-concurrent-calls math in a comment so future tuners
understand why workers=2 not 3.

## Related

- Memory: `feedback_parallel_bulk_renders.md` (this rule, terse)
- Memory: `feedback_gpu_one_render_at_a_time.md` (local-mflux
  serial rule — still applies in fallback)
- CLAUDE.md §"Cloud-first image generation migration" (the
  migration that made parallel safe)
