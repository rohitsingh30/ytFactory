# ytFactory — agent instructions

This file is loaded into every Claude Code session that runs in this repo.
It collects durable, project-wide rules. Channel-specific rules live under
`<channel>/learnings/`, cross-channel rules under `docs/`.

---

## Persistence rule: save every learning to BOTH memory and project docs

Whenever the user gives a durable rule, correction, preference, or finding
worth remembering, persist it in **BOTH** of these places — never just one:

1. **Central memory** — `~/.claude/projects/-Users-rohit-ytFactory/memory/`
   (private to the agent across sessions), with a pointer line in
   `MEMORY.md`.
2. **Project documentation** — an on-disk file inside this repo so the
   rule is visible to the user, teammates, and any other tool/agent.
   Pick the right home:
   - **Cross-channel rule** → `docs/<topic>.md` and link from each
     affected channel's `learnings/channel.md`.
   - **Channel-specific rule** → `<channel>/learnings/<topic>.md`,
     linked from that channel's `learnings/channel.md`.
   - **Pipeline-internal mechanic** → inline comment / docstring in
     the owning module, or a section in `docs/architecture.md`.

The project file is the source of truth (editable, reviewable). The
memory entry can be terser and link out to the project file by path.
Don't skip the project write — rules buried in private memory aren't
discoverable to anyone but the agent, which defeats half the point.

Exceptions (memory-only): ephemeral session state, user-profile facts,
raw activity logs — things that don't belong in the repo.

---

## Cloud-first TTS migration (2026-05-06 — COMPLETE)

GPU-bound TTS runs on **Cloud Run + NVIDIA L4** in `asia-southeast1`.
**The laptop is no longer the default TTS engine for any channel** —
it exists as automatic fallback only. ~10× speedup for English long-form,
~$6/mo at full throttle against existing $150 GCP credits.

### Per-channel routing (2026-05-06 reality)

- **English channels** (16 YAMLs across mystoriesanimated /
  historyrecapped / sportstoriesanimated / cosmosdecoded / airecap /
  rhymetimejunction): `tts_provider: cloudrun_chatterbox` → falls
  back to local F5-TTS on cloud failure.
- **Hindi channel** (`hindutavaanimated`): `tts_provider: kokoro`
  with `hf_alpha` voice (the only on-laptop Hindi). Cloud option
  `cloudrun_indicparler` exists; better Hindi models being researched.

### Where to find what

- **Top-level Cloud Run TTS runbook:** `docs/cloudrun_tts.md`
- **Higgs Audio v2 specifics + PierrunoYT mirror post-mortem:** `docs/cloudrun_higgs.md`
- **Per-channel routing table:** `docs/tts_stack.md`
- **Container code:** `cloud/tts-{f5,higgs,chatterbox,cosyvoice,indicparler}/`
- **Laptop client + auto-fallback:** `pipeline/tts/cloudrun.py`
- **Hindi research (in progress):** `docs/research/hindi_tts_2026.md`

### Laptop fallback policy (2026-05-06)

Established by user: "for laptop we keep F5 for all and kokoro for
hindutavaanimated; for cloud — all chatterbox; for hindi —
indicparler today, better models being researched."

| Cloud provider fails → | Local fallback |
|---|---|
| `cloudrun_chatterbox` / `cloudrun_f5` / `cloudrun_higgs` / `cloudrun_cosyvoice` | local `f5_tts` (sarah.wav) |
| `cloudrun_indicparler` | local `kokoro hf_alpha` |

Implemented in `pipeline/tts/cloudrun.py::_synth_cloudrun_*`. Set
`CLOUDRUN_TTS_DISABLE_FALLBACK=1` in tests to hard-error instead.

### Rollback

`unset CLOUDRUN_TTS_*_URL` — every cloud provider raises
`CloudRunUnavailable` → fallback path activates. No code change
required.

---

## Cloud-first image generation migration (2026-05-07 — COMPLETE for FLUX.2 klein)

GPU-bound image generation runs on **Cloud Run + NVIDIA L4** in
`asia-southeast1`, same pattern as TTS. Render-stage image gen
~3-4× faster than local mflux Z-Image-Turbo on M2 Max.

### Per-channel routing (2026-05-07 reality)

- **Every channel/variant declaring image gen** (16 YAMLs) now uses
  `image_provider: cloudrun_flux2_klein` — FLUX.2 [klein] 4B
  (Apache 2.0, BFL Jan 2026) on Cloud Run NVIDIA L4 with
  `--min-instances=1` (always-warm container).
- **One exception:** `mystoriesanimated/variants/tifu.yaml` stays
  on `image_provider: mflux` (legacy Flux Schnell, low priority).
- **Falls back to local `z_image_turbo` (mflux)** on cloud failure
  via render-level circuit breaker.

### Where to find what

- **Top-level Cloud Run image runbook:** `docs/cloudrun_image.md`
- **Provider matrix:** `docs/image_stack.md`
- **Model-pick research:** `docs/research/image_gen_2026.md`
- **End-to-end pipeline latency map (every channel × every stage,
  laptop vs cloud vs external):** `docs/pipeline_latency_2026.md`
- **Container code:** `cloud/image-flux2-klein/` (FLUX.2 klein 4B,
  shipped); `cloud/image-z-image-turbo/` (Z-Image-Turbo 6B parity
  lane, cold-load reliability WIP — see P3.5);
  `cloud/image-qwen/` + `cloud/image-hidream/` (scaffolded, not
  on production path)
- **Pre-warm before a render:** `cloud/warm_image_services.sh`
- **Laptop client + auto-fallback:** `pipeline/images_cloudrun.py`
- **Dispatcher wiring:** `pipeline/images.py` (provider branches +
  `_PROVIDER_CAPABILITIES` + `warmup()`)

### Render-level circuit breaker (key divergence from TTS)

Each Short renders ~30 images on the critical path. If we per-image
fall back to local mflux on cloud failure, that's 30 ×
`CLOUDRUN_IMAGE_TIMEOUT` (900 s × 30 = 7.5 hr) of timeouts in a
single Short during a cloud outage. Instead, the FIRST
`CloudRunUnavailable` in a render trips a module-global flag → all
subsequent calls skip cloud entirely.

`reset_circuit_breaker()` is wired into all four render entry
points (`make_short`, `render` for footage_only, long_form `main`,
sports_doc `main`). Set
`CLOUDRUN_IMAGE_DISABLE_FALLBACK=1` for canary work to hard-error
instead of falling back.

### Z-Image-Turbo cloud cold-load (P3.5 follow-up)

The Z-Image cloud service exists but cold-load through GCS Fuse
keeps stalling — Cloud Run replaces the container 3× in 17 min
during the 25 GB transformer shard reads. Three candidate fixes
in `memory/feedback_zimage_cloudrun_coldload_stall.md`. Non-blocking:
FLUX.2 klein is the primary cloud image service.

### Rollback

Single-sed flip of all 16 YAMLs back to `z_image_turbo` (local
mflux):

```bash
for f in $(grep -rlE "^[[:space:]]*image_provider: cloudrun_flux2_klein[[:space:]]*$" --include='*.yaml' .); do
  sed -i '' -E 's/^([[:space:]]*)image_provider: cloudrun_flux2_klein[[:space:]]*$/\1image_provider: z_image_turbo/' "$f"
done
```

No service redeploy needed.

## Adding a new Cloud Run service (TTS / image / video / anything)

**Required reading:** `docs/cloud_service_dep_playbook.md`. It encodes
the 6-step rule born from the Higgs Audio v2 thrash on 2026-05-05 (5
failed Cloud Builds before a working one). Every step is mandatory:

```
1. Read upstream requirements.txt + pyproject.toml — FULL
2. Grep upstream source for ALL imports (vendored code too)
3. Resolve every transitive conflict against our cross-cutting pins
4. pip install --dry-run -r requirements.txt — LOCALLY
5. Verify "Would install" output has no surprises
6. THEN build once on Cloud Build
```

Skipping any step has historically cost 3-8 hours of failed build
iterations. **The playbook saves that.** New service → read it first.

---

## LLM backend dispatcher (2026-05-10 — COMPLETE)

`pipeline/llm/cli.py::call_claude_cli` is now a backend dispatcher,
not a thin subprocess wrapper. Every call site (rewrite, cast,
prompts, critic, audio_critic, imitate, wiki research, …) goes
through the same entry point and gets routed by the
`YTFACTORY_LLM_BACKEND` env to one of:

- **`cli`** — shells out to the `claude` binary (laptop default,
  free per call against your Pro/Max OAuth plan). Vision-aware
  (`add_dirs` + `allowed_tools=["Read"]`) only on this backend.
- **`azure_openai`** (cloud render-worker default) — uses
  `openai.AzureOpenAI` against the same Azure deployment that powers
  the chat assistant. Reuses existing `AZURE_OPENAI_*` secrets, no
  separate spend. Tier alias → deployment via `AZURE_OPENAI_MODEL_*`
  env (or generic `AZURE_OPENAI_MODEL`).
- **`anthropic_sdk`** — uses `anthropic.Anthropic` with
  `ANTHROPIC_API_KEY`. Pay-per-token; opens a separate billing line.

Auto-detection (when env not set): `claude` binary on PATH → `cli`;
else Azure env present → `azure_openai`; else Anthropic key →
`anthropic_sdk`; else `cli` (will surface a clear error).

`call_llm` is a public alias for `call_claude_cli` — newer code can
use the friendlier name.

### Where things live

- **Dispatcher + adapters:** `pipeline/llm/cli.py`
- **Tests:** `tests/test_llm_dispatcher.py` + `tests/test_pipeline_llm.py`
- **Cloud Run JOB env contract:** `docs/cloudrun_render_worker.md`

### How a real cloud render flows (post-dispatcher)

1. Browser → `/api/chat` → Azure (proposal extraction, unchanged).
2. Browser → `/api/chat/confirm` → Firestore `jobs/<id>` + render task.
3. Control plane → `gcloud run jobs execute ytfactory-render-worker-v2
   --update-env-vars=YTFACTORY_JOB_ID=<id>`.
4. Worker (real mode): `pipeline.llm.rewrite.rewrite()` →
   `call_claude_cli(stage="rewrite")` → `_choose_backend()` returns
   `azure_openai` → `_call_azure_openai()` → Azure responds with
   `Script` JSON → persisted to `<channel>/scripts/<slug>.json`.
5. Worker shells out to `python -m pipeline.render.shorts --script ...
   --channel ...` which runs cast / prompts / images / tts / asr /
   compose against the cloud TTS + image services.

Vision stages (anatomy_check, critic, imitate_analyze) stay laptop-
only until we wire IP-Adapter image inlining for the SDK backends.

### Toggling

```bash
# Switch the live cloud worker to Anthropic SDK instead:
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_LLM_BACKEND=anthropic_sdk \
  --update-secrets=ANTHROPIC_API_KEY=anthropic-key:latest

# Or back to stub mode while debugging:
gcloud run jobs update ytfactory-render-worker-v2 \
  --region asia-southeast1 --project ytfactory-prod-v2 \
  --update-env-vars=YTFACTORY_RENDER_MODE=stub
```

---

## YouTube stats + OAuth refresh chain (2026-05-10 — permanent fix)

Stats on the dashboard come from **YouTube Data API v3** with a single
``YOUTUBE_API_KEY`` (no per-channel OAuth). The per-account research
cache (subscriber counts, full uploads list) is refreshed by a Cloud
Run JOB on a Cloud Scheduler cron and written to GCS.

**Where to find what:** see [`docs/youtube_stats_refresh.md`](./docs/youtube_stats_refresh.md).
That doc is the single source of truth for the whole chain (token
storage → cloud refresh → GCS cache → dashboard read path → daily
health probe → IAM grants needed).

**Hard rules:**

- **Never `git add data/research/youtube/*.json` or
  `data/research/analytics/*.json`** — both dirs are gitignored as of
  2026-05-10. Pre-fix, a test-fixture leak ("Biggie / UC_b / V_000")
  rode along into prod for several deploys via committed cache files.
- **Tests that touch `pipeline.research.youtube.YOUTUBE_DIR` MUST use
  the `tests/conftest.py::isolate_research_dirs` autouse fixture.**
  ``fetch_account()`` raises ``RuntimeError`` if a test forgets and
  tries to write into the real cache dir during a pytest run. Opt out
  with ``@pytest.mark.no_research_isolation`` only when you genuinely
  need the laptop's real cache (rare).
- **Cache layout dispatch is the single env var
  ``YTFACTORY_STATE_BUCKET``.** Set → cache lives in
  ``gs://$YTFACTORY_STATE_BUCKET/data/research/youtube/<account>.json``.
  Unset → laptop FS at ``data/research/youtube/<account>.json``.
- **OAuth tokens have a typed failure mode.** When ``authenticate(...,
  interactive=False)`` finds the cached blob has lost its
  ``refresh_token``, it raises ``pipeline.upload.upload.RefreshTokenLost(account)``
  instead of degrading silently to a 1h access token. Catch this in
  cloud-side callers and route to ``/api/admin/token-health`` so the
  daily cron alert fires.
- **Token rotation writes back to Secret Manager.** When
  ``creds.refresh()`` succeeds in cloud, ``_persist_token()`` calls
  ``secretmanager.add_secret_version`` to keep the secret current.
  Requires ``roles/secretmanager.secretVersionAdder`` on every
  ``youtube-token-<account>`` secret — wire via
  ``cloud/iam/grant_token_writeback.sh``.
- **Cards on prod are blank?** Set ``YOUTUBE_API_KEY`` on the
  ytfactory-web service. That's the entire fix.

---

## Layout reference

Per-channel layout is the canonical 2026-05-05 spec. **Use
`pipeline.paths.RenderPaths` — never derive paths by string — see
[`docs/channel_layout.md`](./docs/channel_layout.md) for the full spec.**

Quick recap:

- Every YouTube channel: `/Users/rohit/ytFactory/<slug>/` with its own
  `config.yaml`, channel-wide subdirs (`scripts/`, `learnings/`,
  `branding/`, `music/`, `footage/`, …) and per-slug subdirs (`raw/`,
  `narrations/`, `cast/`, `shotlist/`, `uploads/`, `shorts/`,
  `long_form/`, `cache/`, `scratch/`, `critiques/`).
- **Niche rule:** a channel uses niches **everywhere or nowhere**.
  Per-slug subdirs nest under the niche when present
  (`mystoriesanimated/reddit_amitheasshole/narrations/<slug>.json`);
  channel-wide subdirs never do. Source of truth for variant→niche
  mapping is `pipeline/niches.py:NICHE_CHANNEL`.
- **Cross-channel state under `data/`** is reserved for genuinely
  cross-channel things only: `_bench/`, `cache/` (ML model weights),
  `research/`, `telemetry/`. Anything per-render lives under its
  channel root.
- Cross-cutting docs: `docs/`.
- Pipeline code: `pipeline/` (shared) + `<channel>/scripts/`
  (channel-specific entrypoints).
- Renderer entry points: `pipeline/render/{shorts,long_form,
  footage_only,sports_doc}.py` — channel-agnostic. Thin CLI shims at
  `scripts/<old-path>` preserve the legacy `python scripts/...`
  invocations.
