# Cross-engage from the cloud — v2 (2026-05-10)

When the user clicks **Cross-engage** on a burner row at
`https://ytfactory-web-next-…/app/burner-channels`, six things have
to line up correctly between Cloud Run and the laptop. This doc is
the canonical map of that path. It supersedes the
"`burner_engage` IS implemented" note in
[`docs/full_cloud_cutover_2026_05_09.md`](full_cloud_cutover_2026_05_09.md)
§ Phase 4 — that line was true for the laptop spawn but false for the
cloud spawn, and the gap surfaced as a stuck `404` storm on
`/api/burner_channels/<slug>/engage` until the fixes below shipped.

The legacy laptop-only path (single-machine subprocess.Popen + on-disk
state) still works in dev when `K_SERVICE` is unset; this doc is about
the production path.

## Architecture

```
Browser (web-next)               Cloud Run                 Laptop agent
───────────────────              ─────────                 ───────────
POST /api/burner_channels/         │
     <slug>/engage         ──────► │ start_engage()
                                   │   if K_SERVICE:
                                   │     Firestore enqueue ─────────► long-poll lease
                                   │   else: subprocess.Popen          ▼
                                   │                                _exec_burner_engage
                                   │                                   spawns
                                   │                                   `python -m
                                   │                                    pipeline.cross_engage
                                   │                                   .burner_engage run <slug>`
                                   │                                   (start_new_session)
                                   │                                   ▼
                                   │                                fire-and-forget
                                   │                                ack OK to cloud
                                   │                                worker keeps running
                                   │                                ▼
GET /api/burner_channels/          │ poll_engage()       ◄───── _save_state()
     <slug>/engage         ◄────── │   read_state(slug)         GCS push every tick
     (every 2.5s)                  │   (cloud reads from GCS)
                                                                ▼
                                                         brand-switch (avatar→
                                                         Switch account→burner)
                                                         verify via /account
                                                         (NOT studio.youtube.com)
                                                                ▼
                                                         Phase-1: like+sub each
                                                         video on /shorts/<vid>
                                                                ▼
                                                         Phase-2: tab-cycle for
                                                         watch-time (forever)

Stop button ──► request_stop()
                    │
                    └──► local /tmp sentinel + GCS sentinel
                             │
                             └─► worker checks both each tick → graceful exit
```

## Six pieces (and why each was needed)

### 1. `web/server.py:auth_middleware` — IAM bypass for `/agent/*`

The middleware gates `/agent/*` on
`Authorization: Bearer <YTFACTORY_AGENT_TOKEN>`. The laptop agent
sends a `gcloud print-identity-token` OIDC instead. Cloud Run's
Google Frontend already validated that OIDC against the
`run.invoker` binding before the request reached the app — and it
consumed the Authorization header. So the app middleware had to
trust IAM rather than re-require its own bearer.

```python
if any(path.startswith(p) for p in _M2M_PATH_PREFIXES):
    if os.environ.get("K_SERVICE"):    # ← added 2026-05-10
        return await call_next(request)
    if AGENT_TOKEN is None: ...
```

This mirrors the same `K_SERVICE` bypass that
[`control/core/auth.py:require_agent`](../control/core/auth.py)
already had per the cloud-cutover doc — the bug was that the outer
middleware short-circuits *before* the per-router dependency runs,
so the bypass had to live in both layers.

### 2. `control/routes/burner_routes.py:start_engage` — enqueue, don't spawn

On Cloud Run there's no Chrome binary, no macOS Keychain, no display.
The pre-fix endpoint did
`subprocess.Popen([..., "pipeline.cross_engage.burner_engage", "run", slug])`
which crashed silently on the cloud container. UI poll then 404'd
forever (no state file ever written).

Post-fix:

```python
if os.environ.get("K_SERVICE"):
    burner_engage.clear_stop_sentinel(slug)   # in case a stale sentinel exists
    task = TaskEnvelope(
        task_id=new_task_id(),
        job_id=f"burner-{slug}-{int(time.time())}",
        kind=TaskKind.BURNER_ENGAGE,
        payload={"slug": slug},
        max_attempts=1,
    )
    get_queue().enqueue(task)
    return {"started": True, "task_id": ..., "agent_required": True}
```

Local-dev path (`K_SERVICE` unset) keeps the legacy `subprocess.Popen`
flow so a single-machine laptop workflow still works.

### 3. `pipeline/cross_engage/burner_engage.py` — GCS state mirror

The worker writes its state to `data/burner_engage/<slug>.json` every
tick (`_save_state`). Pre-fix that was laptop-only — the cloud
control plane polled its own (empty) disk on `GET /api/burner_channels/<slug>/engage`.

Post-fix `_save_state` mirrors to
`gs://$YTFACTORY_STATE_BUCKET/burner_engage/<slug>.json` after the
local atomic write. `read_state` prefers GCS when the bucket is
configured, falls back to local disk otherwise. 5s in-process TTL
cache absorbs the 2.5s UI poll without hammering GCS.

```python
def _save_state(state):
    # … existing local atomic write …
    _push_state_gcs(state.slug, payload)   # ← added 2026-05-10
```

This is a **reusable pattern** for any laptop-side worker the cloud
UI must observe: write locally first, mirror to GCS, cloud reads
from GCS. Generalising to other Chrome-bound flows
(`playwright_upload`) when those land.

### 4. `pipeline/cross_engage/burner_engage.py` — GCS stop sentinel

Stop also flows through GCS. `request_stop` writes both a local
`/tmp/burner_engage_<slug>.stop` sentinel AND
`gs://$YTFACTORY_STATE_BUCKET/burner_engage/<slug>.stop`.
`_maybe_stop` checks both each tick (cached 2s to avoid hammering).
The cloud POST to `/api/burner_channels/<slug>/engage/stop` ends up
writing the GCS sentinel only; the worker's next tick (within ~2s of
its last `_bump_action`) sees it and exits gracefully.

`clear_stop_sentinel(slug)` is called both at the start of every
worker run AND from the cloud-enqueue path before scheduling a new
task — otherwise a leftover sentinel from a previous Stop click
would make the freshly-spawned worker self-terminate on its first
tick.

### 5. `pipeline/laptop_agent.py:_exec_burner_engage` — fire-and-forget

The pre-fix executor used `subprocess.run(timeout=900)` which would
have killed any worker after 15 minutes and then ack'd the task
failed. But the engage worker is intentionally a long-lived loop
(Phase 1: like + sub every video; Phase 2: tab-cycle for hours of
watch-time). The 15-min cap was wrong for this task kind.

Post-fix:

```python
proc = subprocess.Popen(
    cmd,
    cwd=str(repo_root),
    stdin=subprocess.DEVNULL,
    stdout=log_fp,
    stderr=subprocess.STDOUT,
    env=env,
    start_new_session=True,
)
return True, None, None   # ack immediately
```

The worker outlives the agent (so a launchd restart of the agent
doesn't kill an in-flight engage loop). The cloud queue marks the
task DONE in seconds; per-burner progress is tracked via the
GCS-backed state file (piece 3), not via task status.

This is the **right pattern for any long-lived Chrome task kind on
the laptop**. New task kinds that block for >5 min should follow
this pattern instead of `subprocess.run(timeout=…)`.

### 6. `pipeline/laptop_agent.py` — env injection for spawned worker

The spawned worker subprocess inherits env from the agent process,
which inherits from launchd. The launchd plist doesn't export
`YTFACTORY_STATE_BUCKET`. So the worker — which now reads the
catalog from GCS via piece 7 below — would have got an empty env and
fallen back to disk. Disk has nothing (post-cloud-cutover the
`<channel>/uploads/` dirs are empty on the laptop).

Defaulted in code (`env.setdefault(...)`) so future channels' env
variables don't get silently lost when the launchd plist isn't
edited:

```python
env.setdefault("YTFACTORY_STATE_BUCKET", "ytfactory-prod-v2-state")
env.setdefault("GOOGLE_CLOUD_PROJECT", "ytfactory-prod-v2")
```

### 7. `pipeline/utils/catalog.py` — GCS-aware reads

`list_catalog()` and `catalog_count()` previously read upload records
from `<repo>/<channel>/uploads/*.json`. The 2026-05-09 cutover moved
those records to `gs://ytfactory-prod-v2-state/<channel>/uploads/`
(and the niche-nested layouts under
`<channel>/<niche>/uploads/`). Pre-fix
`list_catalog()` returned 0 entries on cloud and on any laptop
worker spawned by the agent. Worker exited with `"catalog empty
(no shipped videos to engage with)"`.

Post-fix: when `YTFACTORY_STATE_BUCKET` is set, both functions list
GCS first (5s in-process TTL cache, falls back to disk on GCS error
or empty channel). Skips the `_pending` and `.x.json` X-poster
sidecars per the same convention as the disk path.

### 8. `pipeline/cross_engage/burner_engage.py:run` — brand-account switch

One Google account hosts several brand burners
(`rsinghtomar54@gmail.com` hosts `afddfdf`, `ajfsbqe`, `axkxlwv`,
`cbxqzlyivk`). YouTube tracks the active brand via cookies; without
an explicit switch, every Like / Subscribe lands on whichever brand
was last selected in that profile.

Live observation 2026-05-10 22:18: pre-switch run engaged as
`UCxClGoGmVMnkC8ytTOMvQKQ` (a sibling burner) instead of the requested
`UCwKxho5ZLIgoH52eEun8T7g` (`afddfdf`). 11 likes and 1 subscribe
were misattributed before the worker hit Stop.

Post-fix: `burner_engage.run` calls
[`switch_to_burner_brand`](../pipeline/cross_engage/cross_engage_burner_attached.py)
right after Chrome attached, before the engage loop. Hard-fails
(returns 1, phase=`failed`) if the switch can't be verified.

### 9. `/shorts/<vid>` URL form — auto-loop watch-time

Engagement tabs use `https://www.youtube.com/shorts/<video_id>` not
`/watch?v=<video_id>`. The Shorts player auto-loops the video
indefinitely in the same tab. The Phase-2 tab-cycle then accumulates
compounding watch-time across N tabs, vs the regular `/watch` player
that stops at video end and idles.

`vs.url` in state stays the canonical `/watch?v=` link (used for UI
hover / share); only the `page.goto` call rewrites to `/shorts/`.
Falls back to `vs.url` if the URL already contains `/shorts/` or
`video_id` is missing.

### 10. `_read_active_uc` — `/account` fallback for the brand-switch verifier

The brand-switch verifier in
[`cross_engage_burner_attached.py:_read_active_uc`](../pipeline/cross_engage/cross_engage_burner_attached.py)
originally probed `studio.youtube.com` for a `/channel/UC…` redirect.
Headless test 2026-05-10 22:38 surfaced that Studio loads
("YouTube Creator Studio" title) but never initialises the channel
context in headless mode — the SPA returns `None` for the active UC
even *before* the brand-switch click, so it's not a click-failure.

Fixed by adding a fallback that hits
`https://www.youtube.com/account` and counts `/channel/(UC…)` matches
in the HTML. The mode of those matches is the active brand
(www.youtube.com/account is the user's account page; every link on it
points at the active brand). Works in both headless AND windowed mode
— so it also robustifies the windowed path against Studio first-run
modals or transient redirects.

## Headless mode — partial support, opt-in only

`burner_engage.run(slug, headless=True)` plumbs through to
`launch_chrome_for(headless=True)` which adds `--headless=new` +
`--window-size=1366,900`. Tested 2026-05-10:

| Step | Headless |
|---|---|
| Chrome.app launch with `--headless=new` | ✅ |
| macOS Keychain cookie decryption | ✅ |
| Profile pinning | ✅ |
| Avatar menu + Switch-account submenu | ✅ |
| `switch_to_burner_brand` (with `/account` fallback) | ✅ |
| Open `/shorts/<vid>` tabs | ✅ |
| Per-video Like / Subscribe click | ❌ silently no-ops on Shorts player |

Root cause of the remaining failure: `_probe_like` /
`_probe_subscribe` selectors target the `/watch` player markup
(`button#segmented-like-button` style). The Shorts player has
different DOM. Windowed mode masks this through animation/timing
leniency the headless renderer skips.

**Decision (2026-05-10, user):** stay windowed-only. Headless
plumbing remains in the codebase as opt-in (`--headless` CLI flag).
Re-evaluate when we either (a) want hybrid mode (windowed for the
Like+Subscribe phase, headless for the long-running watch-time loop)
or (b) fix the Shorts-player Like/Subscribe selectors.

## Smoke test recipe

After any change to the cloud cross-engage path:

```bash
# 1. Reload laptop agent so it picks up code changes.
#    NB: this is the RESTART recipe. To permanently STOP the agent
#    (e.g. it's spawning Chrome during unrelated work), see
#    docs/laptop_agent_cloud_contract.md § "Operator kill-switch —
#    how to stop the agent" — that path uses `launchctl disable`
#    so login/reboot doesn't reload it.
launchctl unload ~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist
launchctl load   ~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist
sleep 8

# 2. Verify agent heartbeat → 200
TOK=$(gcloud auth print-identity-token)
curl -sS -i -X POST -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -d '{"resources":{"agent_id":"diag","caps":["burner_engage"]}}' \
  https://ytfactory-web-7hwnzw7lya-as.a.run.app/agent/heartbeat

# 3. Click Cross-engage on any burner row in /app/burner-channels
#    (or enqueue a fresh task directly via Firestore — see plan.md)

# 4. Within 30s of clicking, the GCS state file must exist
gsutil ls gs://ytfactory-prod-v2-state/burner_engage/

# 5. Drawer should populate with brand-switch confirmation, then
#    per-video Liked/Subscribed checkmarks
```

The laptop agent log at `/tmp/ytfactory-laptop-agent.err` should show
`executing task <id> kind=burner_engage` followed by
`spawned burner_engage worker pid=<N>`. The worker log at
`<repo>/data/burner_engage/logs/<slug>.log` should show the
brand-switch verify line `✓ switched to <slug> (UC…)` before any
`liked …` lines.

## Engagement modes (2026-05-11)

Cross-engage isn't one-size-fits-all anymore — the burner row exposes
**two controls**: a dedicated **Subscribe** button (one-click hot
path → `subscribe_only`, the most-used mode) plus a **Cross-engage ▾**
dropdown for the three higher-intensity modes. Each mode picks a
different mix of actions per video tab:

| Mode | UI surface | Like | Subscribe | Watch loop | Comments | Exits when |
|---|---|---|---|---|---|---|
| `subscribe_only` | **Subscribe** button (direct) | — | ✓ once/channel | — | — | all channels subbed |
| `like_subscribe` | Cross-engage ▾ menu | ✓ once/video | ✓ once/channel | — | — | all videos liked + all channels subbed |
| `like_subscribe_view` (default) | Cross-engage ▾ menu | ✓ once/video | ✓ once/channel | ✓ forever | — | Stop button |
| `complete` | Cross-engage ▾ menu | ✓ once/video | ✓ once/channel | ✓ forever | ✓ random 30%, max 15/run, ≥270s apart | Stop button |

The split is intentional: the operator was repeatedly opening the
dropdown just to pick `subscribe_only`, so it got promoted to a
direct button. `subscribe_only` stays in `ENGAGE_MODES` and the
server still validates it — the UI just filters it out of the
DropdownMenu items via
`ENGAGE_MODES.filter((m) => m !== "subscribe_only").map(...)`.

> **Drawer-row interaction (2026-05-11):** Subscribe, every
> Cross-engage option, View live, Last run, **Stop**, AND clicking
> the row body all open the right-rail engage drawer via the
> parent's `onOpen()` callback. See
> [`docs/web_next_clickable_row_pattern.md`](web_next_clickable_row_pattern.md)
> for the rule + the HTML gotcha (`<div role="button">` not
> `<button>` because the row body has a nested `<a>` to the YouTube
> channel).

The mode is selected in three layers — make sure each stays in lock-step:

1. **UI** ([`web-next/app/app/burner-channels/page.tsx`](../web-next/app/app/burner-channels/page.tsx) +
   [`web-next/lib/api.ts`](../web-next/lib/api.ts)) — the
   `ENGAGE_MODES` tuple, `ENGAGE_MODE_LABELS`, and
   `ENGAGE_MODE_DESCRIPTIONS` exports drive the shadcn DropdownMenu
   AND the Subscribe direct-button. The Subscribe button hardcodes
   `start("subscribe_only")`; the dropdown filters that mode out.
2. **API** ([`control/routes/burner_routes.py:start_engage`](../control/routes/burner_routes.py)) —
   POST body `{"mode": "..."}` is validated against
   `burner_engage.ALL_MODES` and embedded in `TaskEnvelope.payload.mode`.
3. **Worker** ([`pipeline/cross_engage/burner_engage.py:run`](../pipeline/cross_engage/burner_engage.py)) —
   `mode` arg branches the cycle logic via three booleans
   (`do_like` / `do_watch_loop` / `do_comments`).

Adding a fifth mode = touch all three. There is no central enum the
UI imports from Python; the four-string tuple is duplicated. If we
add a 5th and only update two layers, the old one quietly becomes
"unknown mode" (HTTP 400 from the API) on the missed layer. Cheap
acceptable cost for now (UI is TS, backend is Python; sharing types
across the boundary would mean codegen).

### Bounded vs infinite modes

`subscribe_only` and `like_subscribe` exit when no tab still needs
engagement work (or every tab's `like_attempts` / `sub_attempts` has
maxed at 3). The cycle loop checks at the top of each tick:

```python
if not needs_work and not do_watch_loop:
    state.phase = "stopped"
    _bump_action(state, f"all engagement done for mode={mode} — exiting clean")
    break
```

`like_subscribe_view` and `complete` skip that exit and keep cycling
forever for watch-time accumulation; only the Stop button (or
`request_stop()` CLI) terminates them.

### Comments — LLM-generated, capped (`MODE_COMPLETE` only)

Comment volume + pacing was tuned to stay under YouTube's
shadow-ban radar for a fresh-trust burner account:

- **Subset:** `COMMENT_SUBSET_FRACTION = 0.30` of the catalog,
  randomly picked at run start (stable across cycle re-visits so
  the choice doesn't drift if random-pick re-runs).
- **Hard cap:** `MAX_COMMENTS_PER_RUN = 15` per worker run
  (30% × 58 ≈ 17 → capped to 15).
- **Spacing:** `MIN_SECONDS_BETWEEN_COMMENTS = 270` (~4.5 min) →
  ~13 comments/hr maximum, comfortably under the
  "auto-flagged" threshold.

Comment text is generated per-video via
[`pipeline.llm.cli.call_llm`](../pipeline/llm/cli.py) (Azure GPT
on cloud, Claude on laptop — same dispatcher every other LLM call
uses). 5-15 word casual comment based on video title; the
fallback pool (`_COMMENT_FALLBACK_POOL`) of 15 generic phrases
fires when the LLM call errors out (network blip, quota hit) so
the worker never blocks on the LLM.

### Live verification

Validated 2026-05-11 with a `subscribe_only` smoke run on
`zgsbhqszdheo`: cycle log shows
`cycling 58 tabs (mode=subscribe_only, do_like=False, do_watch_loop=False, do_comments=False)…`
followed by `subscribed to historyrecapped` /
`subscribed to mystoriesanimated` lines, **zero `liked X` lines**,
and the cycle will exit clean when all 7 production channels are
subbed.

### See also

- [`docs/all_tabs_cycle_pattern.md`](all_tabs_cycle_pattern.md) — the
  Phase-0+Phase-1 architecture all engagement modes ride on.
- [`docs/many_chrome_tabs.md`](many_chrome_tabs.md) — the Chrome flags
  + page.goto policy that make 50+ simultaneous YouTube tabs survivable.
- [`docs/cross_channel_engagement.md` § Subscribe-only fast path](cross_channel_engagement.md#subscribe-only-fast-path---no-like-2026-05-11) —
  the laptop attach-mode CLI (`cross_engage_burner_attached --no-like`)
  takes the dedupe insight one step further: visits each unique source
  channel's `/channel/<UC>` page directly instead of opening even the
  deduped representative `/watch?v=…`. Same `_probe_subscribe`
  selectors, ~5-8× faster per burner. Candidate follow-up for the
  cloud worker's `subscribe_only` mode (would replace the per-channel
  watch-page open with a per-channel channel-page open — same actions,
  lighter pages).

## Files touched

- [`web/server.py`](../web/server.py) — `auth_middleware` K_SERVICE bypass
- [`control/routes/burner_routes.py`](../control/routes/burner_routes.py) — cloud enqueue
- [`pipeline/cross_engage/burner_engage.py`](../pipeline/cross_engage/burner_engage.py) — GCS state, GCS stop sentinel, brand-switch + Shorts URL, headless arg
- [`pipeline/cross_engage/cross_engage_via_playwright.py`](../pipeline/cross_engage/cross_engage_via_playwright.py) — `--headless=new` opt-in
- [`pipeline/cross_engage/cross_engage_burner_attached.py`](../pipeline/cross_engage/cross_engage_burner_attached.py) — `_read_active_uc` `/account` fallback
- [`pipeline/laptop_agent.py`](../pipeline/laptop_agent.py) — fire-and-forget `_exec_burner_engage` + env injection
- [`pipeline/utils/catalog.py`](../pipeline/utils/catalog.py) — GCS-aware reads

## See also

- [`docs/burner_channels.md`](burner_channels.md) — burner spin-up flow
- [`docs/full_cloud_cutover_2026_05_09.md`](full_cloud_cutover_2026_05_09.md) — the phase-4 stub note this doc supersedes
- [`docs/cloud_run_quota_self_service.md`](cloud_run_quota_self_service.md) — how the deploy quota was bumped (60 vCPU, asia-southeast1)
- [`docs/cross_channel_engagement.md`](cross_channel_engagement.md) — the older anonymous-tab flow for production-channel-to-channel engagement (separate concern from burner brand engagement)
- [`docs/playwright_with_signed_in_chrome.md`](playwright_with_signed_in_chrome.md) — cookie bridge / CDP attach patterns
