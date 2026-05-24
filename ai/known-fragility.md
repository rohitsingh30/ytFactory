# Known Fragility

Catalog of fragile sites in pipeline + control. Each entry cites code,
explains what it depends on, and names the trigger that breaks it. The
overarching pattern is the same one Q49–Q51 of onboarding-qa.md
diagnosed: **quality gates fire too coarsely AND error-swallowers hide
real failures**. Some sites do both — they fail loud after first being
silent, but the loudness is whole-render termination not granular
retry.

Sources: code reads on 2026-05-22; design tension surfaced in
`/ai/onboarding-qa.md` Q47–Q77; PRE-FIX behaviour patterns persist in
sibling sites the 2026-05-15 audit didn't touch.

---

## F1. Two coexisting render entry points in `pipeline/render/video.py` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 3 deletes the
legacy `render()` and locks `render_via_engines` as the canonical entry
(ADR-027). Entry kept for historical context.

**Where:** `pipeline/render/video.py:81` (`render()`) and
`pipeline/render/video.py:128` (`render_via_engines()`).

**Why fragile:** The module docstring (lines 1–43) describes a Slice-2
state that no longer exists — it says `kind=short` is a no-op
("NotImplementedError, worker still handles short") and Slice 5 will
absorb sports_doc / footage_only. In reality the bigbang already
absorbed everything into `render_via_engines()` and `render()` itself
calls into the new path via `render_long_form()` → `render_via_engines()`
at line 321. The legacy `render()` entry is dead for SHORT (raises
NotImplementedError at line 116) and a thin wrapper for LONG_FORM —
yet it's still exported (`__all__` at line 801) and still imported
by callers that may not have migrated.

**What trips it:** A new contributor reads the docstring, believes
short still goes through the worker stages, writes a caller that
invokes `render(spec)` for a short → blows up at runtime with
`NotImplementedError`. The 2026-05-14 / 2026-05-15 sequence of
"slice 5 / bigbang / fail-loud audit" left the docstring drift in
place. Charter principle "the function body is authoritative" applies
literally here.

---

## F2. `spec.extra` is an untyped dict carrying load-bearing state across plugin slots

**Where:** `pipeline/render/spec_enrich.py:145,214` (writes
`era_anchor_prefix`, `character_description`), read sites at
`pipeline/render/short_engine.py:157,177,414,445,454,458,462,466,571,588`,
`pipeline/render/visualize/ai_beat_slideshow.py` (reads many keys),
`pipeline/render/visualize/longform_panels.py:91-94` (reads
`image_provider`, `image_style_prefix`, `image_seed`, `image_steps`),
`pipeline/render/music/ducked_loop.py:96,116`,
`pipeline/render/overlays/anchored_footage.py:97`,
`pipeline/render/audio/audio_from_fixture.py:32,47`,
`pipeline/render/visualize/visuals_from_fixture.py:26,43`,
`pipeline/render/timeline/timeline_from_fixture.py:21`.

**Why fragile:** `RenderSpec.extra: dict[str, Any]` is a free-for-all
namespace. Every plugin reads keys with `.get(name, default)` and no
schema. A typo in `spec_enrich.py` (e.g. `caracter_description`)
silently produces empty character lock; the rendered character drifts
across beats and the only signal is visual-inspection-after-the-fact.
The "spec_enrich populates BEFORE plugins read" invariant
(`short_engine.py:146-152`) is implicit — the engine can be entered
from a code path that skips `populate_render_extras()` and the
plugins will quietly use defaults.

**What trips it:** Adding a new visualize plugin and forgetting to
mirror the `spec.extra` reads `ai_beat_slideshow` uses. Or shipping
a render path (tests, fixtures) that constructs RenderSpec directly
without going through `build_spec()` + `populate_render_extras()`.

---

## F3. Long-form rewrite parallel fan-out with zero cross-section awareness

**Where:** `pipeline/llm/rewrite_long_form.py:617`
(`_call_outline_with_retry`), `pipeline/llm/rewrite_long_form.py:880`
(`_call_section_body_llm`), `pipeline/llm/rewrite_long_form.py:1017`
(`_generate_all_section_bodies`),
`pipeline/llm/rewrite_long_form.py:1109` (ThreadPoolExecutor,
`_SECTION_BODY_MAX_WORKERS=5`).

**Why fragile:** The outline call allocates per-section `target_words`
within a `[0.5x, 2x]` clamp of the mean. Then N body calls fire in
parallel — each LLM call sees ONLY its own target. The LLM can't see
peer sections, can't see the total budget, can't redistribute. Per-section
retries (`_SECTION_BODY_MAX_RETRIES=2` at line 1014) regenerate from
scratch — they do NOT carry forward "you under-delivered last time by N
words; expand to M." After fan-out the only correction is the aggregate
hard gate in `pipeline/critic_long_form.py:222` (`HARD_FLOOR_FRAC=0.50`)
and `pipeline/critic_long_form.py:225` (`HARD_SECTION_FLOOR_FRAC=0.40`)
which kills the entire render if either tripples.

**What trips it:** This is the documented root cause of jobs `a734babb`
(section 7 = 51 words against mean 363, 14% of mean → trips the
`HARD_SECTION_FLOOR_FRAC=0.40` gate) and `7743ca76` (writeback
duration 1232s < 1440s required → trips the duration gate). Both
were marked rendered "successfully" through every stage and died at
the final aggregate gate. Onboarding-qa Q55–Q56 + Q58–Q63 spell out
the fix vector.

---

## F4. Kwarg-drift between caller and helper at `longform_panels` → `build_image_panels_video`

**Where:** `pipeline/render/visualize/longform_panels.py:127-139`
calling `pipeline.render.shared.long_form_lib.build_image_panels_video`.

**Why fragile:** Pre-2026-05-15 the caller passed `narration_dur_s=`,
`out_path=`, `image_w=`, `image_h=` — none existed on the helper's
signature. The outer `except (TypeError, Exception)` swallowed
`TypeError: unexpected keyword argument` and dispatched to
`_fallback_solid_color`. Every cloudsdecoded / historyrecapped
long-form shipped 26 min of black for weeks (per the docstring at
lines 58-70). The current code pins the real signature but the broad
`except Exception` at line 140 STILL swallows any future signature
drift. The only safety net is the contract pin test
`tests/render/visualize/test_long_form_fallback.py::LongformPanelsBuildKwargContractTest`.

**What trips it:** Any future refactor of
`build_image_panels_video()` that renames or removes a kwarg, with
contributor running ONLY their immediate test file, not the pin test.
The error path now defaults to `RenderFailedError` (good — F11 below)
but a contributor who sees the test fail can re-add
`YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1` to "make it work" and re-introduce
26 min of black.

---

## F5. `RenderFailedError` is too coarse — whole-render termination on per-piece failure (DESIGN TENSION)

**Where:** `pipeline/render/contracts.py:119-154` (definition);
fires at `pipeline/render/visualize/ai_beat_slideshow.py:243,492,608`,
`pipeline/render/visualize/longform_panels.py:234`,
`pipeline/render/visualize/_fallback.py` (helpers at 53, 93, 129, 155).

**Why fragile / design tension:** The 2026-05-15 fail-loud audit was
a structurally correct fix to the silent-fallback class of bug — black
renders, captionless mp4s, frozen-frame tails. But per onboarding-qa
Q49–Q51 ("our philosophy is wrong; gates should be granular retry,
not whole-render termination") the current implementation has the
opposite problem: a 10% per-beat failure → kill the whole 30-min render
instead of retrying the 3 failing beats with a stronger prompt + fallback
provider. The `YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1` escape hatch
trades one failure mode (visible silent failure) for another (invisible
solid-black silent failure). Neither matches the locked principle:
"gate fires → retry the failing piece."

**What trips it:** Any per-beat image-gen failure rate >10% (cloud
incident, content-filter trip, prompt with attractor tokens) at
`ai_beat_slideshow.py:492` kills the whole short. Any
`build_image_panels_video` exception kills the whole long-form. The
test matrix at `tests/render/test_fail_loud_fallbacks.py` pins the
fail-loud behaviour, so the granular-retry refactor (onboarding-qa
attack items 9, 6) must EVOLVE the tests, not break them.

---

## F6. Closer-panel-related dead code path in `pipeline/compose.py:711-719`

**Where:** `pipeline/compose.py:711-719`.

**Why fragile:** Comment block explicitly says "closer_format /
closer_panel_path are accepted for back-compat but ignored — no panel
or row PNGs are rendered or overlaid by compose anymore."
`closer_caption_rows: list[Path] = []` and `closer_panel_path = None`
are hardcoded. The accepted-but-ignored kwargs are a footgun: a
caller upstream still passes them, believes compose honors them, and
ships a render missing the closer. The new engine architecture expects
this functionality to live in the `overlays/` plugin slot
(`pipeline/render/contracts.py:280-330`).

**What trips it:** Any channel YAML that still configures
`closer_panel`-style fields, or any caller in `make_*` scripts that
constructs a closer_panel_path arg expecting it to render. The signal
is silent — the render succeeds, just without the closer.

---

## F7. Subprocess `_extract_last_traceback` heuristic in `pipeline/render/video.py:642-714` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 3 deletes the
subprocess heuristic machinery alongside the legacy `render()` entry
(tech-debt D5). The render path is in-process; no subprocess traceback
filtering is needed. Entry kept for historical context.

**Where:** `pipeline/render/video.py:604-714`.

**Why fragile:** This 100-LoC heuristic exists to filter OTel
exporter tracebacks out of the "real error" surface. It walks the
log backward, classifies tracebacks by "first File frame in
opentelemetry path." Real render errors that *transit through* OTel
code (e.g. a Cloud Logging handler invoked in user code) could be
mis-classified as telemetry noise and never surfaced. The classifier
checks the FIRST `File` frame only — sufficient for the documented
cases but not provably robust. The fallback to "last 25 lines" at
line 672 also depends on tracebacks being well-formed.

**What trips it:** A future OTel SDK version that emits tracebacks
with a different first-frame pattern; or a user-code error that
genuinely starts inside an OTel wrapper. The error surface degrades
silently — operator sees the wrong traceback and chases a phantom
bug (the documented post-mortem of 8a4f7e15).

NOTE: this function is also called from a path
(`_format_subprocess_failure` at line 717) the bigbang made dead —
`render_long_form` calls `render_via_engines` in-process now, not via
subprocess. The function still exists and is still exported.
Pure dead code in the cloud worker path.

---

## F8. Outline-LLM target_words can imbalance section allocations

**Where:** `pipeline/llm/rewrite_long_form.py:617`
(`_call_outline_with_retry`); the [0.5x, 2x] clamp is enforced
downstream by `_call_section_body_llm` (line 880) via the per-section
target it receives.

**Why fragile:** No outline-level sum-check exists today. The outline
LLM can return `sum(section.target_words)` significantly off the
total target (Q55: "outline LLM can imbalance section allocations
within clamp [0.5x, 2x] of mean"). Combined with no peer awareness
across parallel section bodies (F3), the aggregate under-delivery
compounds. Onboarding-qa Q63 spells out the proposed fix
(retry-outline-once if sum is outside ±15% of total). Today: no
such gate.

**What trips it:** Any topic where the outline LLM picks an
imbalanced act structure (e.g., 3 long acts + 7 short scenes, or
one expository preamble at 0.5x mean), and the parallel fan-out
runs to completion before the aggregate gate fires. Same class as
job `a734babb`.

---

## F9. Broad `except Exception` swallows in artifact-emission paths

**Where:** `pipeline/render/video.py:276-277, 397-398, 426-427`
(emit_artifact calls wrapped in `except Exception: ... logger.warning`).
Also `pipeline/render/artifacts.py:82, 96, 194, 360`.

**Why fragile:** When `emit_artifact()` fails, the render proceeds.
That's correct in one sense — the mp4 is the primary deliverable, not
the live preview — but the dashboard's "live preview" promise (per
the docstring at video.py:246-260) silently goes dark. Two classes of
failure are conflated: transient GCS hiccup (correctly recoverable
by ignoring) AND a misconfigured artifact pipeline (silently produces
NO previews for every render).

**What trips it:** Misconfigured GCS bucket, IAM regression on
`render-runner@` SA, or any structural breakage of `emit_artifact()`.
User sees the dashboard pills update via Firestore but the audio /
script / panel previews never mount.

---

## F10. Duplicate route files in `control/*.py` + `control/routes/*.py` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 2 (migrate
callers) + Phase 3 (delete legacy) lock `control/core/*` +
`control/routes/*` as canonical (ADR-028). Top-level flat duplicates
are deleted. Entry kept for historical context.

**Where:** `control/agent_routes.py` vs `control/routes/agent_routes.py`
(differ), and same for `dashboard_routes.py`, `render_routes.py`,
`scheduler_routes.py`, `niche_routes.py`. Both directories also have
parallel `auth.py` (control/auth.py + control/core/auth.py),
`schema.py` (control/schema.py + control/core/schema.py),
`jobs.py` (control/jobs.py + control/core/jobs.py).

**Why fragile:** `web/server.py:1792-1813` (production FastAPI app)
imports ONLY from `control.routes.*` and `control.core.*`.
`control/server_dev.py:24-39` (laptop dev FastAPI) imports BOTH old
flat `control.agent_routes` etc. AND new `control.routes.*` (with
`_v2` suffixes). Result: laptop dev runs against a DIFFERENT route
set than production. Bug reported in dev may not exist in prod and
vice-versa. Confirmed by `diff -q` on all 5 pairs (all `differ`).

**What trips it:** A contributor adds a route to
`control/render_routes.py` (the flat one), tests locally via
`control/server_dev.py`, ships — and the route never reaches prod
because `web/server.py` only imports `control/routes/render_routes.py`.
Or the inverse: a fix lands in `control/routes/` and laptop dev
behaves stale.

---

## F11. Solid-color fallback survives via env override (`YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1`)

**Where:** `pipeline/render/visualize/_fallback.py:53,93,129,155`
(check the env), `pipeline/render/visualize/longform_panels.py:151,233`,
`pipeline/render/visualize/footage_windows.py:57,63,75` (legacy site
still has unguarded silent fallbacks — see below),
`pipeline/render/visualize/ai_beat_slideshow.py:504-506` (silent
0-images fallback).

**Why fragile:** The env override exists "for emergency renders" but
nothing rate-limits its use, alerts on it, or surfaces "this render
was emergency-mode." If `YTFACTORY_ALLOW_SOLID_COLOR_FALLBACK=1`
ever lands in `cloud/render-worker-v2/deploy.sh` (it doesn't today,
line 106 — verified) every long-form ships black silently. Also
`footage_windows.py` still calls `self._fallback_solid_color()`
directly (lines 57, 63, 75) without going through the audit-gated
path — those sites are not consistently fail-loud.

**What trips it:** Any operator setting the env var "just to
unblock the demo." Or contributors confused by the test failures and
adding the env to the test fixture instead of fixing the underlying
issue.

---

## F12. `ai_beat_slideshow.py` has TWO silent-fallback escape paths the audit missed

**Where:** `pipeline/render/visualize/ai_beat_slideshow.py:503-506`
("0 images produced — falling back to solid color") and
`pipeline/render/visualize/ai_beat_slideshow.py:515-517`
("stitch failed — falling back to solid color").

**Why fragile:** Both go through `_fallback_solid_color()` directly,
NOT the gated path. Comments at lines 504, 516 say "falling back" with
a `_logger.warning()` — same shape as the bugs the 2026-05-15 audit
fixed. The 10% per-beat gate fires at line 492 IF some images succeed
but is bypassed when ALL beats fail (the 0-images branch). And the
stitch-failed branch fires AFTER successful image-gen, so the audit's
"refuse to ship visibly-broken artifact" principle is breached on
ffmpeg-side failures.

**What trips it:** A cloud-image-service total outage (all beats
fail) → 0 images produced → solid color short ships silently. Or
an ffmpeg stitch error from a corrupted intermediate (rare but
non-zero in production).

---

## F13. `_synth_cloudrun_indicf5` ref_audio_text dispatch (suspected bug, per onboarding-qa Q71)

**Where:** `pipeline/tts/cloudrun.py:813` (`_synth_cloudrun_indicf5`),
called from chunked synthesis at line 793.

**Why fragile:** Onboarding-qa Q71 attributes the IndicF5 "Hindi
gibberish noise" issue to this function not actually shipping
`ref_audio_text` to the model. Line 837 has `if not ref_audio_text:`
guard but the actual outbound request body needs verification.
HindutavaAnimated renders are downstream-coupled to whatever this
function emits — silent malfunction produces shippable mp4s with
inaudible/incorrect narration. Q9 reliability pain item #2 ("renders
'succeed' but ship with wrong captions / frozen frames / missing
audio") covers this class.

**What trips it:** Any HindutavaAnimated render. The signal is
late — only audible on playback of the finished mp4. No
RenderFailedError fires because TTS technically returns a wav.

---

## F14. `noqa: BLE001` density signals systematic error-swallowing

**Where:** 235 occurrences across pipeline/ + control/ (grepped
2026-05-22). Hotspots:
`pipeline/research/cross_engage.py` (~12 broad excepts),
`pipeline/niche_specs.py:293-406` (8 sites),
`pipeline/render/video.py` (10 sites in this one file),
`pipeline/render/short_engine.py` (8 sites),
`pipeline/render/input_registry.py` (7 sites).

**Why fragile:** `# noqa: BLE001` suppresses ruff's "blind-except"
lint. Each site is a deliberate "I will catch any error here and
continue." Several are defensive (telemetry, progress_cb forwarding),
which is correct. But the density makes audit hard — a contributor
adding a NEW broad except inherits the appearance of legitimacy from
the surrounding pattern. Charter principle "If a stage can't produce
its real output, raise an exception" gets diluted across 235 sites.

**What trips it:** New stage added with broad-except by reflex
(see F12, F11 for sibling sites the audit missed). The maintainability
risk is the lack of a "broad-except budget" — there's no review gate
that says "we have 235; that's the ceiling."

---

## F15. Module docstring drift in `pipeline/render/video.py:1-43` — RESOLVED 2026-05-23

**Status:** RESOLVED 2026-05-23 — refactor-plan.md Phase 3 deletes the
legacy `render()` and rewrites the module docstring (tech-debt D7).
Entry kept for historical context.

**Where:** `pipeline/render/video.py:1-43`.

**Why fragile:** The "Today's responsibilities (Slice 2
minimum-viable)" section describes a state from before the 2026-05-14
bigbang. It says `kind=short` is a no-op, `kind=sports_doc` /
`footage_only` are "not yet wired (Slice 5 lands the wiring)." None
of this is true now. The `render()` function still has the
NotImplementedError stubs for `kind=short` (line 116) and other
kinds (line 122). Whether those are correct CURRENT behaviour or just
leftover from the migration is unclear from the code alone.

**What trips it:** Any future contributor reading the docstring as
ground truth. Charter: "Comments and docstrings can drift; the
function body is authoritative" — exactly the case here. Same
class as Q67–Q68's prompt-refiner-docstring mismatch with reality
(klein-only docstring on a Z-Image-Turbo-only production system).

---

## F16. Ordering assumption: `populate_render_extras` MUST precede plugin reads

**Where:** `pipeline/render/short_engine.py:146-152` (the call site),
read sites across all visualize / overlays / music / audio plugins
(see F2).

**Why fragile:** The invariant is undocumented in `RenderSpec` itself
and unenforced. `spec_enrich.py:51` says "Idempotent: if spec.extra
already has a key, don't overwrite" — which means a partial
pre-population (test fixtures, future callers) is silently used as-is
even if it's stale. No `assert "character_description" in spec.extra`
or similar at plugin entry.

**What trips it:** A test fixture that pre-populates 2 of the 3
required extras keys; the visualize plugin reads `.get("foo", "")` and
proceeds with empty character_description. Render produces
character-drifted slideshow with no diagnostic.

---

## F17. Schedulers / launchd plists referenced but unaudited

**Where:** `control/com.ytfactory.cloud-critic.plist`,
`com.ytfactory.cloud-snapshot.plist`, `com.ytfactory.critique-runner.plist`,
`com.ytfactory.state-sync.plist`, `com.ytfactory.upload-next.plist`.
Schedule unknown without inspecting each .plist.

**Why fragile:** Per onboarding-qa Q36 "manual — every render is
triggered from /app/create UI; no cron/scheduler auto-fires renders."
But the launchd .plist files exist and presumably run on the laptop.
Q36 explicitly punts: "to verify whether these handle upload
scheduling, not render initiation." Unknown trigger surface = unknown
failure surface.

**What trips it:** A laptop that's never been audited for which
launchd jobs are currently loaded. Stale plist could be hammering a
deleted endpoint or running an outdated workflow with new artifacts.

---

## F18. Parallel section-body fan-out has no peer awareness → systematic length collapse

**Where:** `pipeline/llm/rewrite_long_form.py:1017`
(`_generate_all_section_bodies`), `pipeline/llm/rewrite_long_form.py:1109`
(`ThreadPoolExecutor(max_workers=5)`),
`pipeline/llm/rewrite_long_form.py:880` (`_call_section_body_llm`).

**Why fragile (Q55):** N parallel section-body LLM calls fan out via
ThreadPoolExecutor; each call sees ONLY its own `target_words`. No
cross-section visibility, no peer-budget signal, no aggregate
redistribution. When one section under-delivers, peer sections cannot
compensate; when the outline imbalances, the bodies honor the
imbalance and the total under-delivers. The aggregate gate (currently
`HARD_FLOOR_FRAC=0.50` at `pipeline/critic_long_form.py:222`) fires
post-hoc when the damage is irreversible. This is the documented
root-cause pattern that produced job `a734babb` (section 7 at 14% of
mean) — overlaps with F3 (which catalogued the call-site shape).

**What trips it:** Any topic where the outline LLM produces unevenly
sized sections, or any section LLM under-delivers due to thin source
material; the parallel fan-out runs to completion before the issue is
visible. Onboarding-qa Q55 names this as the architectural failure
class; Q58 + Q59 + Q63 are the fix vector. Per ADR-023 the gate stays
but the trigger shape moves to granular per-section retry (D6.6 +
ADR-007) so a peer-blind body still has a recovery path.

---

## F19. Outline LLM can imbalance section allocations within [0.5x, 2x] of mean

**Where:** `pipeline/llm/rewrite_long_form.py:617`
(`_call_outline_with_retry`); enforcement happens implicitly via
`_call_section_body_llm` (line 880) accepting the per-section target
verbatim.

**Why fragile (Q55):** The outline returns `target_words` per section
within a `[0.5x, 2x]` clamp of the mean. Within that band, the outline
LLM can pick a heavily imbalanced shape (e.g. 3 long acts + 7 short
scenes, or one expository preamble at 0.5x mean) with no sum-check
that `sum(target_words) ≈ total target`. Combined with F18 (parallel
bodies honor their allocations blindly) and F3 (no aggregate
redistribution), the imbalance compounds into systematic
under-delivery. Onboarding-qa Q63 spells out the fix: outline
sum-check with single retry → hard-fail (ADR-006). Today: no such
gate. Overlaps with F8 (same site; this is the explicit "Q55 lock"
entry).

**What trips it:** Any outline LLM call that selects an unbalanced act
structure under the `[0.5x, 2x]` clamp. Same root cause class as job
`a734babb` even though the per-section terminal gate is the visible
fire site.

---

## F20. `_PER_BEAT_FAILURE_THRESHOLD=0.10` is a first-failure kill, not a post-retry ceiling

**Where:** `pipeline/render/visualize/ai_beat_slideshow.py:90`
(constant), `pipeline/render/visualize/ai_beat_slideshow.py:492`
(fire site).

**Why fragile (Q66):** The 10% threshold today fires on the FIRST
beat-failure aggregate exceeding it — there is no per-beat retry
before the gate decides. A cloud-image-service hiccup that breaks
12% of beats kills the whole short on first attempt. Per
ADR-023 + locked operating principle 1, the gate stays but the
trigger shape moves: post-gen image-quality validator → per-beat retry
with stronger prompt → only after retry exhaustion does the beat count
as failed → only then does the 10% aggregate fire and kill the render.
The 10% becomes a *post-retry* ceiling, not a first-fail kill.
Onboarding-qa Q66 spells out the retry shape; refactor-plan P4.2
implements.

**What trips it:** Any transient cloud-image-service outage exceeding
10% beat failure rate. The retry path doesn't exist yet, so today
the only mitigation is operator-side re-trigger of the whole render.

---

## F21. `_synth_cloudrun_indicf5` may not ship `ref_audio_text` to the model

**Where:** `pipeline/tts/cloudrun.py:813` (`_synth_cloudrun_indicf5`),
called from chunked synthesis at line 793.

**Why fragile (Q71):** Onboarding-qa Q71 attributes HindutavaAnimated's
"Hindi gibberish noise" to this function not forwarding
`ref_audio_text` to the IndicF5 model. Line 837 has the
`if not ref_audio_text:` guard but the outbound request body needs
verification — config-side reads succeed; payload-side write may
silently drop. Hindi renders technically return a wav so no TTS-level
error surfaces; the failure is only audible on playback. Overlaps
with F13 (same site; this is the explicit Q71-lock entry with the
ADR-016 fix locked).

**What trips it:** Every HindutavaAnimated render. Mitigation per
ADR-016 is a targeted one-line fix + regression test that asserts
`ref_audio_text` is in the request payload (refactor-plan P4.4).

---

## F22. `spec.caption_style` not wired end-to-end → captionless mp4s ship silently

**Where:** `pipeline/render/spec.py` (`RenderSpec.caption_style` field),
overlay producers at `pipeline/render/overlays/word_caption_pngs.py` +
`pipeline/render/overlays/sentence_caption_ass.py`, compose mux in
`pipeline/render/compose/compose.py`.

**Why fragile (Q73):** `spec.caption_style` is set at the engine entry
but is lossy through the overlay → compose path. Per onboarding-qa Q73
the wiring is incomplete: caption import failures currently warn
rather than hard-fail; Devanagari font is missing from the worker
Dockerfile (line 29-38) so Devanagari beats render as boxes; there is
no post-render caption density gate. Result: a render can ship a
"successful" mp4 that has no captions, or has captions for half the
audio time, or has Hindi captions rendered as Unicode replacement
boxes. The signal is silent (no exception, no telemetry, no gate fire).
ADR-012 + refactor-plan P5.2 are the structural fix.

**What trips it:** Any render where the captions pipeline silently
degrades — caption import broken, font missing, ASS pipeline returns
empty timing, density below threshold. The mp4 still ships.

---

## F23. Writeback duration gate kills successful mp4s (`7743ca76` case)

**Where:** `cloud/render-worker-v2/entrypoint.py:~2400` writeback
verification (referenced by job-failure log of `7743ca76`).

**Why fragile (Q54):** The writeback gate hard-fails when mp4 duration
< 80% of target (1440s floor for 1800s target). Job `7743ca76`
(2026-05-21 19:48) produced a real 59.7 MB h264+aac mp4 at 1232s —
perfectly watchable, perfectly valid — and the writeback gate killed
the render because 1232 < 1440. The duration floor duplicates length
policing already done (or that *should* be done) at the rewrite gates;
when upstream gates fire correctly the duration is naturally in band;
when upstream gates over-correct or under-correct, the writeback gate
fires on a real artifact that operators would have happily shipped.
Per ADR-010 + ADR-023 the gate becomes a sanity check only (mp4 has
valid streams; duration > 0; no truncation). Refactor-plan P3.7
implements.

**What trips it:** Any render where the rewrite gates allow a
shorter-than-target narration (within their tolerance bands) AND the
writeback floor is tighter than the rewrite tolerance — exactly the
state today, where rewrite uses `HARD_FLOOR_FRAC=0.50` (50%) but
writeback enforces 80%.

---

## F24. Cloud-service Dockerfile per-file COPY drift (2026-05-23)

**Where:** every `cloud/<svc>/Dockerfile` (render-worker-v2,
image-z-image-turbo, tts-chatterbox, tts-indicf5, asr-whisper,
editing-agent) uses explicit per-file `COPY` instructions, NOT a
wholesale `COPY cloud/<svc>/ ./`.

**Why fragile:** Any new `.py` file added to a cloud-service source
directory is **invisible to Docker** until the Dockerfile is also
edited. The build/push/deploy pipeline runs to completion against
the stale Dockerfile and produces an image structurally missing the
new code. Two failure flavours:

- **Hard fail.** Module imported unconditionally → cold-start
  `ModuleNotFoundError`. Surfaces immediately. Pattern: commit
  `4597686` fixed `writeback.py`; 2026-05-23 same bug bit
  `_stage_envelope.py`.
- **Silent fail.** Module imported via `try: ...; except: x = None`
  softener (cf. the 3 TTS/ASR servers' `_tel_track_io` imports).
  Service starts fine, `/readyz` returns 200, revision goes healthy
  — but every code path that depended on the module is now a no-op.
  Body-capture telemetry had been silently dark for some period
  before discovery on 2026-05-23.

**What trips it:** Every PR that adds a new helper module to a cloud
service directory. The build is silent about the omission. The
deploy is silent about the omission. The runtime is silent (the
softened variant) or noisy (the hard variant).

**Counter-test:** `tests/cloud/test_dockerfile_copy_completeness.py`
(added 2026-05-23, commit `8345624`). Enumerates every
`cloud/<svc>/*.py` and asserts each is named in a `COPY` line in
the matching Dockerfile (skips services that use wholesale
`COPY cloud/<svc>/ ./`). 8/8 services pass with the
2026-05-23 fix. Run before every `deploy.sh`:

```
.venv/bin/pytest tests/cloud/test_dockerfile_copy_completeness.py -q
```

**Incident chain:**
- Commit `4597686` (2026-05-XX): writeback.py case fixed without
  guardrail.
- 2026-05-23: class repeated across 4 services (1 hard, 3 silent).
  ~3 hours of operator time lost to a "deploy is complete" claim
  that was structurally false. Fixed in commit `8345624`; guard
  test added.

See also: `docs/cloud_service_dockerfile_discipline.md`;
memory `feedback_humiliation_2026_05_23.md` (the behaviour-side
correction that the gate-side fix doesn't enforce on its own).

---

## F25. "rev-healthy = deploy complete" assumption (2026-05-23)

**Where:** every `cloud/<svc>/deploy.sh`. After the `gcloud run
deploy` returns, the script stops. There is no post-deploy
preflight that fires a real request and checks for the new
behaviour.

**Why fragile:** `gcloud run services describe` returning
`Ready=True` proves only that the container started, `/readyz`
returned 200, and the entrypoint module imported. It does NOT
prove:
- That telemetry-specific imports succeeded (try/except softener
  hides this — F24).
- That the new files added in recent commits are physically in the
  image (the F24 bug).
- That the actual new behaviour the deploy was supposed to ship
  fires for a real request.

The operator (or LLM) reads rev-healthy as "shipped" and stops
verifying. Failures the rev-health-check can't see become invisible.

**What trips it:** Any deploy that ships new code paths that
aren't exercised by `/readyz`. Body-capture telemetry, new
endpoints, new fields in existing endpoints, new env-var
consumers — all of these can be silently broken while the rev is
healthy.

**Counter-test:** None yet automated. Minimum operator discipline:
deploy is complete only when (1) rev healthy AND (2) a real
preflight render triggered AND (3) `stage.start` visible in
Cloud Logging for that job_id AND (4) at least one body-capture
entry visible for at least one stage.

**Incident:** 2026-05-23 — same chain as F24. See
`feedback_verify_telemetry_with_preflight.md` (memory) and
`feedback_humiliation_2026_05_23.md`.

---

## F26. `cloud/_shared/auth_setup.sh` 1-hour token TTL trap (2026-05-23)

**Where:** `cloud/_shared/auth_setup.sh` (token issuance) +
every `cloud/<svc>/deploy.sh` (which sources it).

**Why fragile:** Token is issued at deploy.sh start. If build +
deploy together exceeds ~55 min (z-image-turbo can take 50+ min
for build alone, plus 5-10 min Cloud Run image import), the
`gcloud run deploy` polling step fails mid-poll with
`UNAUTHENTICATED ... ACCESS_TOKEN_EXPIRED`. Cloud Run continues the
rollout server-side (it doesn't care that the deploy.sh polling
session died), so the rev still becomes healthy — but the script
reports failure and any post-deploy verification logic in the
script doesn't run.

**What trips it:** Any large image deploy (z-image-turbo at 40 GiB
is the canonical case). Worse with the slow Cloud Run side
import.

**Counter-test:** None. Fix: refresh the impersonated token after
`Build SUCCESS` and before `gcloud run deploy`.

**Incident:** 2026-05-23 — z-image-turbo 54-min build + slow image
import exceeded the TTL; the deploy.sh exited non-zero even though
the rev shipped cleanly.

**Status:** unfixed. Logged in `/ai/improvement-opportunities.md`.

---

## F27. Reconciler silently no-ops because `completion_time` is a `datetime`, not a proto Timestamp (2026-05-24)

**Where:** `control/core/reconciler.py:_reconcile_one`.

**Why fragile:** `google-cloud-run` (the `run_v2` client) uses
`proto-plus`, which auto-converts proto3 `Timestamp` to a
`DatetimeWithNanoseconds` (a `datetime` subclass). A `datetime` has
NO `.seconds` attribute, so `getattr(..., "seconds", 0)` returned
`0` for **every** real completion — every completed execution was
labelled `still_running` and never reconciled. Tests passed in
isolation because the fakes used the old raw-proto shape with a
`.seconds` attribute.

**Compounding failure modes** in the same incident:

1. `firestore.indexes.json` had ZERO indexes deployed to
   `ytfactory-prod-v3`. `/api/queue` Completed column 400'd.
2. Reconciler needed an extra index — `jobs(status ASC, updated_at
   ASC)` — that wasn't in the spec.
3. Reconciler swallowed per-status query failures silently.
4. `com.ytfactory.job-reconciler.plist` was never installed.
5. Plist used `curl POST http://127.0.0.1:8090/...` — no-op when
   laptop FastAPI server is down.

**Counter-test:** `tests/test_control_reconciler.py::`
- `test_completion_time_as_datetime_marks_job_failed`
- `test_unset_completion_time_as_epoch_datetime_is_still_running`
- `test_all_status_query_failures_surface_error`

**Incident:** 2026-05-24 — job `82682e8e5d1e4c8bae8ad35ec5468250`
(mystoriesanimated tifu) sat queued for 24h+ before noticed.

**Status:** fixed 2026-05-24.

* `control/core/reconciler.py` — completion_time handles both
  proto-plus datetime and raw-proto seconds shapes.
* `summary["error"]` + `per_status_errors` populated on scan failure.
* `firestore.indexes.json` — added missing `jobs(status, updated_at
  ASC)`. All 3 indexes deployed via gcloud.
* `com.ytfactory.job-reconciler.plist` — Python-direct invocation
  (no HTTP localhost dep); installed + `launchctl bootstrap`ed.

Tracked further as **O33** (move reconciler to Cloud Scheduler) and
**O34** (bootstrap script for `firestore.indexes.json`) in
`/ai/improvement-opportunities.md`.

---

## F28. `verify_web_runner.sh` false-reports MISSING when impersonated SA can't read IAM (2026-05-24)

**Where:** `cloud/iam/verify_web_runner.sh` — every `_check_*` uses
`gcloud ... 2>/dev/null || true`.

**Why fragile:** The deployer SA (via `cloud/_shared/auth_setup.sh`
impersonation) doesn't have `resourcemanager.projects.getIamPolicy`
/ `iam.serviceAccounts.getIamPolicy` /
`secretmanager.secrets.getIamPolicy`. Every gcloud read silently
returns empty → the script counts the binding as MISSING. Result:
deploy.sh fails preflight with all 8 bindings flagged MISSING
**even when they are actually present**. Violates
`feedback_silent_fallback_unshippable_output`: "missing" and
"unreadable" must be distinguishable.

**Counter-test:** None. Need a test that stubs gcloud-read to fail
with PermissionDenied and asserts the script exits non-zero with a
"cannot read IAM — grant the deployer SA roles/iam.securityReviewer"
message instead of false "MISSING".

**Incident:** 2026-05-24 — `cloud/web-server/deploy.sh` blocked at
preflight; bindings verified present as owner identity but the
deployer SA's silent read-fail looked identical to "absent".
Workaround: `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT=""
CLOUDSDK_AUTH_ACCESS_TOKEN=$(gcloud auth print-access-token) bash
cloud/web-server/deploy.sh` to run the deploy under operator's
owner identity.

**Status:** workaround applied; **O35** tracks the script fix
(distinguish absent from unreadable).

---

## F29. Long-form image-gen bypasses the refiner; bare scene + noun-tag anti-text prefix collapses to floating-jersey product photos (2026-05-24)

**Where:**
- `pipeline/render/shared/long_form_lib.py::_generate_panel_stills`
  (pre-fix lines 598-771) — called `images.generate(prompt=scene,
  style_prefix=…)` with the raw authored `scene` text.
- `pipeline/images/images_cloudrun.py::ANTI_TEXT_PREFIX` (pre-fix
  lines 492-499) — prepended 190 chars of comma-separated noun tags
  ("Clean surface, unmarked, blank jersey, smooth fabric, plain
  backgrounds, unmarked book covers, unlabeled bottles, no signage,
  no banners, no watermark, no logo, no caption, no street signs.")
  to every cloud-z-image-turbo prompt.
- `pipeline/render/visualize/ai_beat_slideshow.py::_resolve_prompt_for_beat`
  (lines 167-235) — the **shorts** path *did* route through the
  refiner via `refined_fields_for_render` + `images.build_full_prompt`.
  Long-form had a parallel, refiner-blind path.

**Why fragile:** The refiner's `refined_scene` starts with the literal
substring `"no readable text in image."`; the anti-text-suffix
idempotence check on that substring meant refiner-emitted prompts
were a no-op for the prepend. **The bare-scene long-form path never
hit that substring**, so the noun-tag prefix prepended for every
panel. With short scene tails ("Cloud-filled sky from airplane
wing." = 41 chars), the 190 chars of "blank jersey, smooth fabric,
plain backgrounds, unmarked book covers, unlabeled bottles" was
56-82% of the prompt by length. z-image-turbo's text encoder reads
comma-separated noun tags as subject candidates; **"blank jersey"
is the strongest concrete clothing-subject noun in image-model
training distribution**, so the model rendered a literal floating
product-photo white tee on a plain background, with the panel-
specific scene tail relegated to the background (e.g. clouds
behind the t-shirt). The author's design rationale ("positive
framing at the front because distilled models are left-weighted")
was structurally correct; the choice of noun tags as the framing
was the bug.

**What trips it:** Every long-form render on every channel that uses
the `longform_panels` visualize plugin (mystoriesanimated long-form,
hindutavaanimated long-form, cosmosdecoded long-form). The shorts
path was unaffected because it routed through the refiner.

**Counter-test:** `tests/test_anti_text_suffix_shape.py` pins the
verb-led SUFFIX shape (forbidden subject-nouns absent, ≤250 chars,
verb-led, appended-not-prepended, idempotent on legacy + refiner +
double-apply paths). `tests/render/shared/test_long_form_refiner_routing.py`
pins that long-form routes through `build_full_prompt` with the
refined fields when the flag is on, falls back per-beat when a slot
is empty, RAISES on whole-batch refiner failure.

**Incident:** 2026-05-23 + 2026-05-24 — two consecutive
mystoriesanimated/nosleep long-form renders (job `76508d12…` and
`845bdb0d…`) shipped ~45-46 of 77 panels as floating headless white
t-shirts. See:
- `data/critiques/i-ve-been-flying-for-almost-thirty-hours-and-the-flight-atte-845bdb0d.md`
- `.claude/skills/diagnose-render/learnings/845bdb0df20e4ba3885ca33c7749e74d.md`
- `data/critiques/i-ve-been-flying-for-almost-thirty-hours-and-the-flight-atte-845bdb0d.bugs.md` Bug A
- Memory `project_z_image_turbo_verb_led_prompts.md` Rule 16 ("no X / no Y" negations stuffed into positive prompt) and the bootstrap example in `learnings/_index.md`.

**Status:** fixed 2026-05-24 in `fdc5f65`.
- `pipeline/images/images_cloudrun.py:492-555` — ANTI_TEXT_SUFFIX
  replaced with a 232-char verb-led safety clause, appended-not-
  prepended. Backward-compat aliases kept.
- `pipeline/render/shared/long_form_lib.py` — `_refine_long_form_panels`
  helper + rewritten `_generate_panel_stills` builds wire prompts
  via `build_full_prompt(refined_visual, refined_scene, style_block)`
  (same as shorts). Whole-batch refiner failure RAISES per
  `feedback_silent_fallback_unshippable_output`.
- Tracked further as **O36** (refiner routing — shipped) and
  **O37** (verb-led SUFFIX — shipped) in
  `/ai/improvement-opportunities.md`.

---

## F30. Style propagation gap — no style anchor reaches the wire prompt; same render contains five mutually-incompatible aesthetics (2026-05-24)

**Where:**
- `pipeline/channels/<channel>.yaml` carries `image_style_prefix:`
  with a rich per-channel description ("Warm hand-drawn 2D
  illustration in the style of modern indie webcomics. Confident
  ink line work…").
- The shorts path passed it through `images.build_full_prompt(...,
  style_prefix=…, style_block=…)` correctly, but the long-form
  path (F29) discarded it along with the rest of the refined
  fields. The refiner's `style_block` output was unreachable from
  long-form panels.

**Why fragile:** Without a style anchor in the prompt, z-image-turbo
free-styles per panel — picking whatever aesthetic best matches each
panel's content. Same character renders as 2D flat illustration,
3D Pixar cartoon, photoreal stock-photo, and floating-product-tee
across the same 26-minute video. No editorial pass can repair
multi-style output; it has to be prevented at refiner time.

**What trips it:** Same code path as F29 — every long-form render
on every visual channel.

**Counter-test:** Per-panel substring scan in the diagnose-render
pass: every panel's `final_prompt` must contain at least one
style token (`photoreal | illustrated | animated | cinematic |
cartoon | realistic | studio | stock photo | film | hand-drawn`)
matching the channel's declared `image_style_prefix`. The 845bdb0d
render scored **0/77** on this scan; post-fix should score **77/77**.

**Incident:** 2026-05-24 — `data/critiques/i-ve-been-flying-...-
845bdb0d.md` confirmed 5 distinct visual styles in the same 26-min
render (vector illustration, 3D Pixar cartoon, photoreal stock,
floating product tee, plus headless-cropped-torso variants).

**Status:** fixed 2026-05-24 in `fdc5f65` (composes with the F29
refiner routing fix — the channel's `image_style_prefix` now flows
into `style_block` which lands in `build_full_prompt`'s first
~150 chars per memory `project_z_image_turbo_verb_led_prompts.md`).

---

## F31. `pipeline/render/overlays/sentence_caption_ass.py::SentenceCaptionAss.produce` calls `build_captions_ass` with kwargs that don't exist in the signature → runtime TypeError every time `bottom_one_line` / `bottom_two_line` is selected (2026-05-24)

**Where:**
- `pipeline/render/overlays/sentence_caption_ass.py:70-76` (pre-fix)
  called `build_captions_ass(cues=…, out_path=…, total_duration_s=…,
  play_res_x=…, play_res_y=…)`.
- `pipeline/render/shared/long_form_lib.py:1703` actual signature:
  `def build_captions_ass(out_ass, *, narration_text=…, chunk_wavs=…,
  max_lines=2, …)`.

**Why fragile:** None of the wrapper's kwargs (`cues`, `out_path`,
`total_duration_s`, `play_res_x`, `play_res_y`) existed on the actual
function. Any render that selected `CaptionsLayout.BOTTOM_ONE_LINE`
or `BOTTOM_TWO_LINE` would hit `TypeError: build_captions_ass() got
an unexpected keyword argument 'cues'`. Bug went unobserved because
the most-used layout is `CENTER_WORD_BY_WORD`, routed to the
separate `word_caption_pngs` plugin, never reaching this code path.
Also: the wrapper never passed `max_lines`, so the 1-line vs 2-line
UI distinction was structurally unrespected even if the call had
worked.

**What trips it:** Any render whose `RenderSpec.captions_layout` is
`BOTTOM_ONE_LINE` or `BOTTOM_TWO_LINE`. Both shorts and long-form
engines use the same `_captions_plugin_for_layout` router so both
were exposed.

**Counter-test:** `tests/test_caption_layout_ui_respected.py` —
9 tests pinning (a) routing per layout, (b) sentence_caption_ass
does NOT crash on real Timeline + Audio, (c) `max_lines=1` truncates
with ellipsis (no `\N`), (d) `max_lines=2` wraps with `\N`, (e)
aspect-aware font size scales `caption_style.font_size` for the
actual `play_res_y`.

**Incident:** Latent. The bug never surfaced in production because
all observed renders used `CENTER_WORD_BY_WORD`. Surfaced 2026-05-
24 during the post-845bdb0d code audit while checking that the UI's
caption-layout selection was actually honored end-to-end.

**Status:** fixed 2026-05-24 in `fdc5f65`. Rewrite is self-contained
— writes the ASS file directly from the Timeline's segments, no
dependency on `build_captions_ass`. Honors `max_lines` from
`CaptionsLayout`, aspect-aware font size, uses `spec.caption_style`
fields verbatim.

---

## Cross-cutting pattern

Every entry above shares one root: **error handling is calibrated for
the silent-failure class (audit 2026-05-15) but not for the
granular-retry shape onboarding-qa Q51 + Q64 locked.** Fail-loud is
the *right* default vs silent corruption, but it's the *wrong* shape
when the failing piece is recoverable. The attack set items 1–13 in
onboarding-qa are the structural fix; this doc is the inventory of
sites that need to flip from "raise on first failure" to "retry the
failing piece, raise on retry-exhaustion."

**Second cross-cutting pattern (2026-05-23):** deploys are claimed
"complete" off the wrong evidence (rev-healthy alone). F24 + F25 +
F26 are three sides of the same operator-discipline failure: the
quick signal (rev-healthy, build SUCCESS, exit code 0) was treated
as the full signal. The fix is operator-side: verify with a real
preflight render before declaring shipped. The gate-side counter-test
exists for F24 but not yet for F25/F26.
