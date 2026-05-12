# Telemetry dashboard design — show what the operator can act on

**Established 2026-05-12** after four rounds on the `/app/telemetry`
tab. First three rounds shipped infrastructure (in-process buffer →
Cloud-Run-shaped JSON exporter → cross-service Cloud Logging reader)
that fixed "the dashboard is empty". Round four was the user reading
the dashboard for the first time and saying "you are showing
bullshit" — because the panels still answered the wrong questions.

This doc is the rule that prevents the next dashboard from making the
same UX mistake.

## TL;DR — the rule

A render-pipeline dashboard MUST answer the operator's first question
**before** anything else:

> "Which renders are slow / failing, and where in each render did the
> time go?"

Generic cross-cut counters (total events, success rate, error rate
by category, by-channel rollups) look thorough but are second-order.
They are summaries OVER renders. They do not surface a slow render
or name where its time went — which is the only "where do I start
debugging?" signal an operator can actually act on.

## What round-1 dashboards get wrong

Pre-fix `/app/telemetry` showed (in this order):

1. **Overview** — total events, success rate, p50 / p95 of every
   event (mixed bag). Counts background `cloud.health.sweep` /
   `cache_hit` / HTTP-route auto-spans together with renders, so
   "1240 events, 99% success" is meaningless: 1190 are the cron
   sweep, 50 are renders, p95 is dominated by sweep latency.
2. **By category** — `tts: 12 / image: 30 / cloud: 1190 / pipeline:
   60`. Same problem: averages over heterogeneous events.
3. **By channel** — sums every event tagged with that channel, but
   most events don't carry channel context (no `obs.ctx` outside
   the renderer), so the rollup mostly says "?: 1180 / hr: 60".
4. **Activity (24 h)** — events per 15-min bucket. A heartbeat, not
   a debug surface.
5. **Services (last hour)** — provider rollup. Useful for "is
   chatterbox healthy?" but doesn't answer "did THIS render use
   chatterbox and how long did it take?".
6. **Recent errors** — useful, kept.

Net: an operator opening the tab to investigate "render X was slow"
finds zero per-render data. The dashboard renders fine, all
endpoints return 200, and the user is none the wiser.

## What a render-pipeline dashboard MUST show first

In priority order:

### 1. Recent renders — one row per render with stage waterfall

* One row per `(channel, slug, render_kind)`.
* Total wall-clock from the `render.<kind>` envelope event.
* Horizontal stacked bar showing where the time went, with bar
  width PROPORTIONAL TO wall-clock duration vs the slowest render
  in the window — so the operator's eye lands on the longest bar
  immediately. Same-width bars hide the signal.
* Segments coloured by stage type (sky=tts, violet=image,
  emerald=compose, pink=upload, amber=llm). Failed segment = rose
  override.
* Click a row to expand the per-stage detail table: stage /
  provider / duration / % of render / status.
* Sort: newest first. Channel filter optional.

Implementation: `/api/telemetry/renders` +
`web-next/app/app/telemetry/renders-section.tsx`.

### 2. Stage latency — bar chart of p50/p95 per render-pipeline stage

* Horizontal bar chart, one row per stage (`tts_synth`, `image_gen`,
  `llm_call`, `compose`, `upload_short`, `render.short`,
  `render.long_form`, …).
* Two bars per row: p50 (lighter) + p95 (saturated).
* Sorted by p95 descending — slowest at top. That's the "what
  should I optimise?" signal.
* **MUST filter to a render-pipeline whitelist.** Background
  bookkeeping events (`cache_hit`, `cloud.health.sweep`, HTTP
  request auto-spans) belong in Cloud Trace / Logging, not on the
  same chart as render stages — they swamp the visual.
* Tabular detail below the chart for the long tail (count, errors,
  mean, p50, p95, max, total ms).

Implementation: `/api/telemetry/stage_latency` +
`web-next/app/app/telemetry/stage-latency-section.tsx`.

### 3. Generic counters / activity / services / errors

These come AFTER the two render-first sections. They're useful as
secondary context — "is the system running at all? is one provider
red?" — but never as the primary surface.

## Concrete event-classification rule (for any new endpoint)

When a new `/api/telemetry/*` endpoint aggregates duration data, it
MUST decide which events qualify as "render-pipeline":

```python
# control/routes/telemetry_routes.py
_STAGE_EVENT_NAMES: set[str] = {
    "tts_synth", "image_gen", "llm_call",
    "asr_transcribe", "compose", "upload_short", "youtube_upload",
    "x_post", "post_short", "authenticate",
    "cast_author", "shotlist_author", "rewrite", "critic", "imitate",
}

def _is_stage_event(name: str | None) -> bool:
    if not name: return False
    if name in _STAGE_EVENT_NAMES: return True
    return name.startswith("stage.") or name.startswith("render.")
```

Anything NOT matching is bookkeeping (cron sweeps, cache hits, HTTP
auto-spans) and gets excluded from latency / waterfall / "where did
the time go" panels. Add new render stage names to the whitelist as
they're introduced; do NOT relax the predicate to "any event with
duration_ms".

## Pre-flight checklist for new operator dashboards

Before shipping a dashboard panel, answer:

1. **What operator question does this panel answer?** Write it as a
   single sentence. If the sentence is "show me a count of X",
   start over — that's a heartbeat, not a dashboard.
2. **Can the operator act on this signal?** "p95 latency of all
   events is 8 s" → not actionable. "render `aita-001` took 4 min,
   1.5 min in image_gen via cloudrun_flux2_klein" → actionable
   (look at flux logs).
3. **Does the visual ranking match operator priority?** Slowest
   bar = top. Newest render = top. Failed = red. If sort order is
   "alphabetical by stage name" or "count descending", you're not
   surfacing what matters.
4. **Are background events filtered out?** If `cache_hit` /
   `cloud.health.sweep` outnumbers real work 20:1, your aggregate
   is meaningless. Apply a render-pipeline whitelist before
   aggregating.

## Reference

* Live endpoints: `/api/telemetry/renders`, `/api/telemetry/stage_latency`
* Live UI: `/app/telemetry` → Stage Latency, Recent Renders sections
* Implementation: `control/routes/telemetry_routes.py`,
  `web-next/app/app/telemetry/{renders,stage-latency}-section.tsx`
* Tests: `tests/test_routes_telemetry_api.py::TestStageLatency`,
  `TestRenders`, `_seed_full_render`
* Cross-link: `docs/telemetry.md` (architecture / data flow)
* Memory pointer: `feedback_telemetry_dashboard_two_round_blindspot.md`
  §"2026-05-12 round 4"
