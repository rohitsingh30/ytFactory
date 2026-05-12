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

## Dual-source rule (added 2026-05-12 round 5)

Telemetry-stream views (Cloud Logging `ytfactory.event` records)
miss three classes of failure that operators care about:

* **Dispatch failures** — `gcloud run jobs execute` rejected the
  worker (quota exceeded, IAM denied, malformed env). The worker
  never started → no `obs.render_envelope` opened → no events
  emitted. The job is in Firestore with `status=failed,
  stage=dispatch`, but invisible to `/api/telemetry/renders`.
* **Cancelled jobs** — operator cancelled before the worker grabbed
  the task. Same blindspot.
* **Pre-envelope crashes** — worker booted but crashed during
  bootstrap (config missing, schema drift, broken proposal) before
  `obs.render_envelope()` opened the parent span.

The 2026-05-12 backfill found **8 of 50 historical jobs** were
dispatch failures invisible to the telemetry view — a 16% blindspot
that made the dashboard structurally lie about success rate.

**Rule:** every operator dashboard MUST have at least one view
sourced from the **control-plane source of truth** (Firestore
`jobs` collection) alongside the telemetry-derived views. The
control-plane view is the **default landing tab** — it's the only
one that answers "did my job run at all?" honestly. Implementation:
`/api/telemetry/jobs` (Firestore-backed) sees every job. Per-row
`stages` array is synthesised from the doc's `timeline` field via
`_synthesize_stage_durations()` so historical jobs (predating any
telemetry plumbing) render the same horizontal stacked-bar
waterfall as Cloud-Logging-backed renders — full backfill, not
just a status badge.

## Tabs over scrolling for ≥4-section dashboards (added 2026-05-12 round 5)

Five+ sections in a single column trigger one network poll each on
mount and expensive recharts renders for charts the operator may
never look at. Tabs scope active polling to the visible tab AND
let the operator's eye land on the question they came to answer
without scrolling past four cards first.

**Rule:** any operator dashboard with ≥4 logically distinct
sections (e.g. Jobs / Renders / Stage latency / Services / LLM
costs / Errors / Activity / Open in GCP) ships as tabs, not a
single column. Tab default matches the most-asked operator
question — for `/app/telemetry` that's Jobs (status of submitted
work), NOT Activity (sparkline of event counts).

A permanent header section ABOVE the tabs is fine when the data
is genuinely cross-cutting (e.g. the "Overview" total-events /
success-rate cards). Don't over-use this — anything operator-
specific belongs IN a tab.

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
5. **Does at least one view source from the control-plane source of
   truth (Firestore / Postgres / wherever the work was *enqueued*)?**
   Telemetry-only dashboards miss dispatch failures, cancellations,
   and pre-envelope crashes — see "Dual-source rule" above.
6. **Tabs or single column?** ≥4 distinct sections → tabs. The
   default tab matches the most-asked operator question, not
   chronological order of when the panels were built.

## Reference

* Live endpoints: `/api/telemetry/renders`, `/api/telemetry/stage_latency`,
  `/api/telemetry/jobs`, `/api/telemetry/llm_costs`
* Live UI: `/app/telemetry` → Jobs (default) / Renders / Stage Latency /
  Services / LLM cost / Errors / Activity / Open in GCP tabs
* Implementation: `control/routes/telemetry_routes.py`,
  `web-next/app/app/telemetry/{renders,stage-latency,jobs,llm-costs}-section.tsx`,
  `web-next/app/app/telemetry/page.tsx` (tabs layout)
* Tests: `tests/test_routes_telemetry_api.py::{TestStageLatency,
  TestRenders, TestJobsEndpoint, TestLLMCosts, TestSynthesizeStageDurations}`
* Cross-link: `docs/telemetry.md` (architecture / data flow)
* Memory pointer: `feedback_telemetry_dashboard_two_round_blindspot.md`
  §"2026-05-12 round 4", `feedback_telemetry_dashboard_design.md`
