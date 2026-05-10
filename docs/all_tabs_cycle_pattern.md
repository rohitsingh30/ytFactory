# All-tabs-open + cycle pattern (2026-05-11)

When a worker has to act on N independent live web targets and
verification of each action requires a fully-rendered browser tab, the
naive "open one tab → act → close → next" pattern wastes most of its
wall-clock waiting for individual page loads + animations to settle.
The pattern below opens every target tab upfront and then cycles
through them in a single act-or-verify loop.

Originated 2026-05-11 inside
[`pipeline.cross_engage.burner_engage.run`](../pipeline/cross_engage/burner_engage.py)
for cross-engaging a burner channel against the 58-video production
catalog. Generalises cleanly to any "many concurrent live UI tabs to
act on" workflow we add later (mass-comment, mass-mute, mass-pin, …).

## The two phases

### Phase 0 — open all tabs

```python
video_pages: dict[Page, VideoState] = {}
for vs in catalog:
    if _maybe_stop(state):
        break
    page = ctx.new_page()
    page.goto(target_url, wait_until="commit", timeout=30000)
    vs.tab_open = True
    video_pages[page] = vs
    time.sleep(1.0)   # 1s/tab throttle — see many_chrome_tabs.md
```

* `wait_until="commit"` returns ~500 ms after navigation commits, NOT
  when DOMContentLoaded fires. Phase 1 will give each page time to
  fully hydrate at first cycle-visit; we don't need it loaded yet.
* 1 s/tab throttle keeps Chrome from getting 58 simultaneous nav
  requests in a one-second burst (Chromium queues them but the audio
  decoders + V8 isolates contend; in practice the 14th tab's `goto`
  starts taking 30+ s to even start parsing). With 1 s/tab pauses,
  58 tabs takes ~5-10 min on the user's MacBook Pro.
* Best-effort failures: if `ctx.new_page()` raises for one video,
  log the error onto `vs.error` and continue. Phase 1 cycles only
  the tabs that *did* open.

### Phase 1 — cycle act+verify+watch forever

```python
while True:
    if _maybe_stop(state): break

    needs_work = [p for p in pages if <tab still has missing actions>]
    pool = needs_work if needs_work else pages   # fall back to all tabs for watch-time
    pg = random.choice(pool)
    vs = video_pages[pg]

    pg.bring_to_front(); time.sleep(0.5)
    # dismiss consent / sign-in interstitials
    # for each action (like / subscribe / comment):
    #   probe state → if not done and clickable, click → re-probe to verify
    # accumulate watch-time dwell

    err_streaks[pg] = 0   # success → reset
```

The cycle is **idempotent** — every visit re-probes state before
acting, so a transient YouTube race (rate-limit, half-rendered DOM,
flaky network) doesn't permanently mark the tab as failed. The next
visit picks it up.

`needs_work` is recomputed per tick: as Likes / Subscribes complete,
their tabs drop out and the pool naturally shrinks. Once empty, the
loop falls back to picking any tab and just dwelling for watch-time
(or exits the loop entirely if the mode is bounded — see
[`engagement_modes` section in cross_engage_cloud_v2.md`](cross_engage_cloud_v2.md#engagement-modes-2026-05-11)).

## Per-tab error streak handling

Tabs can persistently misbehave (a `bring_to_front` that always
raises but doesn't kill the page object — happens when Chrome puts
the tab into a frozen state under memory pressure). Without a
streak counter the cycle would loop forever on that tab.

```python
err_streaks[pg] = err_streaks.get(pg, 0) + 1
page_alive = True
try: pg.title()
except Exception: page_alive = False
if not page_alive or err_streaks[pg] >= 3:
    pages.remove(pg)
    video_pages.pop(pg, None)
    if not pages:
        state.phase = "failed"; break
```

Three strikes (any combination of "page object dead" or "alive but
keeps erroring") drops the tab from rotation. The streak resets to
0 on any successful tick.

## Why not async / asyncio.gather over all tabs

Tempting — open + engage all 58 in parallel — but Playwright's
sync API + the way YouTube's player JS arbitrates audio focus mean
parallel `bring_to_front` + click events from a single signed-in
session race in unpredictable ways. The serial cycle is slower per
tab but the per-tab outcomes are deterministic. Verified 2026-05-11.

## State save cadence

Cycle saves state every 5 ticks (~50 s max stale on the
dashboard's 2.5 s poll). At one save per tick the GCS write
volume becomes excessive (each tick = one `_save_state` = one
`_push_state_gcs` upload, ~58 ticks per minute = ~1 write/sec).
The 5-tick cadence keeps GCS write count manageable while
keeping the UI poll responsive.

```python
tick += 1
err_streaks[pg] = 0
if tick % 5 == 0:
    _save_state(state)
```

Every action event also fires `_bump_action(state, …)` which
internally `_save_state`s — so success events surface immediately
on the dashboard regardless of the 5-tick cadence.

## When to use this pattern

Use **all-tabs-open + cycle** when:

- N targets share a single signed-in browser session (so opening
  many tabs is cheap — same cookies, same CDP).
- Each target needs an action that can be verified by re-probing
  the same tab.
- You want watch-time / "active" signal to start accumulating
  immediately, not after a slow per-target serial loop completes.
- You want resilience to per-target transient failures (cycle
  retries; sequential pattern would skip).

Use the older **sequential per-target** pattern when:

- Targets are on different domains / require different sessions.
- Each action is destructive (uploading a file) and re-probing
  doesn't make sense.
- You only have 1-3 targets.

## See also

- [`docs/cross_engage_cloud_v2.md`](cross_engage_cloud_v2.md) — the
  cross-engage cloud flow that uses this pattern.
- [`docs/many_chrome_tabs.md`](many_chrome_tabs.md) — Chrome flags +
  goto policy required to make Phase 0 survive 50+ simultaneous tabs.
- [`docs/burner_channels.md`](burner_channels.md) — burner spin-up
  flow + the older single-burner-attached engage script (now
  superseded by this pattern for the cross-engage path).
