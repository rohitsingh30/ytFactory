# Driving 50+ simultaneous Chrome tabs without choking

When Phase 0 of the all-tabs-cycle pattern (see
[`docs/all_tabs_cycle_pattern.md`](all_tabs_cycle_pattern.md)) opens
50+ YouTube Shorts tabs in one signed-in Chrome session, three
specific things have to be tuned or the worker stalls. All three were
discovered live on 2026-05-10/11 while building cross-engage v2.

## Symptom

Phase 0 starts fine — the first 13-14 tabs open within seconds. Then
`Page.goto` starts taking 30-60 s per tab. By tab 20+ Chrome's render
process gets killed by the OS for memory pressure (`Page crashed`
error in the Playwright stack trace). State file shows
`opened=14/58, errors=1` and stops growing.

## Root cause

Each YouTube Shorts page auto-plays a video on load. Until you mute
or pause it, the audio decoder runs on a per-tab thread and
holds onto a non-trivial slice of CPU + audio-mixer kernel resources.
Past ~10 simultaneous decoders, the next tab's network request gets
deprioritised by Chromium's resource scheduler, so `Page.goto` waits
its turn — for tens of seconds. Meanwhile the original 10 tabs are
still decoding, so the queue never drains.

## Fix — three changes, all required

### 1. `--mute-audio` Chrome flag

In [`pipeline/cross_engage/cross_engage_via_playwright.py:launch_chrome_for`](../pipeline/cross_engage/cross_engage_via_playwright.py):

```python
args = [
    CHROME_BIN,
    "--remote-debugging-port=0",
    ...,
    # Mute every tab globally. The cross-engage Shorts loop opens
    # 50+ tabs concurrently; un-muted, the audio decoders saturate
    # the user's CPU and slow page.goto() to 10+ s per tab. YouTube
    # still counts views from muted plays, so watch-time signal is
    # unaffected. Validated 2026-05-10.
    "--mute-audio",
]
```

YouTube counts watch-time the same way for muted plays (Page Visibility
API + the same playback events on the page) — verified by post-load
view-count probes during the live test. So this is a free win for the
worker even if you needed actual audio playing for some other workflow.

### 2. `wait_until="commit"` instead of `"domcontentloaded"`

In `burner_engage.run`'s Phase 0 loop:

```python
page.goto(play_url, wait_until="commit", timeout=30000)
```

* `commit` returns as soon as the navigation request commits (HTTP
  response headers + first chunk received). ~500 ms typical.
* `domcontentloaded` (the default) waits for the parsed DOM event
  — on a Shorts page this includes Lit-element hydration of the
  player and view-model components. 5-15 s under load.
* `load` would wait for video data + thumbnails. Even slower.
* `networkidle` would never fire on YouTube — there's always a
  background poll.

The cycle's act-or-verify step (Phase 1) gives each tab plenty of
time to fully render before the first probe — so the early `commit`
return doesn't matter for action correctness. It just means Phase 0
finishes in 5-10 min instead of 30+ min.

### 3. 1-second per-tab throttle

```python
for vs in state.videos:
    page = ctx.new_page()
    page.goto(play_url, wait_until="commit", timeout=30000)
    video_pages[page] = vs
    time.sleep(1.0)   # ← THE throttle
```

Without the throttle, even with `--mute-audio` + `commit`, Chrome's
network scheduler still gets ahead of the renderer: 58 simultaneous
nav requests stress the macOS DNS resolver + TLS handshake pool.
1 s spacing means 58 tabs takes ~58 s of network setup — completely
fine for a worker that runs for hours afterward, and YouTube's
anti-automation heuristics don't flag the rate (a bot would
probably hammer at 100/s, not 1/s, from a signed-in profile).

User explicitly requested 1 s/tab. Don't reduce to <500 ms without
re-probing the failure modes — even 0.5 s on the test laptop made
tab 30+ start hitting the 30 s `commit` timeout sporadically.

## What this *doesn't* fix

These three flags get Phase 0 reliable. They don't speed up Phase 1's
per-tab probe → click → verify, which is bound by:

- YouTube's player JS hydration (~3-8 s after `commit`).
- The Lit-element view-model components for like-button +
  subscribe-button mounting (~1-3 s after hydration).
- The probe's `get_attribute("aria-pressed", timeout=2000)` which
  returns "unknown" if the element doesn't exist yet.

The cycle handles this gracefully: probes that return "unknown" just
fall through without action, and the next visit re-probes. With 8-20 s
dwell per tab × 58 tabs, every tab gets re-visited every ~10 min, by
which time the player has long since finished hydrating.

## Memory / resource budget

58 YouTube Shorts tabs in a signed-in Chrome window = **~3.5-5 GB
RAM** + ~80% CPU on an M-series MacBook (one CPU core fully busy
running the page-cycle work, others handling Chrome). Doable on
16 GB+ Macs, will OOM-thrash on 8 GB.

If you need to engage from a Mac with less RAM, options:

- Bound the catalog: only the most recent N videos (currently 58 →
  reduce to 20 if memory-constrained).
- Use a bounded mode (`subscribe_only` or `like_subscribe`) which
  exits when actions are done, instead of the infinite watch-loop
  modes that hold all tabs forever.
- Run on a different burner's Chrome profile that doesn't have a
  monstrous tab-history cache.

## Headless mode is NOT a solution

`--headless=new` does NOT meaningfully reduce memory or CPU per tab
on YouTube Shorts (the Skia compositor + JS engine run regardless of
whether pixels go to a window). It also breaks the Like/Subscribe
selectors on the Shorts player (the actionable buttons silently
no-op when clicked headless). See
[`docs/cross_engage_cloud_v2.md` § Headless mode](cross_engage_cloud_v2.md#headless-mode---partial-support-opt-in-only)
for the partial-support story.

## See also

- [`docs/all_tabs_cycle_pattern.md`](all_tabs_cycle_pattern.md) — the
  Phase 0 + Phase 1 architecture this is the network/CPU layer for.
- [`docs/cross_engage_cloud_v2.md`](cross_engage_cloud_v2.md) — the
  end-to-end cross-engage cloud flow.
- [`docs/playwright_with_signed_in_chrome.md`](playwright_with_signed_in_chrome.md) —
  base Chrome-Debug + Playwright CDP launch pattern that this builds on.
