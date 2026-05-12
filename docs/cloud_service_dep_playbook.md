# Cloud Run Service — dep resolution playbook

> **Prep status visible in `/app/cloud` Deploys section.** Each step
> below corresponds to a `cloud/<svc>/.deploy_prep/stepN.ok` marker
> file the panel reads. See `docs/cloudrun_admin_panel.md`.

> **Status:** required reading before adding any new Cloud Run TTS /
> image / video service to ytFactory. Born from the Higgs Audio v2
> deployment thrash on 2026-05-05 (5 failed builds before a working
> one) — all of which a single up-front grep would have prevented.

## TL;DR — the 6-step rule

When adding a new model service, do **all six** before pushing
ANYTHING to Cloud Build. Skipping any step is what causes the build
loops.

```
1. Read upstream requirements.txt + pyproject.toml in FULL
2. Grep upstream source tree for ALL imports (vendored code too)
3. Resolve every transitive conflict against our pin set
4. pip install --dry-run -r requirements.txt LOCALLY
5. Verify the dry-run "Would install" line has no surprises
6. THEN build once on Cloud Build
```

Estimated time investment per new service: **~20-40 min of analysis**
that saves **~3-8 hours of failed Cloud Build iterations**. The
Higgs Audio v2 thrash burned ~6 hours of build time (5 cycles ×
~25-50 min each) that all 6 steps would have prevented.

---

## Why this rule exists — the failure pattern

Hostile ML dep trees fail in a cascade:

1. **First build:** model's primary requirements.txt has unsatisfiable
   pins (e.g. `cached-path<0.24` vs `gradio>=0.33.5`)
2. **Second build:** patch one pin, expose a `--no-build-isolation`
   need (e.g. `openai-whisper` setup.py needs `pkg_resources`)
3. **Third build:** patch that, expose a CUDA build dep (`deepspeed`
   needs `nvcc` → switch to `devel` image)
4. **Fourth build:** patch that, expose a torch version override
   (model pins `torch==2.3.1`, breaking our `torch==2.4.1+cu124`)
5. **Fifth build:** patch that, expose a missing torch ABI dep
   (`libcudnn.so.9` not found)
6. **Sixth build:** patch that, expose a transitive protobuf conflict
   between Higgs's `descript-audio-codec` and our `google-cloud-storage`
7. **Seventh build:** drop the conflicting dep, expose a missing
   runtime import — because `--no-deps` skipped it
8. **Eighth build:** add the missing runtime imports list, expose a
   VENDORED-CODE import (`from audiotools import AudioSignal` deep
   inside the model's own source tree)
9. ...

Each step is **~25-50 minutes of Cloud Build wall-clock**. The whole
chain is ~6+ hours that one careful upfront analysis would skip.

---

## Step 1 — Read upstream `requirements.txt` + `pyproject.toml`

```bash
# Cache the full text in /tmp so you can grep it
curl -s https://raw.githubusercontent.com/<org>/<repo>/main/requirements.txt > /tmp/upstream-req.txt
curl -s https://raw.githubusercontent.com/<org>/<repo>/main/pyproject.toml > /tmp/upstream-pyproject.toml
cat /tmp/upstream-req.txt
```

What to extract:

| Class | Action |
|---|---|
| Hard pins (`X==1.2.3`) | Keep as-is unless they conflict with our stack |
| Loose specs (`X`, `X>=1`) | Pin to current-stable to stop pip backtracking |
| Training-only deps (deepspeed, tensorrt, lightning, wandb, gradio, fastapi) | Drop — we don't train, we infer |
| **Old protobuf** (`protobuf<3.20` from descript-audiotools) | Either drop the offending dep or install with `--no-deps` |
| Conflicting torch (`torch==2.3.x`) | Pre-install our cu124 torch first; install model with `--no-deps` |
| Build-time-only (`ruff`, `pytest`, `mypy`) | Drop |

## Step 2 — Grep ALL imports in upstream source

This is the step we missed for Higgs Audio v2 → cost us 2 extra build
cycles. **Vendored sub-libraries import packages NOT listed in the
upstream `requirements.txt`.** Always grep for them:

```bash
# Clone the repo locally just for inspection (delete after)
git clone --depth 1 https://github.com/<org>/<repo>.git /tmp/upstream
cd /tmp/upstream

# Find every import the source tree does
grep -rhE "^(from|import) " --include="*.py" | \
    awk '{print $2}' | \
    cut -d. -f1 | \
    sort -u > /tmp/upstream-imports.txt

cat /tmp/upstream-imports.txt
```

For every import that ISN'T:
- Python stdlib
- One of OUR deps (fastapi, uvicorn, pydantic, soundfile, google-cloud-storage)
- One of upstream's `requirements.txt` deps

→ **Add it to OUR requirements.txt** with an explicit pin. Don't
assume `pip install -e .` will pull it.

**Real example (Higgs Audio v2, 2026-05-05):**
- `requirements.txt` listed `descript-audio-codec`
- We dropped it (protobuf conflict with google-cloud-storage)
- **But the vendored `boson_multimodal/audio_processing/descriptaudiocodec/dac/model/dac.py` did `from audiotools import AudioSignal`** — and `audiotools` is the module name installed by `descript-audiotools` (a SEPARATE pkg, also pinned to old protobuf)
- Grep would have shown this import upfront. Instead we shipped the bug to Cloud Build, waited 25 min for the failure, then patched.

The fix: install the conflicting dep with `--no-deps` so its module
is importable but its bad transitive pins don't propagate.

## Step 3 — Resolve transitive conflicts against our pin set

Our cross-cutting pins (every Cloud Run TTS service inherits these):

| Pin | Why |
|---|---|
| `torch==2.4.1+cu124` | Cloud Run L4 + CUDA 12.4 base image |
| `torchaudio==2.4.1+cu124` | Must match torch version exactly (ABI) |
| `transformers==4.46.3` | Compatible with most 2025+ models; doesn't fight tokenizers |
| `accelerate==0.34.2` | Pairs with transformers 4.46 |
| `huggingface-hub==0.26.5` | Modern; satisfies gated-download API |
| `safetensors==0.4.5` | Stable; doesn't conflict with diffusers/transformers |
| `pydantic==2.9.2` | Required by fastapi==0.115.6; do NOT downgrade |
| `fastapi==0.115.6` | The HTTP layer; must satisfy starlette + pydantic |
| `google-cloud-storage==2.18.2` | GCS handoff for >5MB WAV |
| `google-api-core==2.20.0` | Pin to stop resolver backtracking through 30 versions |
| `googleapis-common-protos==1.65.0` | Same — resolver-stop pin |
| `proto-plus==1.24.0` | Same — pulls modern protobuf (>=3.20) |
| `protobuf>=4.21.6,<6` (implicit via google) | New protobuf; will conflict with `descript-audiotools<3.20` if both present |

If a model's deps demand something incompatible:
- **Option A** — install the model with `--no-deps`, then list ALL
  its runtime imports in our requirements.txt explicitly
- **Option B** — install the conflicting transitive with `--no-deps`
  to get the module without its bad pins
- **Option C** — drop the model. Don't keep iterating.

## Step 4 — Local pip dry-run (the gating step)

This is the one that catches conflicts in **30 seconds** instead of
**30 minutes** of Cloud Build wall.

```bash
# Use whichever Python version you have locally — even if it doesn't
# match the container's Python, the dependency RESOLUTION is the same
# (only ABI-pinned wheels differ).
python3 -m venv /tmp/validate
/tmp/validate/bin/pip install --upgrade pip --quiet
/tmp/validate/bin/pip install --dry-run -r cloud/<service>/requirements.txt 2>&1 | tail -20
```

What to look for:

| Output | Meaning |
|---|---|
| `Would install <list>` | ✅ Resolved cleanly. Build is safe to push. |
| `ResolutionImpossible` | ❌ Pin conflict. Read the "conflict is caused by" block carefully. |
| `resolution-too-deep` | ❌ Resolver is searching too many versions. **Pin every transitive listed in the backtracking trace.** |
| Hangs > 3 min | ❌ Same as above — pin more transitives. |

If the dry-run fails, **fix the pins, re-run, repeat — locally**.
Don't push to Cloud Build until dry-run succeeds.

## Step 5 — Sanity-check the dry-run output

Read the `Would install` line and verify:
- ✅ Torch is your pinned cu124 version (NOT default `torch-2.x.y` without `+cu124` suffix)
- ✅ All your direct deps are present at the version you pinned
- ✅ No "unexpected" packages from a model's transitive (e.g. `bitsandbytes` quietly pulled by `accelerate`)
- ✅ No `[no matching distribution]` warnings

## Step 6 — Build once on Cloud Build

```bash
cd cloud/<service>
./deploy.sh <service-name> v1
```

If the build fails despite all the above, it's almost always one of:

| Failure | Fix |
|---|---|
| `pkg_resources not found in build env` | Add `--no-build-isolation` to the offending pip install |
| `nvcc not found` | Switch base image from `runtime` to `devel` (adds ~2GB but provides nvcc) |
| `disk full` | Don't pre-pull weights into image. Let them download on first `/readyz` call. |
| `404 from HuggingFace` | Model is gated. Set HF_TOKEN substitution per `.env`. |
| `OOM during model load on Cloud Run` | Bump `--memory` (4 CPU max 16Gi; 8 CPU max 32Gi) |
| Service starts but `/readyz` returns 500 | Check `gcloud run services logs read <svc>` — usually a missed runtime import (back to Step 2) |

> **Cross-cutting deploy gotcha (2026-05-10):** `gcloud run services
> update --set-secrets` REPLACES the entire mount list. When you add
> a runtime secret out-of-band via `--update-secrets`, you MUST also
> amend the matching `cloud/<service>/deploy.sh` `--set-secrets` line
> in the same session, or the next `bash deploy.sh` regresses prod.
> Audit recipe: [`docs/cloud_run_set_secrets_destructive.md`](./cloud_run_set_secrets_destructive.md).

---

## Concrete checklist for ANY new Cloud Run service

Copy-paste this checklist into a new service's PR description / commit
message. Each box must be ticked before any `gcloud builds submit`.

```
Before first build:
[ ] Read upstream requirements.txt — full
[ ] Read upstream pyproject.toml — full
[ ] Grep upstream source for ALL imports — `grep -rhE "^(from|import) "`
[ ] Mapped every non-stdlib import to a pip package
[ ] Pinned cross-cutting deps from docs/cloud_service_dep_playbook.md
[ ] Resolved every transitive conflict listed there
[ ] Dropped training-only / build-only deps (deepspeed, tensorrt, lightning, etc)
[ ] pip install --dry-run -r requirements.txt SUCCEEDED locally
[ ] Reviewed "Would install" line — no surprises
[ ] If gated model: HF_TOKEN added to .env, build-arg wired in cloudbuild.yaml

deploy.sh wiring (mandatory — every cloud/<svc>/deploy.sh):
[ ] Starts with `set -euo pipefail`
[ ] Sources `cloud/_shared/auth_setup.sh` immediately after, so the
    ADC-token bypass auto-applies and a fresh `gcloud auth application-
    default login` covers the deploy without asking the user to also
    run `gcloud auth login` (workspace reauth policy).
    See docs/deploy.md "Adding a new cloud/<svc>/deploy.sh" for the
    exact snippet, and feedback_gcloud_reauth_use_adc_bypass.md for
    why this is mandatory (escalated to a hard rule on 2026-05-12
    after firing twice in one day).

After first build:
[ ] /readyz returns {"status":"ready", "warm_s":<n>}
[ ] /synth returns valid WAV (verified with ffprobe + Whisper transcribe)
[ ] For multilingual claims: tested with target language (don't trust marketing)
```

---

## Reference: the `descript-audiotools` lesson

When Higgs Audio v2 broke v3 because `from audiotools import AudioSignal`
wasn't in upstream `requirements.txt`, the fix was:

```dockerfile
RUN python -m pip install --no-deps \
        descript-audiotools==0.7.2 \
        argbind randomname markdown2 ipython julius flatten-dict
```

`descript-audiotools` ships the `audiotools` module name, with old
protobuf in its requirements — `--no-deps` strips the protobuf pin
without losing the module. The other packages (`argbind`, etc) are
descript-audiotools's own runtime imports we'd otherwise hit next.

This pattern (`--no-deps` + manual transitive list) works for ANY
hostile dep tree where the model needs the module but not the bad
pins. Document new instances of it in this file under the next
heading so the next agent doesn't rediscover them.

---

## Hostile-dep-tree registry (per model)

Append a section here for each model whose dep tree fights our stack.

### IndicF5 (AI4Bharat) — 2026-05-06

- **`matplotlib` is an inference-time dep, not training-only.** Their
  `f5_tts/infer/utils_infer.py:14` does `import matplotlib` at module
  level (looks like for debug/dev plotting helpers, but the import is
  unconditional). If you skip matplotlib because you assume it's
  training-only, IMPORT_SMOKE_OK fails. **Always include it.**
- **Conflict:** their requirements pin `transformers<4.50` and
  `numpy<=1.26.4`. We pin `transformers==4.49.0` and `numpy==1.26.4`
  exactly. If we ever bump transformers to 4.50+, IndicF5 stops
  loading.
- **torch override trap:** `accelerate>=0.33.0` (and a few others)
  list `torch>=2.0.0` as a hard dep. `pip install -r requirements.txt`
  silently upgrades our cu124 torch to whatever the latest CPU build
  is. **Fix:** install `torch==2.4.1+cu124` BEFORE requirements.txt,
  THEN re-install with `--force-reinstall --no-deps` AFTER (because
  the requirements.txt install will overwrite it again).
- **Fork name collision:** IndicF5 ships its OWN `f5_tts` package
  (different code from PyPI's `f5-tts==1.1.20`). The two cannot
  coexist in one image. **Fix:** isolate IndicF5 in its own service
  (`cloud/tts-indicf5/`), separate from `cloud/tts-f5/`.
- **Gated:** `gated=auto` — needs HF_TOKEN for the snapshot download.
  ToS auto-approves on form submission.
- **Skip safely (in modules our server.py never imports):** `gradio`,
  `wandb`, `pandas`. These are in `infer_gradio*.py`, `model/trainer.py`,
  `infer_cli_batch.py` respectively — none of which our server.py
  reaches.

### LESSON LEARNED 2026-05-06: don't trust requirements.txt for inference deps

When IndicF5 first failed on missing matplotlib, the failure pattern
was the same as the Higgs Audio v2 trap from 2026-05-05: trusting
the upstream `requirements.txt` instead of doing **step 2 of this
playbook (grep upstream source)**.

The fix is mechanical:

```bash
git clone --depth 1 https://github.com/<org>/<repo>.git /tmp/grep-it
cd /tmp/grep-it

# All non-stdlib top-level imports in the inference path
grep -rhE "^(from|import) " --include="*.py" <inference_dir>/ | \
    awk '{print $2}' | cut -d. -f1 | sort -u

# For each non-stdlib import that isn't already in your requirements.txt,
# check whether it's used in code your server.py actually reaches
# (otherwise it's safe to skip):
grep -rn "import <module>" <inference_dir>/
```

Don't skip this. The matplotlib gap on IndicF5 cost one 12-min build
cycle. Higgs's audiotools gap cost 6 hours of build cycles. The grep
takes 30 seconds.

---

## Hostile-dep-tree registry (per model)

Append a section here for each model whose dep tree fights our stack.

### CosyVoice 2 (FunAudioLLM)

- **Conflict:** pins `torch==2.3.1+cu121`, `tensorrt-cu12`, `deepspeed`, `pydantic==2.7.0`, `gradio`, `fastapi==0.115.6`, `lightning`
- **Fix:** install only the inference-time subset, NOT `pip install -r requirements.txt`. Skip tensorrt (10GB), deepspeed (needs nvcc), lightning (training), gradio (UI), fastapi (we provide our own).
- **Server gotcha:** `inference_zero_shot` expects a **file path**, not a torch tensor — its frontend internally calls `torchaudio.load(path)`. Always pass `str(path)` not the tensor.

### Higgs Audio v2 (Boson AI)

- **Conflict 1:** `boto3==1.35.36` + `s3fs` triggers pip's resolver to backtrack through 100+ aiobotocore/botocore versions → `resolution-too-deep`. **Fix:** drop `s3fs` (only used in training scripts), pin `botocore==1.35.36` exact.
- **Conflict 2:** `descript-audio-codec` pulls `descript-audiotools<0.7.x` which pins `protobuf<3.20`, conflicts with `google-cloud-storage>=2`. **Fix:** drop `descript-audio-codec` from pip — Higgs uses a vendored copy of DAC at `boson_multimodal/audio_processing/descriptaudiocodec/`, not the pip package.
- **Conflict 3 (the v3-v5 trap):** vendored DAC code does `from audiotools import AudioSignal`. **Fix:** install `descript-audiotools==0.7.2 --no-deps` to get the module without the protobuf pin. Also need the FULL audiotools transitive set (greppable from `audiotools/setup.py` install_requires + `grep -rhE "^(from|import) " audiotools/`): `argbind randomname markdown2 ipython julius flatten-dict ffmpy rich matplotlib pyloudnorm pystoi torch-stoi importlib_resources`. Skip `tensorboard` / `protobuf<3.20` / `gradio` (training/UI, not needed at inference).
- **Apparent bugs that are NOT bugs:**
  - `boson_multimodal/audio_processing/quantization/core_vq.py` imports the missing `xcodec` package — but `vq.py` uses `core_vq_lsx_version` (line 17 commented out the `core_vq` import). Dead code; ignore.
  - `boson_multimodal/model/higgs_audio/utils.py` imports `deepspeed` — but only conditionally via `transformers.is_deepspeed_available()`. Skip the install.
- **Memory:** `--memory=24Gi` requires `--cpu=8` (Cloud Run rule).

---

## The build-time import smoke test (the rule that ends the iteration loop)

Every Cloud Run service Dockerfile MUST end with a `RUN python -c
"…"` block that walks the **same import chain `serve_engine.py`
executes on `/synth`** — without loading model weights to GPU.

This catches missing-module bugs at BUILD time (15s feedback) instead
of at `/readyz` time (~30 min build + ~13 min image pull + crash).

Template (adapt the imports per model):

```dockerfile
RUN python -c "
import sys
print('--- import chain smoke test ---')
print('1. <package> top-level…')
import <model_package>
print('2. <subpackage>…')
from <model_package>.<sub> import <fn>
# ... walk every level of the chain that /synth triggers
print('OK — all critical imports resolve.')
"
```

If this `RUN` fails:
- Read the missing-module name from the traceback
- Add it to your `requirements.txt` (with `--no-deps` if its
  transitives conflict with our pin set)
- Rebuild — but do step 2 of the playbook (grep upstream for ALL
  imports) FIRST so you don't add one then need another tomorrow.

This pattern is now in `cloud/tts-higgs/Dockerfile` (the Higgs v6
build is the canonical example). When adding a new service, copy
the block and adapt the import chain to that model.

### Indic Parler-TTS (AI4Bharat)

- **Conflict:** none. Cleanest dep tree of the bunch.
- **Gating:** auto-gated (ToS only); needs `HF_TOKEN` for weight pull at build time. Auto-approved on form submission.
- **API:** description-driven (NOT WAV-clone). Caller supplies `description` field describing the voice character. Auto-detects language from prompt text.

### CosyVoice 3 (FunAudioLLM, Dec 2025)

- Not yet integrated. Same dep-tree warnings as CosyVoice 2 expected.
- **Hindi support:** still NOT in the language list (Chinese, English, Japanese, Korean, German, Spanish, French, Italian, Russian + 18 Chinese dialects).

### CosyVoice 2 — Hindi gotcha

- **CosyVoice 2 0.5B does NOT support Hindi.** Trained on CN/EN/JP/KR + some EU languages. Feeding Hindi text → produces gibberish that Whisper detects as Korean. Skip CosyVoice for Hindi entirely. Use Indic Parler or Higgs for Hindi.

---

## Auto-patch scripts must detect each Dockerfile's build context (2026-05-11)

`cloud/_shared/add_otel_copy.sh` blindly emitted
`COPY otel_init.py ./` into every `cloud/<svc>/Dockerfile`. That
worked for 12 of 14 services but silently failed for the 2
services whose Dockerfile expects build context = repo root
(`editing-agent`, `render-worker-v2`). The error:

```
COPY failed: file not found in build context: stat otel_init.py:
file does not exist
```

### Two valid build-context conventions in this repo

| Style | Build invocation | Top-of-Dockerfile clue | OTel COPY line |
|---|---|---|---|
| **per-service-dir** (12 services) | `cd cloud/<svc>/ && gcloud builds submit .` | `COPY server.py ./` / `COPY requirements.txt .` | `COPY otel_init.py ./` |
| **repo-root** (editing-agent, render-worker-v2) | `gcloud builds submit . --config=cloud/<svc>/cloudbuild.yaml` (from repo root) | `COPY pipeline/...`, `COPY cloud/<svc>/server.py ...` | `COPY cloud/<svc>/otel_init.py /workspace/otel_init.py` |

Repo-root context is needed when the Dockerfile must COPY shared
modules like `pipeline/editing/` or `pipeline/cloud/` — these live
outside the per-service directory, so per-service-dir context can't
reach them.

### Audit recipe

```bash
for df in cloud/*/Dockerfile; do
  echo "=== $df ==="
  if grep -qE '^COPY (pipeline|scripts|control|cloud)/' "$df"; then
    echo "  context: repo-root"
  else
    echo "  context: per-service-dir"
  fi
  grep -nE '^COPY .*otel_init' "$df" || echo "  no OTel COPY"
done
```

### Rule for any new auto-patch script

Auto-patch scripts that touch Dockerfiles MUST detect each
service's build context before emitting COPY lines. The detector
heuristic: scan for any `^COPY (pipeline|scripts|control|cloud)/`
line — if any are present, build context is repo-root. The patcher
in `cloud/_shared/add_otel_copy.sh` is now context-aware; copy its
detector pattern when adding new auto-patch scripts.

### Why this is a class-of-bug (not a one-off)

Every shared-template auto-patch (OTel today; tomorrow whatever
cross-cutting concern needs to land in N services) is subject to
the same trap. Build-context divergence between services is a real
constraint we can't unify (repo-root context is needed for shared
modules), so the fix is per-script awareness, not template
unification.

### See also

- Memory: `feedback_otel_init_copy_path_per_context.md`
- Implementation: `cloud/_shared/add_otel_copy.sh` (context-aware)
- Commit: `ffafae4` (the editing-agent Dockerfile fix); follow-up
  patches the script + this playbook.

---

## Auto-patch scope: only Python OTel-using services (2026-05-12)

`cloud/_shared/add_otel_copy.sh` previously patched **every**
`cloud/<svc>/Dockerfile` regardless of language, including
`web-next/Dockerfile` (Node.js, runs `next start`). Three things
made the bug invisible until the next web-next deploy:

1. The patcher emitted `COPY otel_init.py ./` into web-next's
   Dockerfile.
2. `cloud/_shared/sync.sh` (correctly) only copies `otel_init.py`
   into dirs containing `server.py` or `entrypoint.py` — i.e.
   Python services. So `cloud/web-next/otel_init.py` was never
   created.
3. web-next's Dockerfile builds with **repo-root** context (via
   `cloudbuild.yaml`), so even if `otel_init.py` had been synced
   into the service dir, the path `./otel_init.py` would have
   resolved to the repo root, not the service dir — also missing.

Combined effect: the next `cloud/web-next/deploy.sh` failed at
`Step 14/14 : COPY otel_init.py ./` with:

```
COPY failed: file not found in build context or excluded by
.dockerignore: stat otel_init.py: file does not exist
```

This is a class-of-bug whenever any "shared template" patcher and
its companion sync helper disagree on what counts as an
"OTel-using service".

### Single source of truth: the OTel-eligible service set

A service is OTel-eligible **iff** its `cloud/<svc>/` directory
contains `server.py` OR `entrypoint.py`. Equivalently: OTel
applies to Python services only — the SDK we vendor in
`cloud/_shared/otel_init.py` is `opentelemetry-sdk` for Python and
would never be imported by Node.js, static-asset, or one-shot
init containers.

All three shared scripts now use this same filter:

| Script | What it does | Filter |
|---|---|---|
| `cloud/_shared/sync.sh` | Copies `otel_init.py` AND `cloud_run_json_exporter.py` into every service dir | `server.py` OR `entrypoint.py` OR existing `otel_init.py` exists (the `otel_init.py` clause was added 2026-05-12 to catch slim-wrapper images like `cloud/web-server/` whose actual app code lives in the repo's `web/server.py` and is COPY'd in via repo-root context) |
| `cloud/_shared/add_otel_copy.sh` | Patches `Dockerfile` with the right `COPY <helper>` line for EVERY helper in the `OTEL_HELPERS` array (data-driven; adding a fourth helper later is a one-line config change) | **same** as `sync.sh` (added 2026-05-12) |
| `cloud/_shared/append_otel_deps.sh` | Appends `otel_requirements.txt` block to `requirements.txt` | naturally Python-only (no `requirements.txt` ⇒ no-op) |
| `cloud/_shared/redeploy_for_otel.sh` | Parallel re-deploy of every OTel service after `_shared/` changes | hardcoded allowlist of 14 Python services (now includes `web-server` so the dashboard backend itself ships the new exporter / reader together) |

If you add a fourth shared-template script, mirror this filter
exactly. If you add a non-Python service that genuinely DOES need
distributed tracing (e.g. wire it via the JS OTel SDK in a SSR
layer), do NOT relax this filter — instead, add an explicit
opt-in marker file like `cloud/<svc>/.otel-eligible` and switch
the filter to "Python file present OR opt-in marker exists".

If you add a fifth helper file under `cloud/_shared/` that needs
to ship into every service image, append it to BOTH
`cloud/_shared/sync.sh::HELPERS` AND
`cloud/_shared/add_otel_copy.sh::OTEL_HELPERS` arrays — the two
arrays must stay in lockstep or `bash cloud/_shared/sync.sh
--check` will pass while Cloud Build fails with `COPY <helper>:
file not found`.

### Verifying scope after editing any shared script

```bash
# All three scope-using scripts agree (no diff in service lists):
diff <(for d in cloud/*/; do
         [[ -f "${d}server.py" || -f "${d}entrypoint.py" \
            || -f "${d}otel_init.py" ]] \
           && [[ "$(basename "${d%/}")" != "_shared" ]] \
           && basename "${d%/}"
       done | sort) \
     <(grep -oE '"[a-z0-9-]+\|' cloud/_shared/redeploy_for_otel.sh \
         | tr -d '"|' | sort)
# Empty diff = aligned. Any diff = a service is in one list but not
# the other. Reconcile before deploying.
```

### Why a non-Python service can never quietly piggyback on the patcher

Adding `web-next` (or any future Node/static service) to the
patcher's set without ALSO adding it to `sync.sh` reproduces the
exact failure mode above. The `iff server.py OR entrypoint.py`
filter is the smallest invariant that prevents it. Don't loosen
it just to add a one-off — use the explicit opt-in marker
approach.

### See also

- Implementation: `cloud/_shared/add_otel_copy.sh` (scope filter
  added in this commit)
- Implementation: `cloud/_shared/sync.sh` (canonical scope filter)
- Service that triggered this rule: `cloud/web-next/` (Node.js;
  Dockerfile carries an inline note pointing back here)
- Sibling rule: §"Auto-patch scripts must detect each Dockerfile's
  build context" (above) — this rule covers WHEN to patch; that
  rule covers HOW to patch.
