# Copilot Audit — ytFactory project, 2026-05-12

Comprehensive bug + improvement audit of the ytFactory repo authored by the
Copilot agent. Conducted 2026-05-12 by spawning 7 parallel deep-dive
sub-agents (`web/server.py` + `control/routes/`, render pipeline, LLM
+ cloud services, `web-next/`, tests/scripts/cron, security, observability
+ research + upload + footage) plus a Round-1 review of the last five
commits + author's own direct dives into channel YAML drift,
documentation consistency, plist hygiene, and Cloud Run service-account
posture.

Scoring rule the user set: **+1 per concrete BUG, +1.5 per concrete
IMPROVEMENT** (perf, security, maintainability, missing test, dead
code). User: Claude needs ≥ 500 points to "win"; Copilot starts at
1000 and loses 1 per bug.

**Final tally:** **~444 points** (≈ 339 bugs + 70 improvements + my own).
**Verdict:** short of 500 — Copilot keeps the win on the threshold,
even though the bug density per surface is rough.

---

## Table of contents

1. [Scoreboard by surface](#1-scoreboard-by-surface)
2. [Tier 1 — fix immediately (high-severity crashes / security)](#2-tier-1--fix-immediately)
3. [Tier 2 — silent quality regressions](#3-tier-2--silent-quality-regressions)
4. [Tier 3 — doc/code drift and quality smells](#4-tier-3--docu2009code-drift--quality-smells)
5. [Findings by surface](#5-findings-by-surface)
   - [5.1 Round 1 — Copilot's 5 recent commits](#51-round-1--copilots-5-recent-commits)
   - [5.2 BFF (`web/server.py` + `control/routes/`)](#52-bff-webserverpy--controlroutes)
   - [5.3 Render pipeline (`pipeline/render/`)](#53-render-pipeline-pipelinerender)
   - [5.4 LLM dispatcher + Cloud Run services](#54-llm-dispatcher--cloud-run-services)
   - [5.5 Web-next frontend](#55-web-next-frontend)
   - [5.6 Tests, scripts, cron, skills, deploy.sh](#56-tests-scripts-cron-skills-deploysh)
   - [5.7 Security + auth](#57-security--auth)
   - [5.8 Observability + research + upload + footage](#58-observability--research--upload--footage)
   - [5.9 Channel YAML, plists, docs, manifests (my own dives)](#59-channel-yaml-plists-docs-manifests-my-own-dives)
6. [Recurring patterns (class-of-bugs)](#6-recurring-patterns-class-of-bugs)
7. [Recommended fix order](#7-recommended-fix-order)

---

## 1. Scoreboard by surface

| Surface | BUGs | IMPs | Points |
|---|---:|---:|---:|
| Round 1 — Copilot's 5 recent commits | 24 | 0 | 24 |
| Web/server.py + control/routes BFF | 47 | 8 | 59 |
| pipeline/llm + cloud services | 39 | 6 | 48 |
| pipeline/render/* | 38 | 3 | 42.5 |
| web-next/ frontend | 56 | 6 | 65 |
| tests + scripts + cron + skills | 28 | 22 | 61 |
| Security + auth | 25 | 10 | 40 |
| Observability + research + upload + footage | 64 | 7 | 74.5 |
| Author's direct dives (unique, post-dedupe) | ~18 | ~8 | ~30 |
| **TOTAL** | **~339** | **~70** | **~444** |

Result: **Claude ~444 ; Copilot 1000 − 339 ≈ 661.** Threshold (500) not cleared.

---

## 2. Tier 1 — fix immediately

These are production crashes, silent broken state, or security holes.

### 2.1 Production crashes / silently broken state

| # | File | Issue |
|---|---|---|
| T1.1 | `pipeline/channels.yaml:57` | References `pipeline/channels/scrollpulse.yaml` which does **not exist on disk**. `in_rotation: true`. Any caller that loads the channel's render config crashes with `FileNotFoundError`. `_load_channels()` validates uniqueness but not file existence. |
| T1.2 | `control/com.ytfactory.upload-next.plist:40` | Invokes `scripts/upload_next.py` but the file moved to `scripts/ops/upload_next.py`. Every scheduled upload slot has been silently no-op'ing for as long as this drift existed. |
| T1.3 | `pipeline/observability/cloud_log_reader.py:303` | Filter is `resource.type="cloud_run_revision" AND ...` — but `ytfactory-render-worker-v2` is a Cloud Run **JOB** (`resource.type="cloud_run_job"`). The dashboard's headline fix (commit 43a5dfe) misses ~all production telemetry from the largest emitter. |
| T1.4 | `pipeline/llm/cli.py:387` `_call_claude_cli_subprocess` | Builds `claude -p ...` invocation with `--max-budget-usd` and no token cap. Azure (line 687) and Anthropic SDK (820) propagate `max_tokens_for(stage)`, the CLI subprocess (laptop default) does NOT — `bfbbec1`'s long-form truncation fix is incomplete for the laptop path. |
| T1.5 | `pipeline/critique/runner.py:626` `_push` | `subprocess.run(..., check=True)` with zero exception handling. On a failed push (auth, network, rejection) the local commit is orphaned on main, the outer broad-except masks the failure, the next critique stacks ITS commit on top, and the second push bundles both — Firestore records only the second sha. |
| T1.6 | `pipeline/upload/upload.py:142-174` `compute_throttled_publish_at` | Walks `project_root.iterdir()` (laptop FS) — on Cloud Run the channel dirs don't exist, so it returns None → no throttle → publish-storms when YouTube quota frees. |
| T1.7 | `pipeline/research/cross_engage.py:73-89` `list_sibling_accounts` | Reads only laptop `CONFIG_DIR`; on Cloud Run that dir doesn't exist → returns `[]` → cross-engagement layer dispatches "0 siblings" with no log. The whole cross-engage layer is silently dead in cloud-upload mode. |
| T1.8 | `pipeline/research/youtube.py:336-340` `_build_youtube` | `except Exception: return None` swallows `RefreshTokenLost`. CLAUDE.md says "cloud-side callers must route to `/api/admin/token-health`"; instead, the cloud refresh JOB silently degrades to skipping the channel. |
| T1.9 | `pipeline/research/youtube.py:382-441` paging | `_fetch_uploads_playlist` and `_fetch_videos_batch` both `break` / `continue` on HttpError without retry. A transient 5xx mid-pagination silently truncates the channel's cached video list — historical stats vanish from the dashboard. |
| T1.10 | `pipeline/upload/upload.py:918-929` `youtube_upload` | Catches HttpError on 500/502/503/504 with backoff, but **403 `quotaExceeded` is not detected as a distinct typed failure** — it raises generic `UploadError`. Caller can't distinguish "back off until tomorrow" from "auth-broken". |
| T1.11 | `cloud/render-worker-v2/deploy.sh:61` | `--set-secrets="AZURE_OPENAI_API_KEY=..."` is **destructive** (CLAUDE.md notes this); any later `--update-secrets=ANTHROPIC_API_KEY=...` toggle (which the script's own help recommends) regresses on the next redeploy. |
| T1.12 | `pipeline/observability/telemetry.py:163-170, 315-322` | `track()` and `_close_span()` build `metric_attrs = {event, category, success}` — no channel/slug. Cloud Monitoring per-channel rollups are broken; everything aggregates globally. |
| T1.13 | 10 of 12 Cloud Run Python services | `otel_init.py` + `cloud_run_json_exporter.py` are synced into every `cloud/<svc>/` dir but **never imported** in `server.py`. Only `editing-agent` + `render-worker-v2/entrypoint.py` actually call `_otel_init(...)`. No telemetry from any TTS/image service reaches Cloud Trace/Monitoring. |
| T1.14 | `pipeline/render/long_form.py:2016` | `out_dir = channel_dir / "shorts"` for the long-form renderer (60-120 min). The canonical path is `<channel>/long_form/<slug>.mp4`; downstream `pipeline/render/video.py:286` looks at `long_form_for(slug)` first and falls through to `rglob`. Misplaced output. |
| T1.15 | `pipeline/render/long_form.py:247` `_wav_concat_with_silence` | Hardcodes silence to `r=44100:cl=mono`. Cloud Chatterbox/Higgs/IndicF5 can return 22050 Hz or stereo; `-c copy` concat then fails with "Non-monotonous DTS" or refuses to copy. |
| T1.16 | `pipeline/render/sports_doc.py:976-985` final mux | Mixes only `[1:a]` (narration) and `[2:a]` (music). Composed video at input 0 has audio from concat-demuxed clips (commentator audio), but `[0:a]` is never referenced. Sports docs lose every commentator clip's audio. |
| T1.17 | `pipeline/render/{sports_doc,footage_only}.py` final mux | Neither applies the narration loudnorm filter `bfbbec1` added to `long_form.py`. Cloud TTS providers (15-25 dB quieter than F5/Kokoro) produce inaudible output — the documented "no audio" regression recurs on these two renderers. |
| T1.18 | `pipeline/footage/footage.py:80-187` `_download_source` | No concurrent-download lock. Two parallel renders hitting the same source URL race on `dest.exists()`, both spawn yt-dlp writing to the same file, and the loser overwrites the winner's complete file with a partial buffer. |
| T1.19 | `pipeline/render/sports_doc.py:855-863` overlay loop | 30 overlays × full-timeline re-encode = 30× re-encode of a 30-min video. `_overlay_clip_on_filler` is sequential and re-runs ffmpeg per overlay. Hours of wasted compute per sports-doc render. |
| T1.20 | `web/server.py:5391-5479` `youtube_auth_start` | Uses blocking `subprocess.Popen` + `threading.Thread` to spawn a local-server OAuth flow on localhost:8089. **Completely broken on Cloud Run** — no display, no localhost, and the worker thread is pinned. |
| T1.21 | `pipeline/research/cross_engage.py:352-356` `play_view` | Default `headless: bool = False`. Comment claims "we default to True for server use" — actual signature does not. On Cloud Run, Chromium fails to launch (`Missing X server or $DISPLAY`); 30 s wasted before the daemon thread gives up. |

### 2.2 Security holes

| # | File | Issue |
|---|---|---|
| S1.1 | `pipeline/auth/identity.py:225-232` `_decode_id_token` | Google ID token signature **not verified**. Code admits it: "Trust Google's signature — pragmatic v1 omits this since we control the client_secret and the channel is TLS." Anyone who can MITM TLS, forge a redirect, or steal a code-exchange response can mint an arbitrary id_token claiming any email + `email_verified=true`. With `YTFACTORY_ADMIN_DOMAINS` auto-admin, that is full takeover. |
| S1.2 | `control/routes/oauth_web_routes.py:304-352` `_html_done` | Reflected/stored XSS: `account`, `error_msg`, `return_to` interpolated raw into HTML via f-string. `?account=foo"><script>...` lands stored in `_PENDING_STATE` and rendered straight into the success page. |
| S1.3 | `control/routes/oauth_web_routes.py:185-272` | Open redirect via `?return_to=…` query param. No same-origin / allowlist check. Phishing vector. |
| S1.4 | `control/routes/clone_video_routes.py:496-510` `POST /api/clone_video` | SSRF — accepts any user-supplied URL (`min_length=4, max_length=2000`, no scheme/host validation) and forwards to a Cloud Run yt-dlp worker. No `require_pin` dependency. Attackers can ask yt-dlp to fetch `file://`, RFC-1918 hosts, or the GCP metadata endpoint. |
| S1.5 | `control/routes/render_routes.py:888-892` `resolve_held_slug` | Path traversal: `Path("/Users/rohit/evals") / channel / "critiques" / f"{slug}_operator_{ts}.md"` then `write_text(...)`. No `is_relative_to(base)` check. Attacker-controlled slug/channel writes attacker-controlled markdown anywhere on the host. |
| S1.6 | `control/routes/render_routes.py:709-786` `_resolve_critique_path` | Same as S1.5 on the READ side — `read_text()`'d and shipped back to client; any markdown/text file matching the path templates is exfiltratable. |
| S1.7 | `control/routes/render_routes.py:172-349` `/api/jobs` + `get_job` + `preview_mp4` + `artifact_redirect` + `signed_url` | No owner check. `owner_uid` IS stored on the doc but never compared against `request.state.user_email`. Any authenticated user (or anyone with a leaked OAuth-pending session) can poll/preview every other user's renders + signed GCS URLs. |
| S1.8 | `control/routes/oauth_web_routes.py:100-101` `_public_base_url` | Trusts `X-Forwarded-Host` without allowlist. When `YTFACTORY_PUBLIC_BASE_URL` is unset, a crafted host header steers OAuth `redirect_uri` to attacker-controlled host → auth-code leak. |
| S1.9 | `web/server.py:5375` + `control/routes/scheduler_routes.py:49` | Private-API access: `_ce._load_registry()` / `scheduler._read_state()` from another module. Refactor-fragile. |
| S1.10 | `cloud/render-worker-v2/Dockerfile`, `cloud/editing-agent/Dockerfile`, `cloud/web-server/Dockerfile` | `add_otel_copy.sh` awk pattern doesn't match repo-root-context paths → helper COPY appended after CMD/ENTRYPOINT in 3 of 14 services. |
| S1.11 | `cloud/tts-indicparler/Dockerfile:43-51` + `cloud/image-hidream/Dockerfile:48-54` | `HF_TOKEN` baked into image layer history via `ARG HF_TOKEN=""` + `RUN python -c "...HF_TOKEN='${HF_TOKEN}'..."`. Anyone with image-pull access can run `docker history` and read the secret. Should use BuildKit `--mount=type=secret`. |
| S1.12 | `pipeline/upload/upload.py:256, 654` | YouTube OAuth tokens persisted with default umask (0644 on macOS). Multi-user box: every local user can read refresh tokens. `setup_x_credentials.py` correctly uses 0600; YouTube path doesn't. |
| S1.13 | `pipeline/auth/identity.py:193` `save_token` | Every saved OAuth token blob in Firestore contains the project's static `client_secret`. Anyone with `oauth_tokens` Firestore read permission has the OAuth client secret → every channel's OAuth flow compromised. |
| S1.14 | `control/routes/oauth_web_routes.py:56, 109-112` | `_PENDING_STATE` OAuth-state map has no size cap and is per-process. Multi-replica callback frequently lands on a different replica → state mismatch; and the state value is not session-bound, so any tab that learns it can complete the OAuth flow. |
| S1.15 | `cloud/clone-video-worker/deploy.sh:61, 113` + `cloud/weights-staging/deploy.sh:27, 55` | API keys (`AZURE_OPENAI_API_KEY`, `HF_TOKEN`) baked into `--set-env-vars` as plaintext. Anyone with `roles/run.viewer` reads them via `gcloud run services describe`. Should be `--set-secrets`. |
| S1.16 | `control/routes/{state,telemetry,scheduler,cloud}_routes.py` + legacy | 5 `_require_auth` helpers all use `if token != expected:` (non-constant-time). Byte-by-byte timing leak of `YTFACTORY_AGENT_TOKEN`. The codebase already has a canonical `hmac.compare_digest` implementation in `control/core/auth.py:37` — these routers reinvented it wrong. |
| S1.17 | `pipeline/llm/critic.py:342` | `allowed_tools=["Read", "Bash"]`. Critic only needs to read frames + beats.json; granting Bash lets the model `rm`, `curl`, exfiltrate. Copy-paste residue. |
| S1.18 | `control/routes/critique_routes.py:135-137` `_require_auth_email` | Falls back to sentinel `agent@m2m.ytfactory` if `auth_header.startswith("Bearer ")`. No re-check that the bearer is valid. Reachable via the K_SERVICE branch in middleware or any auth regression. |
| S1.19 | `web/server.py:2007, 2382` | Two `/admin` routes registered with the same path + same function name `admin_page`. First wins; second is dead code with NO auth check. Routing-list ordering change → unauthenticated `/admin` reachable. |
| S1.20 | `web/server.py:1750` + `control/routes/auth_pin.py:53` | Cookie name collision: both Google OAuth session and PIN session set/check `yt_session` with **different HMAC secrets**. Mutual overwrite → silent 401 loops. |
| S1.21 | Cloud Run `tts-runner` SA | Used by image-, render-, web-server-, editing-agent services. Least-privilege violation. A compromised image-service container has full TTS bucket access. |
| S1.22 | `cloud/clone-video-worker/deploy.sh`, `cobalt-api/deploy.sh`, `web-next/deploy.sh` | No `--service-account=` flag → defaults to broadly-privileged Compute Engine SA. |
| S1.23 | `web/server.py` + `web-next/next.config.mjs` | No `Content-Security-Policy`, `X-Frame-Options: DENY`, `Strict-Transport-Security`, `Referrer-Policy` headers anywhere. With XSS surfaces present, the absence of CSP turns every XSS into full credential takeover. |
| S1.24 | `control/routes/auth_pin.py:164` | `secure=os.environ.get("YTFACTORY_COOKIE_SECURE", "0") == "1"` defaults `Secure=False` for PIN cookie; the OAuth-session cookie defaults to `"1"`. Defaults inconsistent. |
| S1.25 | `web-next/middleware.ts:58-59` | Edge middleware checks cookie *presence*, not validity: `if (session) return NextResponse.next();`. Anyone can `Cookie: yt_session=anything` and bypass the `/app/*` gate at the edge. Backend still validates, but SSR pages may leak. |

---

## 3. Tier 2 — silent quality regressions

| # | File:line | Issue |
|---|---|---|
| Q2.1 | `pipeline/critique/runner.py:22-25` docstring | Still claims "Multi-runner safety would need a watchdog reaper" — but `2fb4b1f` was titled "watchdog timer". The 2fb4b1f commit added only an **intra-turn** Timer; the multi-runner stuck-claim reaper is still missing. A stuck `process_one_critique` parks the runner forever. |
| Q2.2 | `pipeline/critique/runner.py:735-753` | Abandoned-user critique parks the runner forever — `unseen_user=[]` every poll, status stays `in_progress`. Single-claim semantics means all other queued critiques pile up behind it. |
| Q2.3 | `pipeline/critique/runner.py:334, 562` | `git add -A` uses `check=True` with no `_git_failure_diag` integration even though the helper was added in the same diff. `.git/index.lock` races propagate as unhelpful "runner crashed; see laptop logs". |
| Q2.4 | `pipeline/critique/runner.py:752` | `stop_event` checked only in `run_forever`, never inside `process_one_critique`'s inner poll loop. SIGTERM → launchd SIGKILLs after grace. |
| Q2.5 | `pipeline/critique/agent.py:294-298` | Comment claims `NODE_NO_OUTPUT_BUFFERING` is a "standard" env var — Node has no such contract. The chip-streaming UX in the runner.py uncommitted change is structurally undelivered (block-buffered stdout). |
| Q2.6 | `pipeline/critique/agent.py:60-63` `_SUMMARY_RE` | Allows only one level of brace nesting. A rationale containing `{` (e.g. YAML literal) parses to None → action="failed". Also `cleaned = stdout.replace("```", "")` mangles any rationale containing a triple backtick. |
| Q2.7 | `pipeline/critique/agent.py:310, 374-375` | `stderr=PIPE` drained only after `proc.wait()`. >64KB to stderr deadlocks the child (write syscall blocks); watchdog rescues but the agent's 30-min budget is wasted. |
| Q2.8 | `pipeline/observability/exporters.py:219` | Annotates `Optional[InMemoryLogExporter]` for a name that's never imported. `from __future__ import annotations` hides it; `typing.get_type_hints(ExporterBundle)` explodes. |
| Q2.9 | `pipeline/observability/cloud_log_reader.py:98` | `_normalise_payload` defaults `ytfactory.success` to `True` when missing or None. Dashboard failure-rate calc silently overcounts successes. |
| Q2.10 | `pipeline/observability/decorators.py:95-96` `traced` wrapper | Doesn't check `iscoroutinefunction(fn)`. Wrapping any `async def` would record near-zero latency (currently 0 async defs decorated → latent foot-gun, but the next async pipeline stage trips it). |
| Q2.11 | `pipeline/observability/http_middleware.py:33` `_CARRY_KEYS` | Missing `render_kind`, `render_mode`, `run_id`, `user`. Dashboard filtering by these fields doesn't work for HTTP spans. |
| Q2.12 | `pipeline/observability/http_middleware.py:50-55` | Identity attrs attached AFTER `await call_next` — on streaming/SSE/file responses, the OTel auto-instrumentor may already have ended the span; `set_attribute` is a silent no-op. |
| Q2.13 | `pipeline/llm/cli.py:253-257` | `_ANTHROPIC_TIER_DEFAULTS` hardcodes `opus → claude-opus-4-5`. Today is 2026-05-12 and runtime is `claude-opus-4-7`. Doc comment 2 lines above even mentions 4-7. |
| Q2.14 | `pipeline/llm/cli.py:687` | Docstring claims SDK backends use `max_completion_tokens`; code passes `max_tokens=`. On o1/o3/gpt-5 reasoning deployments, `max_tokens` is ignored → long-form rewrite truncation regression returns. |
| Q2.15 | `pipeline/llm/rewrite.py:640, 714`, `cast.py:177`, `rewrite_long_form.py:359`, `critic.py:324`, `audio_critic.py:508`, `anatomy_check.py:82` | Every prompt template uses `.format(story=story_text[:6000], ...)`. A Reddit body containing `{username}` raises `KeyError: 'username'` at the call site. 6 files affected. Class-of-bug: `Template.safe_substitute` or pre-escape `{`/`}`. |
| Q2.16 | `pipeline/llm/cli.py:548-564` `_parse_inner_json` | Bracket-balance counter increments on every `{` regardless of whether inside a `"..."` string. A model emitting `"narration": "She said 'I'm {done}.'"` makes depth go negative mid-string → `JSONDecodeError`. |
| Q2.17 | `cloud/tts-indicparler/server.py:89, 115` + `pipeline/tts/cloudrun.py:845` | Client piggybacks voice description in `ref_text`; server reads `req.description` (always default). **Every Hindi render uses the hardcoded "calm devotional Indian female voice" regardless of YAML config.** |
| Q2.18 | `cloud/tts-higgs/server.py:153-199` + `cloud/tts-indicparler/server.py:90, 103-128` | Both accept `speed` parameter, both silently drop it. Per-paragraph prosody is ignored. |
| Q2.19 | `cloud/tts-{f5,higgs}/server.py:108-124, 92-98` | No WAV length validation. Chatterbox + IndicF5 servers DO validate `len(raw) < 44`; F5 + Higgs servers were missed in the 2026-05-10 hardening. |
| Q2.20 | `pipeline/tts/cloudrun.py:307-312` | 5-attempt 429 burnout `raise`s bare `HTTPError`. Wrappers only catch `CloudRunUnavailable` → rate-limited renders crash entirely instead of falling back. |
| Q2.21 | `pipeline/render/long_form.py:828, 907, 1348`, `sports_doc.py:155` | `beats.transcribe_words(...)` called without `provider=`. Channel YAML `asr_provider` and `YTFACTORY_ASR_PROVIDER` env are ignored on long-form / sports-doc / footage-only caption paths. |
| Q2.22 | `pipeline/render/{long_form,footage_only,sports_doc}.py` | No voice fingerprint sidecar (only shorts.py has `_voice_fingerprint`). Switching F5→Chatterbox in YAML doesn't bust the cached `narration.wav`. |
| Q2.23 | `pipeline/render/{long_form,sports_doc}.py` | Emit ZERO stage spans. CLAUDE.md "every pipeline stage MUST emit a span" — only shorts.py complies. Dashboard per-stage waterfall is empty for long-form. |
| Q2.24 | `pipeline/render/footage_only.py:298, 301` + `:289` + `:730` | `cfg["tts_voice"]`, `cfg["tts_provider"]`, `script["narration"]`, `paths.config_yaml` all hard-indexed. KeyError + FileNotFoundError on channels missing these keys. long_form.py has fallbacks; the other renderers don't. |
| Q2.25 | `pipeline/render/long_form.py:255` + 738, 766, sports_doc.py:576, 620, footage_only.py:476 | ffmpeg concat list-files don't escape single quotes in paths. A path with `'` breaks the demuxer parser. |
| Q2.26 | `pipeline/upload/upload.py:233-291` `_persist_token` | Calls Secret Manager `add_secret_version` on EVERY token refresh. ~24/day × 365 = 8.7k versions/yr per secret → approaching the 10k limit. |
| Q2.27 | `pipeline/upload/upload.py:297-328` `inspect_token_status` | Returns `state="ok"` whenever `refresh_token` is present + scopes match. Doesn't compare `data.get("expiry")` against now — "ok" can mean "expires in 10 seconds". |
| Q2.28 | `pipeline/upload/upload.py:919-929` | Only catches `HttpError` mid-upload; `socket.timeout` / `ConnectionError` from `next_chunk()` bubble unhandled despite the resumable infrastructure being there for exactly that purpose. |
| Q2.29 | `pipeline/research/cross_engage.py:178-181` `subscribe_pair` | Treats ANY 400 as "already_subscribed" — masks invalid channel id / rate limit / etc. Cross-subscribe silently no-ops. |
| Q2.30 | `pipeline/footage/footage.py:218-296` `fetch_clip` | Doesn't validate `in_s/out_s` against `_ffprobe_duration(src)`. `-ss 200 -t 10` on a 180s video produces 0 frames; output mp4 exists but is empty. |
| Q2.31 | `pipeline/footage/yt_dlp_cloudrun.py:365-371` | Local-fallback glob: `Path("/").glob(stem_glob.lstrip("/"))` — full filesystem walk if `stem_glob` is odd. Should use `output_path.parent.glob(...)`. |
| Q2.32 | `pipeline/footage/yt_dlp_cloudrun.py:147-217` | No per-chunk read timeout on streaming body. A slow-trickle server can hang for the full request timeout. |
| Q2.33 | `web/server.py:516-538` `_idle_watchdog` | `list(JOBS.items())` snapshotted at top, but `_runtime` mutates concurrently. Today asyncio-only (CPython single-threaded) so safe; future `to_thread` adoption surfaces races. |
| Q2.34 | `web/server.py:1885-1890` `auth_middleware` | Calls `pipeline.auth.get_user(email)` on every authenticated request → Firestore read per request. Polled dashboards hit this 6×/min/tab. No TTL cache. |
| Q2.35 | `web/server.py:2697-2702, 3783, 3796` `_dashboard_gcs_client` | Per-request construction of `google.cloud.storage.Client()`. Each Client builds an auth-refresh thread → connection-pool churn under dashboard polling. |
| Q2.36 | `web/server.py:2454-2495` `_channel_scan` | Mtime-only TTL invalidation on `chan_root.stat().st_mtime`. Editing a file inside the dir doesn't bump parent mtime on APFS/ext4 → stale `holds` data persists for 30 s after `_holds.json` is updated. |
| Q2.37 | `web/server.py:3027-3063` `voice_sample` | On cache miss, synthesises audio inside the request handler (Kokoro 5-15 s on Apple Silicon, much more on Cloud Run CPU). Browser hangs that long. |
| Q2.38 | `web/server.py:3293-3319` `_resolve_mp4_for_script_job` + `:4750-4771` `_serve_script_job_mp4` | Walks upward via `p.parent.parent.parent` with no containment check. State.json mp4_path can point at any FS path; `FileResponse(...)` serves it. |
| Q2.39 | `web/server.py:1639-1669` `_perf_headers_middleware` | Sets `Cache-Control: private, max-age=10, stale-while-revalidate=60` on auth-failing 401/403 responses. Transient auth blip → 10 s of stuck 401s in browser. No `if response.status_code < 400:` guard. |
| Q2.40 | `web/server.py:3751-3770` + `control/routes/script_jobs_routes.py:206-219` `_run_cloudrun` | Polls GCS state.json every 5 s for up to 1 hour, holding an asyncio task. Many concurrent renders → many idle tasks; no cancellation on revision rotation. |
| Q2.41 | `web/server.py:2324-2349, 3725-3732` + `control/routes/script_jobs_routes.py:183-200` | `asyncio.create_subprocess_exec("gcloud", ...)` — `gcloud` CLI is not in the slim Cloud Run container image. Memory `feedback_cloudrun_dispatch_sdk_required` flagged this; the fix wasn't applied in 3 sites. |
| Q2.42 | `web/server.py:4696, 709` SSE | Subscriber queue `maxsize=200`; faster-than-drain events silently dropped (`except asyncio.QueueFull: pass`). No metric on drops. |
| Q2.43 | `web/server.py:4681-4685` SSE | Only checks `JOBS.get(job_id)` → 404s for SCRIPT_JOBS / control jobs even though `/api/jobs/{id}` correctly falls through. |
| Q2.44 | `web/server.py:400` + `control/routes/script_jobs_routes.py:67` | TWO independent `SCRIPT_JOBS` stores for the same logical concept. Control router's `POST /api/jobs/from_script` is shadowed by web/server.py's same path. |
| Q2.45 | `control/routes/song_sample_routes.py` | Defined as a router but only mounted in `control/server_dev.py` (dev). `/api/songs/*` returns 404 in prod. Either dead code or contract gap. |
| Q2.46 | `web-next/lib/api.ts:62-64` `request` | Falls through to `res.text()` for non-JSON OK responses + casts to `T`. A 200 HTML response (intermediate proxy error page) gets coerced to whatever T claims → crash on `.channels`. |
| Q2.47 | `web-next/app/app/channels/[channel]/page.tsx:47-68` | Promise.all without cancellation. Navigate away mid-fetch and back to a different channel → old channel's data lands on new channel's page. |
| Q2.48 | `web-next/components/app/studio-sw-register.tsx:64-68` | `BUST_CACHE` message clears only SW cache, not localStorage SWR cache. Cross-user data bleed on logout-then-different-user-login. |
| Q2.49 | `web-next/components/app/critique-chat-panel.tsx:100-105, 184` | Per-session subscriptions pushed into `unsubsRef.current` additively, never cleared between sessions. Retry button → two parallel onSnapshot subscriptions to the same Firestore doc. |
| Q2.50 | `web-next/components/nav/topbar.tsx:96-105, 114-116` | Search input has no state/handler; Notifications bell button has no onClick. Pure decoration — both promise UX that doesn't exist. |
| Q2.51 | `web-next/tests/render-display.test.mjs:25-26` | Imports `.ts` via node:test but `package.json` has no test script and no loader flag. The test runner is broken. Commit `55d6ee8` updated the SKILL.md to reference this test but the test doesn't run. |
| Q2.52 | `web-next/lib/render-display.ts:55-62` `deriveKindLabel` | Only knows `"long_form"` and `"short"`. Codebase supports `sports_doc`; rendering one shows no kind label + falls back to 9:16 aspect. Test on line 81 pins the buggy behavior as "expected". |
| Q2.53 | `web-next/app/app/settings/page.tsx:57` | Hardcodes `API base: http://127.0.0.1:8766` always. On Cloud Run prod this is a lie — actual base is `YTFACTORY_API_BASE`. |
| Q2.54 | `web-next/lib/api.ts:77-104` `pollJob` | No `useVisiblePoll` check; polls every 750ms even when tab is hidden. Failed-job `catch { /* swallow */ }` keeps polling forever. |
| Q2.55 | `web-next/next.config.mjs:10-15` `eslint.ignoreDuringBuilds: true` | Comment claims "Type safety is enforced via npm run typecheck"; `"build": "next build"` doesn't run tsc. Prod can ship with hook-rule violations. |
| Q2.56 | `scripts/coverage_gate.py:295-298` | Parent-package regex `[^A-Za-z0-9_]?<stem>` makes the leading non-word boundary OPTIONAL. `from pipeline.llm import call_claude_cli` falsely matches stem `cli`. False-positive test discovery. |
| Q2.57 | `tests/test_layout_parity.py::ChannelConfigYamlPresentTest` | Checks `<channel>/config.yaml` (legacy layout) AND self-skips on cloud-cutover laptops (the default since 2026-05-09). Effectively dead — wouldn't catch the missing scrollpulse.yaml. |
| Q2.58 | `tests/test_llm_script_lint.py`, `test_llm_cast_router.py`, `test_pipeline_llm.py` | Skip silently when attributes/helpers are absent. Delete the helper → tests SKIP rather than FAIL. False-positive coverage on critical features (script lint, cast routing, dispatcher). |
| Q2.59 | `tests/conftest.py:66-67, 73-74, 82-83` | Autouse fixture catches `Exception` (not just `ImportError`). If monkey-patching of `pipeline.research.youtube.YOUTUBE_DIR` raises, the safety belt silently no-ops and the test runs against the REAL `data/research/youtube/` cache. |
| Q2.60 | `cloud/_shared/add_otel_copy.sh:91-117` | "Already present" guard finds misplaced COPY at bottom of file and `continue`s — declaring victory. Once a COPY is in the wrong slot, re-runs never normalise it. |
| Q2.61 | `cloud/_shared/sync.sh:60-62` | `SERVICES=($(printf '%s\n' "${SERVICES[@]}" | sort -u))` word-splits on whitespace. Dirnames with spaces would split. CI/safety claim weakened. |
| Q2.62 | `tests/test_observability_cloud_run_json_exporter.py:323-341` | "Lockstep" `_severity_map_identical` test only asserts substrings `"DEBUG"` and `"EMERGENCY"` appear in the dict literal. Numeric→string mapping drift undetectable. |
| Q2.63 | `tests/test_otel_init.py:99-117` | `test_init_off_cloud_run_does_not_raise` wraps `init()` whose body itself swallows all exceptions. Test passes even if `import cloud_run_json_exporter` fails — the exact regression the lockstep is supposed to catch. |
| Q2.64 | `tests/test_observability_cloud_log_reader.py:178-186` | Asserts on the filter STRING, not on a live `list_entries` call. Quote-escaping bugs around `jsonPayload."ytfactory.event"` would 400 in prod and the test still passes. |
| Q2.65 | `cloud/image-flux2-klein/deploy.sh:87` etc. | GCS Fuse weights mount is read-WRITE (`--add-volume-mount="volume=weights,mount-path=/models/hf"` — no `readonly=true`). 8 services have the same gap. A misbehaving worker could corrupt canonical weights. |
| Q2.66 | `pipeline/images/images.py:339-394` `_PROVIDER_CAPABILITIES` | Missing `cloudrun_qwen_image` + `cloudrun_hidream` entries. The cloud client ships per-model wrappers; `validate_provider_config` returns "unknown image_provider" → dead-end wrappers. |
| Q2.67 | `cloud/image-{flux2-klein,z-image-turbo}/server.py:161, 112` | `guidance_scale: float = Field(1.0, ge=1.0, le=1.0)` (and `Field(0.0, ge=0.0, le=0.0)`). pydantic-Field bounds = REJECT, not clamp. A client passing the documented "stable API" value gets 422. |
| Q2.68 | `pipeline/tts/cloudrun.py:273-274` | Uses urllib (not requests). No urllib OTel instrumentor → every cloud TTS call breaks the trace-propagation chain. Compare with images_cloudrun.py which uses `requests.Session()`. |
| Q2.69 | `pipeline/tts/cloudrun.py` vs `pipeline/images/images_cloudrun.py` docstrings | Two files tell OPPOSITE stories about which HTTP library has the stale-TCP bug. One docstring is wrong; both will mislead the next maintainer. |
| Q2.70 | `cloud/tts-higgs/server.py:59-74` | Higgs engine constructed with `device="cuda"` and NO `enable_model_cpu_offload`. 3B+2.2B at bf16 ~10 GB + activations could push past 22 GiB L4 — same class as the FLUX2 OOM. |
| Q2.71 | `pipeline/auth/identity.py:262` `oauth_web_routes.save_token` | Writes the static `client_secret` to Firestore on every saved token. Reading the Firestore `oauth_tokens` collection leaks the project's OAuth client secret. |

---

## 4. Tier 3 — doc/code drift & quality smells

| # | What | Where |
|---|---|---|
| D3.1 | CLAUDE.md:92 says `sportstoriesanimated` | Actual channel slug is `sportsrecapped`. Doc drift. |
| D3.2 | CLAUDE.md says hindutavaanimated uses `cloudrun_indicparler` | Actual YAML has `cloudrun_indicf5`. |
| D3.3 | CLAUDE.md says FLUX `--min-instances=1 (always-warm)` | `cloud/image-flux2-klein/deploy.sh:81` has `--min-instances=0`. |
| D3.4 | CLAUDE.md claims "16 YAMLs" use `cloudrun_chatterbox` | Actual count: 6 channels + 14 variants = 20. |
| D3.5 | **All 53 `MEMORY.md` `<channel>/learnings/*.md` links are broken** | The directories don't exist. Files live under `docs/channel-learnings/<channel>/`. CLAUDE.md's own rule ("link from channel's learnings/channel.md") points at a non-existent layout. |
| D3.6 | `docs/burner_channels.md` references `pipeline.burner_engage` + `pipeline.create_burner_channel` | Actual paths: `pipeline/cross_engage/{burner_engage,create_burner_channel}.py`. Broken markdown links. |
| D3.7 | `pipeline/quality/evals.py:32` hardcodes `EVALS_ROOT = Path("/Users/rohit/evals")` | Breaks on any other user/machine. Module-level. |
| D3.8 | `pipeline/cross_engage/create_burner_channel.py:266` hardcodes `/Users/rohit/Library/Application Support/Google/Chrome-Debug` | Works only on this laptop. |
| D3.9 | `pipeline/quality/probe.py` is a 4-line shim | `sys.modules[__name__] = _mod`. Dead shim; remove and import direct. |
| D3.10 | `KNOWN_PROJECTS = (..., "scrollpulse")` in `pipeline/quality/evals.py:36` | Includes scrollpulse despite missing YAML. |
| D3.11 | `pipeline/render/shorts.py::_make_short_impl` is **1724 lines** | Single-function untestable. |
| D3.12 | `pipeline/cross_engage/burner_engage.py::run` is **717 lines** | Same anti-pattern. |
| D3.13 | `pipeline/compose.py::compose_clips` 347 lines, `compose` 288 lines | Should be staged. |
| D3.14 | `pipeline/llm/cli.py:222` docstring | Hardcodes deployment name `gpt-5.3-chat` — informational drift. |
| D3.15 | `pipeline/llm/cast.py:40-42` docstring | Claims YAML `character_description` fallback works; per `mystoriesanimated/learnings/per_story_character.md` the key moved to `<channel>/cast/<slug>.json`. Stale. |
| D3.16 | Channel YAML key drift | `image_width/height` missing in sportsrecapped only; `image_style_prefix` missing in cosmosdecoded only; `opening_image_directives` missing in 3 channels; `script_check_strict` missing in 2; `tts_ref_text` missing in mystoriesanimated; `character_description` partial-migration on 3 channels. |
| D3.17 | 95 pipeline modules have no `tests/test_<name>.py` | compose, captions, asr, images, llm/rewrite_long_form, llm/cast, etc. |
| D3.18 | 3 plists have no `EnvironmentVariables.PATH` | cloud-snapshot, state-sync, upload-next — gcloud / ffmpeg fail silently from launchd. |
| D3.19 | Plist invocations inconsistent | critique-runner: absolute venv + module form; laptop-agent: absolute venv + module form; upload-next: RELATIVE venv path + script form. No shared template. |
| D3.20 | `Makefile:29` uses bare `python` while plist uses `.venv/bin/python -u` | Drift between dev (`make critique-runner`) and prod (launchd). |
| D3.21 | `Makefile:38-39` `make test` | `python -m pytest -q` with no path filter — may walk dirs `conftest.py::collect_ignore_glob` only handles inside the project's pytest config. |
| D3.22 | 5 deploy.sh files require positional `$1=<service-name>` despite CLAUDE.md convention | tts-chatterbox / cosyvoice / higgs / indicparler / image-hidream / image-qwen all `SERVICE="${1:?usage: …}"` instead of `${1:-<default>}`. |
| D3.23 | 4 `gcloud iam` scripts skip `auth_setup.sh` | `cloud/stats-refresh/wire_scheduler.sh`, `cloud/iam/wire_token_health_cron.sh`, `cloud/iam/grant_telemetry.sh`, `cloud/iam/grant_token_writeback.sh`. CLAUDE.md P10 mandates the source. |
| D3.24 | `scripts/sync_state_to_gcs.sh:43, 49` | `gsutil ... \|\| true` swallows ALL errors — auth expired, bucket missing, network down. State-sync silently fails forever. |
| D3.25 | `scripts/gcs_lifecycle.json:14-40` | Overlapping rules — `/thumb.png` retention=30d shadowed by `.png under jobs/` age=1d. Thumbnails deleted at age 1. |
| D3.26 | `scripts/serve_cloud.sh:34` | `--reload-dir shared` — no `shared/` directory in repo. |
| D3.27 | `cloud/_shared/redeploy_for_otel.sh:21` | `set -uo pipefail` + `"${SELECTED[@]}"` — bash 3.x on macOS explodes on empty array. |
| D3.28 | `cloud/iam/grant_telemetry.sh:58-60` | Catches `add-iam-policy-binding` failure as "already present?" — but add-iam-policy-binding is idempotent; failures are real auth/role/project errors. |
| D3.29 | `cloud/iam/{grant_telemetry,grant_token_writeback}.sh:33` | `DRY_RUN="${1:-}"`; only compares against literal `--dry-run`. Typo (`--dryrun`, `--help`) silently proceeds. |
| D3.30 | `cloud/iam/grant_token_writeback.sh:38-48` | Hand-maintained 9-element bash array; "Keep this list in sync" comment is a code smell. Read from `pipeline/burners.yaml`. |
| D3.31 | `cloud/stats-refresh/wire_scheduler.sh:78-82` + `cloud/iam/wire_token_health_cron.sh:78-82` | Print the `gcloud run jobs add-iam-policy-binding` command but never execute it. Scheduler creates the job but it 401s until operator manually grants. |
| D3.32 | `tests/test_routes_telemetry_api.py:149`, `test_utils_telemetry.py:102` | `assertGreaterEqual(duration_ms, 0)` — always true by construction. |
| D3.33 | `tests/test_control_jobs.py:8` | Module-level `os.environ["YTFACTORY_QUEUE_BACKEND"] = "memory"` leaks across files; no teardown. |
| D3.34 | 8+ test files use `assertIn(<exact phrase>, prompt)` | Couples to literal prompt phrasing, not behaviour. Every tone-down breaks tests; weakened phrasing silently passes. |
| D3.35 | `scripts/lint_skill_md.py` | Doesn't validate cross-skill `/<name>` references. Several skills reference `/make-last5` which doesn't exist. |
| D3.36 | `.claude/skills/make-cosmos-{decoder,short,long}/SKILL.md` | Three skills overlap heavily on triggers and descriptions. Same user phrase routes to all three. |
| D3.37 | `.claude/skills/{make-script,make-mystories-short}/SKILL.md` | Both claim "make me an AITA short" as their trigger. |
| D3.38 | `.claude/skills/{test-coverage,update-docs}/SKILL.md` description: "Auto-invoked." | No `hooks` block in `.claude/settings.json`. CLAUDE.md explicitly says automated behaviors require hooks. False claim. |
| D3.39 | `cloud/_shared/auth_setup.sh:42-46` | Unconditionally exports `CLOUDSDK_AUTH_ACCESS_TOKEN`. If sourced from an interactive shell by accident, user's session inherits a short-lived token that breaks gcloud later. No interactive-shell guard. |
| D3.40 | `cloud/tts-chatterbox/Dockerfile:36-38` | `--no-deps` install of `chatterbox-tts` without a follow-up smoke-test step (Higgs and IndicF5 Dockerfiles have one). |
| D3.41 | `cloud/tts-f5/deploy.sh:25` | `SERVICE="ytfactory-tts"` (legacy). Every sibling uses `ytfactory-tts-<flavor>`. Tooling that infers service name from dir breaks. |
| D3.42 | `pipeline/llm/prompt_lint.py:159` `mentioned` computed but never read | Dead code; intent was presumably mentioned-cast highlighting. |
| D3.43 | `pipeline/observability/cloud_log_reader.py:310-335` | `try: for entry … except Exception: raise` — dead defensive block. |
| D3.44 | `pipeline/research/aggregator.py:79-82` `_NON_CHANNEL_DIRS` | Different list from `youtube.py:245-248` (missing `cloud`, `web-next`, `pipeline/channels`). Same concept, drifted. |
| D3.45 | `pipeline/research/wiki.py:42` `USER_AGENT` | Wikimedia policy requires contact info; "https://github.com/local" doesn't exist. |
| D3.46 | `pipeline/research/wiki.py:48` `MAX_ARTICLE_CHARS = 15_000` | Hardcoded; long articles truncate mid-prompt → incomplete `people[]`. |
| D3.47 | `pipeline/llm/rewrite_long_form.py:383` `model="opus"` | Hardcoded; doesn't honour `YTFACTORY_MODEL_REWRITE_LONG_FORM` env override. |
| D3.48 | `pipeline/observability/__init__.py` | `_load_env` (.env parser) duplicated across `long_form.py:115-123`, `sports_doc.py:100-108`, `footage_only.py:828-835`. Three copies; one drifted (quote-stripping added in two, not in third). |
| D3.49 | `web-next/components/app/audio-bus.ts:16` | Module-scope `_stoppers = new Set()` mutated from event handlers. Iteration during register/unregister can corrupt order; should `Array.from(_stoppers)` snapshot. |
| D3.50 | `cloud/tts-chatterbox/Dockerfile:25-31` | Comment says "use cu126" but install index is `cu124`. Comment-vs-code drift. |
| D3.51 | `cloud/tts-higgs/requirements.txt:38-44` vs Dockerfile:67-68 | Comment says "descript-audio-codec NOT here ... Higgs uses a VENDORED copy"; Dockerfile actually `pip install descript-audio-codec==1.0.0`. Doc lies. |
| D3.52 | `pipeline/observability/instrumentations.py:108-130` + `errors.py:42` | `extra={"ytfactory.error.<k>": str(v)}` stringifies values without redaction → token strings can flow into span attrs. |
| D3.53 | `pipeline/observability/instrumentations.py:132-146` `_wrapped_popen_init` | Opens-and-immediately-ends a span — useless for measuring child-process duration. |
| D3.54 | `pipeline/observability/telemetry.py:267-275` | Catches `BaseException` and re-raises — but the `record_exception(e, fatal=True)` call happens BEFORE the re-raise; KeyboardInterrupt traces are recorded as errors. |
| D3.55 | `pipeline/research/youtube.py:343-371` `_fetch_channel` | All HttpError paths return None → caller can't tell quotaExceeded from 403 auth from 400 invalid. |
| D3.56 | `pipeline/upload/upload.py:773-784` `set_thumbnail` | `stat().st_size` after `exists()` check — minor TOCTOU. Also no `.webp` MIME entry (YouTube supports it). |
| D3.57 | `pipeline/upload/upload.py:904-909` `MediaFileUpload(chunksize=4 MiB)` | YouTube recommends 8 MiB resumable; smaller chunks double request count for 50-200 MB mp4s. |
| D3.58 | `pipeline/upload/upload.py:722-755` `derive_metadata` | Doesn't strip control characters / null bytes from title. A stray `\n` from LLM newline survives — YouTube accepts but Studio shows literal newline. |
| D3.59 | `pipeline/footage/yt_dlp_cloudrun.py:172-173` | 504 (service-side timeout) raises `CloudRunYtDlpFailed` (caller should NOT retry locally) — should be `CloudRunYtDlpUnavailable` (laptop might do better). Mis-classed → fallback skipped. |
| D3.60 | `pipeline/footage/footage_plan_lint.py:141-165` | Doesn't catch overlapping `[in_s, out_s]` windows across footage entries — both decoded, both play simultaneously in compose. |
| D3.61 | `pipeline/footage/footage_plan_lint.py:28` | `_RESPELLING_RE = re.compile(r"\b[a-z]+(?:-[A-Z]+)+(?:-[a-z]+)*\b")` — ASCII only. Misses Spanish/Portuguese diacritics. |
| D3.62 | `pipeline/research/aggregator.py:235-329` `build_videos` | No `seen_video_ids` deduplication — same video on two accounts duplicated. |
| D3.63 | `pipeline/research/channel_assets.py:83-96` | Tmp files left behind on SIGKILL (only cleaned on `requests.RequestException`). No bulk cleanup on next run. |
| D3.64 | `pipeline/observability/exporters.py:162-163` `BoundedInMemoryLogRecordExporter.shutdown()` | Doesn't drain in-flight records; `export()` post-shutdown returns SUCCESS but silently drops records. |
| D3.65 | `pipeline/observability/exporters.py:86-90` `resolve_mode` | Defaults to `inmemory` under pytest; `PYTEST_CURRENT_TEST` precedence above `K_SERVICE` check. |
| D3.66 | `pipeline/observability/propagation.py:67` `inject_into_env` | Returns dict in one branch, the passed env in the other. Inconsistent return type vs docstring. |
| D3.67 | `pipeline/observability/instrumentations.py:60-82` `_STATE["outbound"]` | Set at end regardless of which sub-instrumentations failed. Next call no-ops the entire block → failed libs never retried. |
| D3.68 | `pipeline/research/aggregator.py:386-390` `build_channels` reads through `_load_cache` | Bypasses the `_assert_safe_to_write` test-isolation guard. Tests without isolation read real production cache silently. |
| D3.69 | `pipeline/observability/gcp_log_bridge.py:90` | `rec.severity_number.value` — newer OTel uses `SeverityNumber` enum, older int. `AttributeError` on version mismatch + `except Exception` swallows the whole batch. |
| D3.70 | `pipeline/observability/gcp_log_bridge.py:62-72` `export` | Per-record try/except + always returns SUCCESS — Cloud Logging client failures invisible to SDK; no retry. |
| D3.71 | `pipeline/observability/errors.py:15, 44-47` | Traceback truncated at 4 KB; Cloud Trace caps span attrs at ~12 KB total → traceback alone can starve other attrs. |
| D3.72 | `pipeline/observability/decorators.py:75-94` `capture` | `bound.arguments[name]` Pydantic-model arg → `str(v)[:200]` stringifies whole dict; unhelpful spans. |
| D3.73 | `pipeline/upload/upload.py:373-384` `_run_local_server_with_chrome_profile` | `subprocess.Popen([chrome_bin, ...], start_new_session=True)` orphans Chrome on Ctrl-C — stale OAuth tab persists. |
| D3.74 | `pipeline/research/wiki.py:88-114` `_wiki_extract` | Returns first page in `pages.values()` — order non-deterministic on disambiguation. |
| D3.75 | `pipeline/research/wiki.py:62-115` | No retries/backoff. One 503 from Wikimedia kills the dossier. |
| D3.76 | `pipeline/research/youtube.py:497-512` `_assert_safe_to_write` | Symlink/canonicalisation edge cases. No opt-out for legitimate same-resolved-path. |
| D3.77 | `pipeline/footage/footage.py:32-45` `_extract_video_id` | `_VIDEO_ID_RE` doesn't match playlist URLs (`?list=...&index=...` w/o `v=`). Raises ValueError instead of clear error. |
| D3.78 | `pipeline/footage/footage.py:80-87` `_download_source` | Cache key is `video_id` only; 720p vs 1080p of same video share `<id>.mp4` → first cached wins. |
| D3.79 | `pipeline/footage/footage.py:118-184` | When cookies path bad AND browser unreachable, wastes a full yt-dlp retry chain before final anonymous attempt. |
| D3.80 | `pipeline/footage/footage.py:353-355` | concat list uses `intro_path.name` (relative); per `feedback_ffmpeg_concat_absolute_paths`, rule is "always absolute". |
| D3.81 | `pipeline/render/long_form.py:1192-1206` `_render_caption_png` | Line metrics computed eagerly into a list, then iterated; `lh` from zip unused except for `y += lh`. Wasted compute. |
| D3.82 | `pipeline/render/long_form.py:504-514` `_trim_clip_letterbox` | Silently falls through to letterbox chain on ffprobe failure (any reason). User pays 30-40 min of avoidable encode time on aspect-match clips. |
| D3.83 | `pipeline/render/long_form.py:812-880` `build_captions_srt` | Dead code. Only referenced by tests/test_render_long_form.py. ~70 lines. |
| D3.84 | `pipeline/render/long_form.py:671-679` `build_image_panels_video` | xfade `cumtime = seg_paths[0][1] - crossfade_s` can go negative; ffmpeg requires `offset ≥ 0`. |
| D3.85 | `pipeline/render/long_form.py:220` `_aud._synth_kokoro` | Cross-module private-name call. Underscore-prefixed = "internal API"; future refactor breaks this caller. |
| D3.86 | `pipeline/render/shorts.py:1378-1388` `tts_warmup_thread` | Fire-and-forget — never joined. The cold-start tax it hides stays on the critical path of the first TTS call. (image_warmup IS joined at 2068.) |
| D3.87 | `pipeline/render/shorts.py:1539-1549` | `can_threaded_warmup = image_provider in ("sdxl_lightning", "sd_turbo")` — both providers deleted in 2026-05-09 cleanup. Whole warmup block is dead code today. |
| D3.88 | `pipeline/render/shorts.py:2124-2130` `caption_prerender_thread` | Never joined; PIL writes PNG files on daemon thread that compose ffmpeg reads. Partial-write race possible. |
| D3.89 | `pipeline/render/shorts.py:2085-2110` `use_ip` IP-Adapter machinery | Dead — `images.generate(ip_adapter_image=...)` is back-compat no-op on every channel (all cloudrun_flux2_klein). ~30 lines. |
| D3.90 | `pipeline/render/shorts.py:2685-2692` | Footage-bearing renders `return out_path` BEFORE upload block. User-requested `upload_override=True` silently dropped on the hybrid path. |
| D3.91 | `pipeline/render/shorts.py:2671-2680` | `compose_hybrid` doesn't accept `word_caption_font_size=`. Captions-density form override dropped on the hybrid path. |
| D3.92 | `pipeline/render/shorts.py:1808-1820` `mixed_path` | Regenerated every render — no cache check. Voice fingerprint at 1558 doesn't include `music_bed_default`/`audio_music_bed_db` either. |
| D3.93 | `pipeline/render/shorts.py:305-399` `_voice_fingerprint` | Doesn't include music bed or per-render audio cfg. Toggling bed doesn't bust the cached `narration_with_bed.wav`. |
| D3.94 | `pipeline/render/sports_doc.py:885-898` | Always PNG-overlay captions path; 600+ overlays push ffmpeg peak RAM to ~10 GB (per long-form.py's 2026-05-05 lesson). No libass fallback. |
| D3.95 | `pipeline/render/sports_doc.py:178-183` `_align_anchors_to_narration` | Returns FIRST occurrence — multi-chapter docs with reused phrases ("the match") land on the EARLIEST occurrence regardless of chapter. |
| D3.96 | `pipeline/render/sports_doc.py:461` | `prose.split(".")[0]` — breaks on "Dr.", "U.S.", ellipses. |
| D3.97 | `pipeline/render/sports_doc.py:603-619` overlay filenames | `_h_{at_s:.2f}.mp4` — two overlays at 10.123 vs 10.125 → same `.2f` filename → clobber. |
| D3.98 | `pipeline/render/sports_doc.py:872-878` | `list(music_dir.glob("*.wav"))` order is FS-dependent (macOS APFS vs Linux ext4). Non-deterministic music selection. |
| D3.99 | `pipeline/render/footage_only.py:354` | `cache_dir = REPO_ROOT / channel / "footage" / "sources"` — variant niches collide. |
| D3.100 | `pipeline/render/shorts.py:2660` vs `:2671` | `t0 = time.time()` set 11 lines before `compose.compose_hybrid` call; `_record_stage_done("compose", t0)` includes loop time too. Minor latency accuracy bug. |
| D3.101 | `web-next/components/nav/topbar.tsx:42-52` + `:57-58` vs `app/layout.tsx:37` | Hard-coded `<html className="dark">`; topbar toggle calls `classList.toggle("light", next === "light")` adding `.light` but leaving `.dark`. Both classes present, CSS specificity decides. |
| D3.102 | `web-next/app/app/create/page.tsx:1860, 1901, 1926` | Orphan helper components `VoiceTab`, `UseCaseChip`, `FilterRow` defined but never imported. ~120 LOC dead client bundle. |
| D3.103 | `web-next/lib/api.ts:77-104` `pollJob` | Reinvents poll loop without visibility-aware behavior the codebase otherwise enforces. |
| D3.104 | `web-next/app/app/render/[jobId]/page.tsx:69-89` | Two independent loaders race on mount. Initial GET error overwrites successful poll. |
| D3.105 | `web-next/app/app/create/page.tsx:186-189` | `useEffect(() => ... [presetChannel, schema])` references `variant` inside; suppressed with `eslint-disable`. Late variant flip won't trigger step jump. |
| D3.106 | `web-next/lib/use-swr-cache.ts:240-243` | `getCached<T>(key)` called 3× in `useState` initializers; up to 3 localStorage round-trips per hook instance on first render. |
| D3.107 | `web-next/lib/use-visible-poll.ts:44` | `if (cancelled || typeof document !== "undefined" && document.hidden)` — JS precedence works but reader-confusing. Missing parens around the right-side conjunction. |
| D3.108 | `web-next/lib/use-swr-cache.ts:115-124` | `getCached` mutates in-memory cache from localStorage read without `notify()` → subscribers don't repaint with rehydrated value until next poll. |
| D3.109 | `web-next/lib/use-swr-cache.ts:293-307` | `refresh()` bypasses `inflight` Map — concurrent `prime()` + Refresh click → two fetches race for cache slot. |
| D3.110 | `web-next/public/sw.js:129-137` | `trimCache` FIFO by insertion, not LRU — frequently-used keys evicted. |
| D3.111 | `web-next/public/sw.js:140-145` | `BUST_CACHE` deletes only current-version cache; old-version `ytfactory-api-*` caches survive. |
| D3.112 | `web-next/public/sw.js:80-101` | `staleWhileRevalidate` puts full response (including Set-Cookie / auth headers) into Cache API; SKIP_PREFIXES is allowlist by prefix only. |
| D3.113 | `web-next/app/api/[...path]/route.ts:60-67` | Strips inbound `authorization` but not `cookie`. Cloud Run gets BOTH cookie session and IAM Bearer → ambiguous auth contexts. |
| D3.114 | `web-next/next.config.mjs:20-28` | Rewrites for `/agent/*` + `/healthz` don't inject Bearer ID token. If control plane requires auth, these 401 in prod. Inconsistent auth posture vs the route handler. |
| D3.115 | `web-next/components/app/clone-voice-dialog.tsx:91-115` | 200 ms setTimeout reset races with re-open. Re-open within 200 ms wipes the preset. |
| D3.116 | `web-next/app/app/create/clone/page.tsx:55, 70-73` | `let timer; ... if (timer) clearTimeout(timer)` — declared without initializer, cleanup race with async tick. |
| D3.117 | `web-next/components/marketing/stats-band.tsx:43` | `.catch(() => {})` — marketing stats fail silently. |
| D3.118 | 5+ files | `as any` / `// @ts-ignore` / `// @ts-expect-error` casts on `unknown` data — bypasses TypeScript guarantees. |
| D3.119 | 11 `useEffect` suppressions across web-next | `eslint-disable-next-line react-hooks/exhaustive-deps` hides stale-closure bugs. |
| D3.120 | `web-next/components/app/critique-chat-panel.tsx:100-105, 184` | Per-session subscriptions pushed additively to `unsubsRef.current`; Retry button → duplicate onSnapshot. |
| D3.121 | `web-next/app/app/burner-channels/page.tsx:251` + `channels/[channel]/page.tsx:289` | Native `window.prompt()` / `confirm()` — jarring vs shadcn/ui aesthetic. |
| D3.122 | `web-next/components/app/audio-sample-button.tsx:89-93` | `Audio` element src mutation reuses handlers across playback → onpause races mid-swap. |
| D3.123 | `web-next/app/app/telemetry/services-section.tsx:97-107` | Error % colored red/amber/green with no text equivalent — colorblind-hostile. |
| D3.124 | `web-next/app/app/telemetry/errors-section.tsx:86` | `new Date(e.ts * 1000).toLocaleTimeString()` — implicit local TZ; Cloud Run logs are UTC. |
| D3.125 | `web-next/components/nav/sidebar.tsx:56-69` | `api.get<WhoAmI>("/api/auth/whoami")` directly; races AppShellWarmer's prime → duplicate request. |
| D3.126 | `web-next/app/app/burner-channels/page.tsx:930-940` | `useMemo([isObj, state])` — `state` replaced every 2.5 s; memoization is no-op. |
| D3.127 | `web-next/components/app/inspiration-drawer.tsx:47-63` | `fetchedFor.current === channel` gates re-fetch; backend re-fetch invalidation never propagates. |
| D3.128 | `web-next/public/` | No `manifest.json` — incomplete PWA setup (SW alone). |
| D3.129 | `web-next/lib/use-swr-cache.ts:115-124` | No TTL check on `getCached` — stale week-old localStorage entry repaints as "current". |
| D3.130 | `web-next/components/app/clone-voice-dialog.tsx:148-152` | `mr.mimeType` after start may be negotiated codec (`audio/webm;codecs=opus`); server-side MIME validator may only allow bare `audio/webm`. |
| D3.131 | `web-next/lib/use-swr-cache.ts:255-262` | Subscribe listener fires 4 setters even when payload equals current data — 2 wasted renders/sec per subscriber on 2s polls. |
| D3.132 | `web-next/components/app/audio-bus.ts:23-27` | `for (const s of _stoppers)` mid-loop `register/unregister` corrupts Set iteration order; should snapshot. |
| D3.133 | `web-next/app/app/burner-channels/page.tsx:420-432` | `<div role="button" tabIndex={0} ...>` containing nested `<a href>`; SR announces both — confusing semantics. |
| D3.134 | `web-next/components/nav/topbar.tsx:96-105, 114-116` | Search input has no state/handler; Notifications bell has no onClick. UX promises features that don't exist. |
| D3.135 | `web-next/middleware.ts:58-59` | Edge auth gate checks cookie presence, not validity. |
| D3.136 | `web-next/app/api/[...path]/route.ts:39, 46-48` | 2 s `AbortSignal.timeout` for metadata-server token fetch. Cold-load metadata can momentarily 5xx → silent no-auth forwarding. |

---

## 5. Findings by surface

(All Tier-1/2/3 findings above are categorized below by which sub-agent / dive surfaced them. Some are referenced by short ID, the rest are inline. Full context for each is in the audit chat transcript.)

### 5.1 Round 1 — Copilot's 5 recent commits

Commits audited: `2fb4b1f` (watchdog timer + launchd auto-start), `43a5dfe` (telemetry dashboard), `bfbbec1` (critique 404 + audio loudnorm + max_tokens cap + character-lock), `2bc280e` (gitignore coverage_gate runtime), `55d6ee8` (test-coverage glob fix).

24 confirmed bugs spanning all 5 commits. Highlights:
- **T1.3**: telemetry filter `resource.type="cloud_run_revision"` misses Cloud Run JOBs.
- **T1.4**: max_tokens cap propagated only to SDK backends, not CLI subprocess.
- **T1.5**: `_push` orphan-commit failure path.
- **Q2.6**: parse_agent_summary regex single-level brace nesting.
- **Q2.7**: stderr=PIPE drain deadlock.
- **Q2.20-Q2.21**: 55d6ee8's glob fix narrowed `tests/**/*.test.mjs` to `tests/*.test.mjs` — recursive lost.
- **Q2.1**: watchdog docstring still claims multi-runner reaper is missing; 2fb4b1f only added intra-turn timer.

### 5.2 BFF (`web/server.py` + `control/routes/`)

47 BUGs + 8 IMPROVEMENTs. See Tier 1 (T1.20, S1.1-S1.25) and Tier 2 (Q2.33-Q2.45) above. Hottest:
- 5 timing-attack-vulnerable `_require_auth` helpers (S1.16).
- `/admin` double-registration (S1.19).
- Cookie collision OAuth-vs-PIN (S1.20).
- XSS + open-redirect cluster in `oauth_web_routes.py` (S1.2, S1.3).
- Path traversal in `resolve_held_slug` + `_resolve_critique_path` (S1.5, S1.6).
- Cross-tenant data leak in `/api/jobs/*` (S1.7).

### 5.3 Render pipeline (`pipeline/render/`)

38 BUGs + 3 IMPROVEMENTs. See Tier 1 (T1.14-T1.19) and Tier 3 (D3.81-D3.100). Hottest:
- long-form mp4 writes to `shorts/` dir (T1.14).
- `_wav_concat_with_silence` hardcodes 44100 mono (T1.15).
- sports_doc drops clip audio (T1.16).
- sports_doc + footage_only miss narration loudnorm (T1.17).
- sports_doc N-overlay full-timeline re-encode (T1.19).
- 4 renderers diverged on `_load_env` (D3.48).
- IP-Adapter machinery fully dead in shorts.py (D3.89).

### 5.4 LLM dispatcher + Cloud Run services

39 BUGs + 6 IMPROVEMENTs. See Tier 1 (T1.4, T1.13), Tier 2 (Q2.13-Q2.20, Q2.65-Q2.70), Tier 3 (D3.40-D3.51). Hottest:
- max_tokens not passed to CLI subprocess (T1.4).
- 10 of 12 services never init OTel (T1.13).
- IndicParler description field never reaches model — every Hindi render uses hardcoded "calm devotional Indian female voice" (Q2.17).
- Higgs + IndicParler silently drop `speed` parameter (Q2.18).
- F5 + Higgs miss WAV-length validation (Q2.19).
- 429 → bare HTTPError instead of CloudRunUnavailable (Q2.20).
- HF_TOKEN baked into image layer history (S1.11).
- 8 services GCS Fuse weights mount NOT readonly (Q2.65).

### 5.5 Web-next frontend

56 BUGs + 6 IMPROVEMENTs. See Q2.46-Q2.55, D3.101-D3.136. Hottest:
- `lib/api.ts::request` falls through to text() and casts to T (Q2.46).
- ChannelDetailPage no aborted-fetch cancellation (Q2.47).
- BUST_CACHE doesn't clear localStorage SWR cache (Q2.48).
- Critique chat panel duplicate onSnapshot on Retry (Q2.49).
- topbar Search + Notifications: pure decoration (Q2.50).
- render-display.test.mjs imports .ts without TS loader → tests don't run (Q2.51).
- `deriveKindLabel` ignores sports_doc (Q2.52).
- 11+ `eslint-disable-next-line` suppressions across hooks (D3.119).
- `eslint.ignoreDuringBuilds + no tsc in build` (Q2.55).

### 5.6 Tests, scripts, cron, skills, deploy.sh

28 BUGs + 22 IMPROVEMENTs. See Tier 1 (T1.2, T1.11), Tier 2 (Q2.56-Q2.64), Tier 3 (D3.18-D3.41). Hottest:
- `upload-next.plist` invokes nonexistent path (T1.2).
- cloud-snapshot + state-sync + upload-next plists have no PATH env (D3.18).
- `render-worker-v2/deploy.sh --set-secrets` destructive (T1.11).
- `clone-video-worker` + `weights-staging` deploy.sh leak keys as plaintext env (S1.15).
- `gcs_lifecycle.json` overlapping rules delete thumbnails at age 1d (D3.25).
- 8+ `assertIn(<exact prompt phrase>, prompt)` brittle tests (D3.34).
- `lint_skill_md.py` doesn't validate cross-skill references (D3.35).
- 3 skills overlap on cosmos triggers (D3.36).
- 2 skills overlap on "make me an AITA short" (D3.37).
- `Auto-invoked` claims without hooks (D3.38).

### 5.7 Security + auth

25 BUGs + 10 IMPROVEMENTs. See S1.1-S1.25 above. Most actionable single fix: verify the Google ID token signature in `pipeline/auth/identity.py::_decode_id_token` (S1.1).

### 5.8 Observability + research + upload + footage

64 BUGs + 7 IMPROVEMENTs. Largest single haul. See Tier 1 (T1.6-T1.10, T1.18, T1.21), Tier 2 (Q2.8-Q2.12, Q2.26-Q2.32), Tier 3 (D3.42-D3.80). Hottest:
- metric_attrs missing channel/slug (T1.12).
- YouTube pagination silently truncates on HttpError (T1.9).
- `_build_youtube` swallows RefreshTokenLost (T1.8).
- `compute_throttled_publish_at` cloud-blind (T1.6).
- `list_sibling_accounts` cloud-blind (T1.7).
- `play_view` headless=False default (T1.21).
- `youtube_upload` doesn't detect 403 quotaExceeded (T1.10).
- footage `_download_source` no concurrent-download lock (T1.18).
- yt_dlp_cloudrun `Path("/").glob(...)` (Q2.31).
- Secret Manager `add_secret_version` on every refresh (Q2.26).

### 5.9 Channel YAML, plists, docs, manifests (my own dives)

~18 BUGs + ~8 IMPROVEMENTs. See D3.1-D3.20 and:
- `pipeline/channels/scrollpulse.yaml` missing entirely; channel `in_rotation: true` (T1.1).
- 53 broken MEMORY.md links to `<channel>/learnings/` (D3.5).
- 95 pipeline modules have no test file (D3.17).
- 1724-line `_make_short_impl` (D3.11).
- `tts-runner` SA used cross-service (S1.21).
- 3 services without `--service-account=` (S1.22).
- @traced doesn't handle async def (Q2.10).

---

## 6. Recurring patterns (class-of-bugs)

These patterns surfaced repeatedly across surfaces. Each one is a single root cause + a fix recipe.

### P1. "Patch the SDK paths, miss the CLI path"

Examples: max_tokens cap (`bfbbec1` only patched Azure + Anthropic SDK, missed `_call_claude_cli_subprocess`); watchdog timer (`2fb4b1f` only added intra-turn timer, missed multi-runner reaper documented in docstring); narration loudnorm (`bfbbec1` only patched long_form.py, missed sports_doc.py + footage_only.py).

**Fix:** when fixing a class of bug, grep for every call site of the affected primitive — fix everywhere or document why one path is intentionally exempt.

### P2. Filter strings encoding contracts that no test exercises

Examples: `cloud_log_reader.py` `resource.type="cloud_run_revision"` (misses Cloud Run JOBs); `pipeline/channels.yaml` config_yaml paths (missing scrollpulse.yaml); plist invocation paths (upload-next references nonexistent script); `_M2M_PATH_PREFIXES` (omits burner channels); add_otel_copy.sh awk pattern (doesn't match repo-root-context COPY).

**Fix:** for every producer/consumer pair that encodes a contract in a literal string, write a test that round-trips a real value through both sides (not just `assertIn(<literal>, filter_)` on the producer).

### P3. `check=True` subprocess + broad outer `except` = blackholed diagnostics

Examples: `runner.py` `_push` (T1.5), `runner.py` `git add -A` calls (Q2.3), `runner.py` `_stage_and_commit` (`check=True` on `git add` without `_git_failure_diag`). The pattern was identified and a helper (`_git_failure_diag`) was built — but applied to only 2 of 5 git call sites.

**Fix:** when adding a diagnostic helper, sweep every subprocess call site in the same module.

### P4. In-process state pretending to be durable

Examples: `_PENDING_STATE` OAuth map (S1.14), `JOBS` dict, telemetry shadow buffer (Q2.11), throttle scan on laptop FS only (T1.6), `AGENT_TOKEN` cached at module import (S1.13), `_DISABLED_REASON` never auto-recovers (cloud_log_reader).

**Fix:** for every module-global dict/state, document the lifetime (per-process / per-request / per-deploy) and check if the dispatch decisions depending on it work in the worst case (multi-replica, cold restart).

### P5. `from __future__ import annotations` hiding name typos

Example: `InMemoryLogExporter` annotation in `exporters.py:219` for a name never imported (Q2.8). PEP 563 keeps annotations as strings → `dataclass` doesn't evaluate; `typing.get_type_hints` explodes.

**Fix:** add `mypy --strict` to CI, or call `get_type_hints` smoke during test collection.

### P6. Per-service copies maintained in lockstep with byte-identical tests, but each copy never imported

Example: `cloud/web-server/otel_init.py` + `cloud/web-server/cloud_run_json_exporter.py` baked into the image via `add_otel_copy.sh`; `web/server.py` imports `pipeline.observability` instead. Lockstep test pins byte-identity, so developers spend time keeping dead copies in sync.

**Fix:** when a service has a slim runtime importing from the laptop tree, the per-service copies should NOT be COPY'd. Either remove the sync rule for the slim service, or convert the service to use the per-service copy.

### P7. Tests pinning phrasing, not behaviour

Examples: 8+ `assertIn(<exact prompt fragment>, prompt)` across tests/test_pipeline_prompts.py / test_llm_*.py (D3.34); `_severity_map_identical` substring check (Q2.62); `test_filter_includes_jsonpayload_field_and_timestamp` (Q2.64); `test_init_off_cloud_run_does_not_raise` (Q2.63); `test_template_carves_out_landscape_panels` substring assertion.

**Fix:** assert on behaviour against a mocked LLM / live filter execution / actual API call. Substring assertions on templates are negative-confidence — they fail on tone-down + pass on weakened phrasing.

### P8. Tests that skip-silently on missing attributes

Examples: `tests/test_llm_script_lint.py` skipTest("capitalization helper not present"), `tests/test_llm_cast_router.py` skipTest("witness-beat guard not present"), 3+ tests in `tests/test_pipeline_llm.py`, `tests/test_layout_parity.py::ChannelConfigYamlPresentTest` self-skip on cloud-cutover laptop.

**Fix:** convert `hasattr(mod, "_helper") or skipTest()` to `assert hasattr(mod, "_helper")`. If a feature is optional, the test should pin its presence as required and conditionally skip only on EXPLICIT opt-out env.

### P9. Auth posture inconsistency across routers

Examples: 5 `_require_auth` helpers with non-constant-time comparison while the canonical `control/core/auth.py` uses `compare_digest`; `/api/auth/whoami` registered both in `web/server.py` and `auth_pin.py`; PIN cookie defaults `Secure=False` while OAuth cookie defaults `Secure=True`; M2M routes inconsistent on `_M2M_PATH_PREFIXES` membership; 6+ `K_SERVICE` blanket bypasses on browser-reachable routes.

**Fix:** one canonical `require_agent` / `require_user` dependency, used everywhere via `Depends(...)`. Forbid hand-rolled token compare.

### P10. Doc-vs-reality drift, especially in CLAUDE.md / MEMORY.md

Examples: CLAUDE.md says `sportstoriesanimated` (D3.1); CLAUDE.md says hindutavaanimated uses `indicparler` but YAML has `indicf5` (D3.2); CLAUDE.md says `--min-instances=1` but deploy.sh has `0` (D3.3); 53 MEMORY.md links to non-existent paths (D3.5); `docs/burner_channels.md` references moved modules (D3.6); cast.py docstring claims YAML fallback that no longer works (D3.15).

**Fix:** add a doc-link checker to CI (`grep` for `/Users/rohit/ytFactory/...` paths in MEMORY.md, assert each exists; grep for module paths in docs/, assert importable). One-time cleanup of CLAUDE.md against `pipeline/channels.yaml` source of truth.

### P11. Hardcoded user paths

Examples: `pipeline/quality/evals.py:32` `EVALS_ROOT = Path("/Users/rohit/evals")` (D3.7); `pipeline/cross_engage/create_burner_channel.py:266` `/Users/rohit/Library/...` (D3.8); 9+ help-text references to `/Users/rohit/ytFactory`.

**Fix:** env-config every absolute path; never `Path.home()` in tests; project-relative everywhere.

### P12. Cloud-blind code that worked on laptop

Examples: `compute_throttled_publish_at` (T1.6), `list_sibling_accounts` (T1.7), `discover_existing_uploads` (uses `_project_root()` = `/app/pipeline/` on cloud), 3 sites that shell out to `gcloud` (Q2.41), `youtube_auth_start` opens localhost browser (T1.20), `play_view` `headless=False` default (T1.21).

**Fix:** for every laptop-FS-dependent helper, write a "GCS path" variant and dispatch by `YTFACTORY_STATE_BUCKET`. Tests that monkeypatch the bucket env should cover the cloud path explicitly.

### P13. Multi-process state-of-the-art leaks

Examples: shadow buffer per-process (Q2.11), OAuth `_PENDING_STATE` per-process (S1.14), AGENT_TOKEN module-import snapshot (S1.13).

**Fix:** when Cloud Run autoscales >1 instance, every dict that "remembers something" without a backing store is broken. Move to Firestore / Memorystore / sticky sessions.

---

## 7. Recommended fix order

The 21 Tier-1 items + 25 security items are the load-bearing surface. Fix in this order:

### Day 1 (production blockers)

1. T1.1 — `pipeline/channels/scrollpulse.yaml` — either create the YAML or remove the channel from `pipeline/channels.yaml`.
2. T1.2 — fix `com.ytfactory.upload-next.plist` path to `scripts/ops/upload_next.py`.
3. T1.3 — `cloud_log_reader.py` filter: add `cloud_run_job` to the resource.type clause.
4. T1.13 — wire `otel_init.init()` into all 10 missing services' `server.py`.
5. T1.5 + Q2.3 — runner.py: wrap `_push` and the two `git add -A` calls with `_git_failure_diag` + a real recovery path (reset HEAD on push failure).
6. S1.16 — replace 5 `_require_auth` helpers with `control/core/auth.require_agent` using `compare_digest`.
7. S1.1 — verify Google ID token signature in `_decode_id_token`.

### Day 2 (silent-failure cluster)

8. T1.4 — pass max_tokens to `_call_claude_cli_subprocess` (or document that the CLI backend doesn't support it and warn at config-validate time).
9. T1.6 + T1.7 + T1.8 — three cloud-blind helpers: `compute_throttled_publish_at`, `list_sibling_accounts`, `_build_youtube` catch. Bucket-aware paths.
10. T1.9 — retry/backoff in `_fetch_uploads_playlist` and `_fetch_videos_batch`.
11. T1.10 — typed `QuotaExceededError` in `youtube_upload`; caller handles "back off until tomorrow".
12. Q2.17 — fix `cloud/tts-indicparler/server.py` to read `req.description` from the client payload.
13. T1.11 — change `--set-secrets` to `--update-secrets` in `cloud/render-worker-v2/deploy.sh`.
14. Q2.65 — add `,readonly=true` to all 8 GCS Fuse weights mounts.

### Day 3 (XSS / open-redirect / SSRF)

15. S1.2 + S1.3 — `oauth_web_routes.py`: escape `account` / `error_msg` / `return_to` via `html.escape`; allowlist `return_to` against same-origin only.
16. S1.4 — `clone_video_routes.py POST /api/clone_video`: scheme + host allowlist; `Depends(require_pin)`.
17. S1.5 + S1.6 — `render_routes.py resolve_held_slug` + `_resolve_critique_path`: validate `safe_join(base, channel, slug)` with `is_relative_to(base)`.
18. S1.7 — add `owner_uid` filter to `/api/jobs/*` endpoints.
19. S1.8 — `_public_base_url` host allowlist.
20. S1.19 + S1.20 — remove duplicate `/admin` route; pick one cookie name per auth flow.
21. S1.11 — convert `ARG HF_TOKEN` to BuildKit `--mount=type=secret`.

### Day 4 (render-pipeline correctness)

22. T1.14 — long_form.py: write to `<channel>/long_form/` not `shorts/`.
23. T1.15 — silence at canonical TTS sample rate / channel count, or `aresample` chunks first.
24. T1.16 — sports_doc final mux: route `[0:a]` (clip audio) into the amix.
25. T1.17 — apply narration loudnorm to sports_doc + footage_only final mux.
26. T1.18 — footage `_download_source`: filelock + tempfile-then-rename.
27. T1.19 — sports_doc overlays: assemble in a single ffmpeg invocation (filter_complex), not N re-encodes.

### Week 2 (observability + tests)

28. T1.12 — metric_attrs to include channel/slug/job_id everywhere `counter().add` / `histogram().record` fires.
29. Q2.10 — `@traced` async-aware wrapper.
30. Q2.11 — `_CARRY_KEYS` to include render_kind, render_mode, run_id, user.
31. Q2.57-Q2.59 — fix the 3 skip-silently test patterns and the `test_layout_parity` self-skip.
32. D3.34 — replace 8+ prompt-substring tests with mocked-LLM behaviour tests.

### Week 3+ (refactor)

33. D3.11 — split `_make_short_impl` (1724 lines) into stage helpers.
34. D3.12 — split `burner_engage.run` (717 lines).
35. D3.48 — extract single `_load_env` shared helper.
36. D3.5 — fix MEMORY.md link rot (one-time sed across all 53 links).
37. D3.1-D3.4 — CLAUDE.md doc drift cleanup against `pipeline/channels.yaml` source of truth.

---

## Appendix A — Verification commands

```bash
# T1.1 — verify scrollpulse YAML is actually missing:
ls pipeline/channels/scrollpulse.yaml || echo MISSING

# Force-load all channels and find which ones fail:
.venv/bin/python -c "
from pipeline.channels import all_channels
from pathlib import Path
for c in all_channels():
    p = Path(c.channel_yaml_path())
    print(c.slug, 'exists=', p.exists(), 'in_rotation=', c.in_rotation)
"

# T1.2 — verify upload-next plist target:
grep -E 'scripts/.*\.py' control/com.ytfactory.upload-next.plist
ls scripts/upload_next.py scripts/ops/upload_next.py

# T1.3 — verify cloud_log_reader filter excludes render-worker:
grep -n 'resource.type' pipeline/observability/cloud_log_reader.py
grep -E 'JOB=|jobs deploy' cloud/render-worker-v2/deploy.sh

# T1.4 — verify CLI backend doesn't pass max_tokens:
grep -A5 '_call_claude_cli_subprocess' pipeline/llm/cli.py | grep -iE 'max.token'

# T1.13 — verify which services init OTel:
grep -l 'otel_init\|_otel_init' cloud/*/server.py cloud/*/entrypoint.py 2>/dev/null

# D3.5 — count broken MEMORY.md links:
grep -oE '/Users/rohit/ytFactory/[a-z]+/learnings/[a-z_]+\.md' \
    ~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md \
    | while read p; do [ -f "$p" ] || echo "MISSING: $p"; done | wc -l

# D3.18 — plist PATH coverage:
for p in control/com.ytfactory.*.plist; do
  echo "$p $(grep -c 'PATH' $p)"
done
```

## Appendix B — Audit methodology

Sub-agents launched in parallel, each with a tight scope and a +1/+1.5 scoring brief. Each agent ran for 5-12 minutes against the live working tree (HEAD = `2fb4b1f`, with uncommitted change `M pipeline/critique/runner.py`). No code was modified during the audit.

Agents:
1. `Deep dive web/server BFF` — 47 BUGs / 8 IMPs
2. `Deep dive web-next frontend` — 56 BUGs / 6 IMPs
3. `Deep dive LLM + cloud services` — 39 BUGs / 6 IMPs
4. `Deep dive tests + scripts + cron` — 28 BUGs / 22 IMPs
5. `Deep dive pipeline/render/*` — 38 BUGs / 3 IMPs
6. `Security + auth audit` — 25 BUGs / 10 IMPs
7. `Last-pass — observability + research + uploads + footage` — 64 BUGs / 7 IMPs

Plus my own surface-mining: missing YAMLs, doc drift, plist hygiene, Cloud Run SA posture.

Spot-verification commands ran inline in this transcript:
- `_parse_tool_use_chip` resolved (false alarm in Round 1)
- `resource.type="cloud_run_revision"` confirmed against `cloud/render-worker-v2/deploy.sh:23 JOB="..."` and `:52 gcloud run jobs deploy`
- scrollpulse YAML absence confirmed via `.venv/bin/python -c "from pipeline.channels import all_channels..."` raising FileNotFoundError on the channel.
- 53 MEMORY.md link failures confirmed via the grep+test loop in Appendix A.
- All 18 `cloud/<svc>/deploy.sh` source `auth_setup.sh` (CLAUDE.md P10 holds).
- Channel YAML key drift confirmed for `image_width/height`, `image_style_prefix`, `opening_image_directives`, `script_check_strict`, `tts_ref_text`, `character_description`.
