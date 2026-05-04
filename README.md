# ytFactory

End-to-end product for generating YouTube Shorts from a chat conversation.

A user opens the website, describes the Short they want in chat, and the
system produces a finished 1080×1920 mp4 — script, voice, illustrations,
captions, broadcast cut-ins where applicable. Owner accounts can also
publish the result directly to a connected YouTube channel.

## Live

**Production URL:** https://ytfactory-control-767262167641.us-central1.run.app

That URL is the canonical entry point for everything. The chat UI, the
agent lease protocol, and the eventual job-status endpoints all live
behind it. Local dev still works, but **the deployed Cloud Run service is
the source of truth** — never run a parallel local server in production
mode.

| Endpoint | What |
|---|---|
| `GET  /` | Public chat UI (web/static/chat.html) |
| `POST /api/chat` | Chat with the assistant — extracts a `short_proposal` JSON when it has enough info |
| `POST /api/chat/confirm` | Turn the latest proposal into a Job + first Task in the Firestore queue |
| `GET  /docs` | FastAPI auto-generated API docs |
| `POST /agent/heartbeat` `lease` `ack/{id}` | Laptop agent lease protocol (bearer-token auth) |

Hitting the live URL directly (curl / browser / Playwright MCP) is now
the supported workflow. No local server needed.

## Architecture

Cloud control plane on Cloud Run + Firestore + GCS, fronted by a public
chat UI; light I/O work runs on Cloud Run jobs; heavy MLX rendering runs
on the laptop via a pull-based agent over outbound HTTPS.

### Documentation

| Doc | Audience | What it covers |
|---|---|---|
| [`docs/architecture.md`](./docs/architecture.md) | engineers | Components, deployment, IAM, repo layout, mermaid system diagram |
| [`docs/user_flows.md`](./docs/user_flows.md) | designers / PMs / new contributors | Three user types (visitor, owner, operator), state machine, anti-abuse perimeter — sequence diagrams |
| [`docs/data_flows.md`](./docs/data_flows.md) | backend engineers, debuggers | Where data lives, chat extraction, lease protocol, render pipeline, lifecycle GC, polling — flowcharts + sequence diagrams |
| [`docs/legacy_pipeline.md`](./docs/legacy_pipeline.md) | rendering engineers | What `scripts/make_shorts.py` does internally — the ~7-min render, stage by stage |

| Layer | Where | What |
|---|---|---|
| Control plane | **Cloud Run** (deployed) | FastAPI app: chat, queue, agent endpoints, static UI |
| Queue + state | **Firestore** (native, us-central1) | tasks, jobs, chat sessions |
| Artifacts | **Cloud Storage** (`gs://ytfactory-prod-artifacts`) | scripts, images, audio, mp4s, thumbnails |
| Secrets | **Secret Manager** | Azure OpenAI key, agent bearer token |
| Heavy workers | **Laptop agent** (outbound HTTPS) | image gen, TTS, ASR, ffmpeg compose, footage trim |

Lifecycle rules on the bucket auto-delete heavy intermediates after 1 day,
finished mp4s after 7 days, metadata after 30. After a successful YouTube
upload the worker also explicitly GCs heavy artifacts and hands off to the
research pipeline (analytics-only mode for the published video).

## Repo layout

```
control/              # Cloud Run service (deployed)
  server_dev.py         # FastAPI app entry point (also used in prod)
  chat_service.py       # Azure OpenAI chat with proposal extraction
  chat_routes.py        # /api/chat + /api/chat/confirm
  agent_routes.py       # /agent/heartbeat /lease /ack
  queue.py              # Firestore + InMemory queue backends
  storage.py            # GCS adapter
  auth.py               # bearer-token agent auth (Firebase user auth in #12)
shared/
  schema.py             # cross-side: TaskEnvelope, JobEnvelope, ShortProposal
agent/                  # laptop daemon (outbound-only HTTPS)
  main.py               # heartbeat + lease loops
  config.py runner.py resources.py
workers/
  heavy/                # registered with agent runner; runs on laptop
    render_short.py     # (in progress) wraps make_short() as one mega-task
  light/                # (later) Cloud Run jobs for fan-out
web/static/
  chat.html             # public chat UI (vanilla JS, no framework)
  index.html            # legacy operator UI

# Legacy monolith — still works today, gets decommissioned at the end:
make_shorts.py  pipeline/  pull_stories.py  upload.py  channels/  web/server.py
```

## Running

### Production (canonical)

```bash
# That's it. Open:
open https://ytfactory-control-767262167641.us-central1.run.app
```

### Laptop agent (so renders actually run)

The laptop agent leases tasks from the live Cloud Run service. It needs
the bearer token the service expects, plus the optional TTS dependencies
matching whichever providers the channel YAMLs declare.

```bash
# Pull the agent token from Secret Manager (one-time)
gcloud secrets versions access latest --secret=ytfactory-agent-token \
  --project=ytfactory-prod > .agent-token

# Install free local TTS providers (one-time per provider, per laptop).
# All production channels default to free local TTS as of 2026-05-04 —
# no API key needed for rendering. See "TTS providers" section below
# for the full per-channel mapping.
.venv/bin/pip install f5-tts-mlx                                       # historyrecapped Shorts + sportstoriesanimated
.venv/bin/pip install --no-deps chatterbox-tts                         # mystoriesanimated (AITA)

# StyleTTS2 (0.1.6) and Indic Parler-TTS are wired in pipeline/audio.py
# but BOTH are incompatible with this venv:
#
# - StyleTTS2 0.1.6 hard-pins huggingface_hub<0.20 / librosa<0.11 /
#   networkx<3 — conflicts with everything else.
# - parler-tts is unmaintained against transformers ≥ 4.49 (which
#   diffusers 0.38 requires for image-gen). Its Config API uses the
#   removed PreTrainedConfig attribute.
#
# Both stay wired so anyone with a separate venv can use them; the
# default channel YAMLs route around them:
#   - hindutavaanimated → Kokoro hf_alpha (Hindi female)
#   - historyrecapped long-form → Kokoro bf_isabella + atempo

# Cartesia is now an OPTIONAL ship-quality fallback (per-render override
# in the channel YAML). Set the key only if you choose to flip a channel
# back to tts_provider: cartesia for a one-off premium render:
# export CARTESIA_API_KEY=sk_car_...

# Run the agent — points at production by default
YTFACTORY_AGENT_TOKEN=$(cat .agent-token) \
  .venv/bin/python -m agent.main
```

### TTS providers

Each channel declares its TTS provider in `<channel>/config.yaml`. The
production defaults as of 2026-05-04 are all **free, local, and
commercial-licensed** — `$0/render`, no per-character caps:

| Channel | Provider | Voice / config | License |
|---|---|---|---|
| historyrecapped (Shorts) | `f5_tts` | clones from `pipeline/voice_refs/theo.wav` | MIT |
| historyrecapped (long-form sleep) | `kokoro` | `bf_isabella` + atempo 0.6 | Apache 2.0 |
| mystoriesanimated | `chatterbox` | clones from `pipeline/voice_refs/sarah.wav` | MIT |
| sportstoriesanimated | `f5_tts` | clones from `pipeline/voice_refs/theo.wav` | MIT |
| hindutavaanimated | `kokoro` | `hf_alpha` (Hindi female) | Apache 2.0 |
| airecap | `kokoro` | `af_bella` | Apache 2.0 |
| rhymetimejunction | n/a (sung audio via Suno) | external_song | n/a |

**Why this changed:** Cartesia Sonic-2 was the production backbone from
2026-05-03 to 2026-05-04. The user's $5 prepay exhausted in <1h on a
10-pack of historyrecapped Shorts + a long-form sleep render — Cartesia
charges $50–100 per million characters, which compounds fast at our
cadence. The 4 production channels were migrated to free local
equivalents on 2026-05-04. Cartesia is retained as a per-channel
fallback (the previous YAML config is preserved in the comments above
each `tts_provider:` line) for one-off ship-quality renders.

**Voice cloning details:** F5-TTS-MLX and Chatterbox both clone from a
9.5s reference WAV. The reference clips at `pipeline/voice_refs/{theo,
sarah}.wav` were captured from the original Cartesia renders so the
cloned voices preserve the documentary/narrator identity the channels
were originally tuned for. To refresh a clip see
`pipeline/voice_refs/README.md`.

**Indic Parler-TTS interface differs:** It is description-conditioned,
not voice-cloned. The hindutavaanimated YAML's `tts_voice` field is a
natural-language description (e.g. "Sneha speaks in a calm…") rather
than a UUID or ref-WAV path.

### Local dev (rare — only when changing control plane code)

```bash
# 1. Set env (chat needs Azure keys, agent endpoints need a token)
source .env

# 2. Run the control plane locally
.venv/bin/uvicorn control.server_dev:app --host 127.0.0.1 --port 8765

# 3. Point the agent at localhost
YTFACTORY_CONTROL_URL=http://127.0.0.1:8765 \
YTFACTORY_AGENT_TOKEN=$YTFACTORY_AGENT_TOKEN \
  .venv/bin/python -m agent.main
```

## Production targets

| YouTube channel | Source | Aesthetic |
|---|---|---|
| **MyStoriesAnimated** | Reddit (AITA / TIFU / etc.) | Flat 2D crayon, pastel fills |
| **SportsStoriesAnimated** | Football moments | Tifo line-art + real broadcast cut-ins at the climactic moment |
| **HindutavaAnimated** | Mahabharat episodes | Amar Chitra Katha comic-book, Hindi narration |
| **History Recapped** | War/military stories | 100% archival footage with documentary narration |
| **Rhyme Time Junction** | Bilingual nursery rhymes | Continuous animation, Hinglish lyrics, recurring mascots |
| **AI Recap** *(X-first, scaffolded 2026-05-03)* | Daily AI/tech announcements | Clean isometric editorial illustration |

Other channel YAMLs are research / variant configs that share a target.

### Cross-posting to X (Twitter)

Each channel can opt into cross-posting to X by adding an `x:` block to
its `config.yaml` (see `airecap/config.yaml` for the canonical shape).
The X uploader sits at Stage 8b and reuses the same rendered mp4 the
YouTube uploader ships:

- `pipeline/x_upload.py` — chunked-upload + tweet-create via tweepy.
  CLI: `python -m pipeline.x_upload --channel <channel> --slug <slug>`.
  Idempotent record at `<channel>/uploads/<slug>.x.json` (sidecar to
  the YouTube `<slug>.json`).
- `scripts/setup_x_credentials.py` — interactive installer with hidden
  input + live verification against X's API. Run once per X handle.
- See [`docs/X_SETUP.md`](./docs/X_SETUP.md) for the full per-handle
  bring-up flow.

X is the **primary** monetization platform for AI Recap (highest-RPM
niche after crypto on X creator revenue sharing); a secondary
cross-post path for the other channels.

## Cost

Hard ceiling: **<$10/month at low traffic.** Hard rules to keep it there:

- No GKE, no persistent VM, no Vertex, no GPU on cloud, ever.
- Cloud Run scales to zero. Min instances = 0.
- Firestore queue, not Pub/Sub.
- No Cloud SQL.
- Lifecycle rules wipe artifacts on a short clock.
- Per-IP, per-user, per-session, daily Azure spend caps in code (#12).

Current monthly burn estimate: **~$3–8** at low traffic
(Cloud Run free tier + Firestore free tier + ~10–20 GiB GCS + Azure
OpenAI gpt-5.3-chat at <100 chat sessions).

## Migration status

| ✅ done | what |
|---|---|
| ✅ | git init + safety baseline |
| ✅ | docs trimmed (5 → 2 files, −1.6K LOC) |
| ✅ | dead spec-render path deleted (−2K LOC) |
| ✅ | GCP project provisioned (ytfactory-prod) |
| ✅ | laptop agent + lease protocol over outbound HTTPS |
| ✅ | GCS storage adapter + bucket lifecycle rules |
| ✅ | per-task scratch dir with unconditional cleanup |
| ✅ | chat ported from trading project, Azure OpenAI, ShortProposal extraction |
| ✅ | chat UI at `/`, vanilla JS, proposal preview + confirm |
| ✅ | per-IP daily rate limits + global Azure spend cap |
| ✅ | control plane deployed to Cloud Run, prod URL canonical |
| ✅ | RENDER_SHORT mega-task (wraps scripts/make_shorts.py) |
| ✅ | YOUTUBE_UPLOAD light worker (post-upload GC inside) |
| ✅ | RESEARCH_HANDOFF light worker (calls into pipeline/research.py) |
| ✅ | scripts/laptop_cleanup.py (dry-run reclaims ~1.9 GiB) |
| ✅ | X (Twitter) cross-post — pipeline/x_upload.py + airecap channel scaffold |

| 🟡 deferred | why |
|---|---|
| 🟡 channels reorg by target | parallel session still adding flat YAMLs; do after monolith decom |
| 🟡 move llm/cast_router/prompts → shared/ | every legacy import would need updating; do after monolith decom |
| 🟡 decommission monolith | needs first successful prod render to verify the new chain |

## Tests

```bash
.venv/bin/python -m unittest discover tests/
```

22 control-plane / agent / chat / storage tests run in ~100ms. Existing
pipeline tests run alongside.

## License

Private.
