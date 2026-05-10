# Cloud pre-render hook (shared by every `/make-*` skill)

> **Source of truth.** Every `/make-*` SKILL.md links here at the
> bottom of its "How to run it" section so future updates touch one
> place. The retrofit landed 2026-05-10.

## What every authoring skill must do at script-finalization

After you finish writing the narration / shotlist JSON and **before**
handing off to a `pipeline/render/*.py` entrypoint, do these three
things in order:

### 1. Cloud-routing assertion (synchronous, fast)

Read the channel's `config.yaml` (and the relevant variant YAML if
the channel uses variants per `pipeline/niches.py:NICHE_CHANNEL`)
and assert:

- `tts_provider` starts with `cloudrun_` (e.g. `cloudrun_chatterbox`,
  `cloudrun_indicf5`)
- `image_provider` starts with `cloudrun_` (e.g.
  `cloudrun_flux2_klein`)

Exception: variants explicitly marked `tts_provider: kokoro`
(currently only `mystoriesanimated/variants/tifu.yaml` for image and
`hindutavaanimated` for `kokoro hf_alpha` Hindi fallback) — these are
the documented local-only paths per CLAUDE.md "Per-channel routing"
table. Skip the assertion for them.

If the assertion fails, **do NOT silently render with the local
provider**. Surface to the operator:

```
⚠ cloud-routing assertion failed for <channel>
   <variant.yaml>:  image_provider: z_image_turbo  (expected: cloudrun_*)
   This means the next render will use local mflux instead of cloud FLUX.2
   klein, which is ~3-4× slower AND has different visual quality.

   Either: (a) flip back to cloudrun_flux2_klein in the YAML
           (b) confirm this is intentional with --allow-local

Aborting hand-off.
```

This catches silent regressions like the one observed in
`mystoriesanimated/variants/tifu.yaml`.

### 2. Pre-warm the cloud (now automatic — was step 2)

**As of 2026-05-10 the renderer pre-warms automatically.** Every
`pipeline/render/{shorts,long_form,footage_only,sports_doc}.py`
entrypoint calls `pipeline.cloud.warm.warm_async(channel)` immediately
after argparse — fire-and-forget on a daemon thread, no-op when no
`CLOUDRUN_*_URL` is configured. Skills used to invoke `/warm-cloud`
explicitly; that step (and the entire `warm-cloud` skill) was removed
because it duplicated infra the renderer can do itself.

If you need to warm OUTSIDE a render — e.g. a cron job, the cloud web
server when a user clicks Generate, or a manual admin action — call
the same module:

```python
from pipeline.cloud.warm import warm_for_channel  # synchronous
warm_for_channel("mystoriesanimated")             # returns a WarmReport
```

Or hit the admin panel button:

```
POST /api/cloud/warm?channel=mystoriesanimated
```

Or call the bash entry directly (what the panel button shells to):

```bash
./cloud/warm_tts_services.sh chatterbox
./cloud/warm_image_services.sh flux
```

### 3. Pre-flight cloud health (now `/app/cloud` tab — was step 3)

Just before calling the render entrypoint, glance at the
**`/app/cloud` admin tab** in web-next (Sidebar → Cloud) — the Health
section shows green/yellow/red for every service the render might use.
For automated checks (CI, cron) hit the same data via:

```bash
curl -s ${YTFACTORY_API_BASE:-http://127.0.0.1:8765}/api/cloud/health \
  | jq '.summary, .rows[] | select(.status != "green" and .status != "unconfigured")'
```

If any required service is RED, abort. Operator can either redeploy
the service (`gcloud builds submit --config=cloud/<svc>/cloudbuild.yaml`,
or follow `docs/cloud_service_dep_playbook.md` for a fresh service)
or explicitly fall back to local (`--allow-local-fallback`).

The `/cloud-health` skill that used to wrap this was deleted on
2026-05-10 — same data now lives in the admin panel + API.

## Why all three (not just one)

| Step | Catches |
|---|---|
| (1) routing assertion | silent YAML regressions to local providers |
| (2) pre-warm (now in renderer) | the 5-7 min cold-load tax that produces wrong-WPM mp4s |
| (3) health gate (`/app/cloud` tab) | mid-incident cloud outages (better to fail loud than silent-fallback to local) |

Skip any one and you reintroduce the failure mode the doolittle-raid
post-mortem documented in `cloud/warm_tts_services.sh` — a Short that
shipped 14 s short of the band because cold-load triggered local
fallback at 300 wpm instead of 165 wpm.

## How `/make-*` skills reference this

At the bottom of each `/make-*` SKILL.md, after the per-skill how-to
ends, add this single block:

```md
---

## Cloud pre-render hook (mandatory)

Before handing off to `pipeline/render/<entrypoint>.py`, run the
remaining manual step in [`docs/cloud_prerender_hook.md`](/Users/rohit/ytFactory/docs/cloud_prerender_hook.md):
the (1) cloud-routing assertion. Step (2) pre-warm is now automatic
inside the renderer (`pipeline.cloud.warm.warm_async`). Step (3)
health gate lives in the `/app/cloud` admin tab + `/api/cloud/health`.
The `warm-cloud`, `cloud-health`, `cloud-cost`, and
`deploy-cloud-service` skills were removed on 2026-05-10 — that
functionality consolidated into `pipeline/cloud/` + the admin tab.
```

The retrofit script that did the initial sweep was a single one-time
operation; future `/make-*` skills inherit the hook by including this
block in their SKILL.md template (enforced by `/make-skill`'s
heuristics — see `make-skill/learnings/heuristics.md`).
