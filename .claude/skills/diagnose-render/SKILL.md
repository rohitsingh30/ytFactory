---
name: diagnose-render
description: Automated diagnostic playbook over a completed (or failed) render job. Pulls Cloud Logging events + Firestore decision_log + every GCS artifact under gs://ytfactory-prod-v3-artifacts/jobs/<id>/, reconstructs the stage timeline, classifies findings against /ai/known-fragility.md (F1-F26), points at fixes in /ai/improvement-opportunities.md (O1-O32), and emits a markdown report. Use when the user says "diagnose render <job_id>", "what went wrong with <job_id>", "why is <job_id> bad", "/diagnose-render <id>", or after any render where output quality is suspect. For visual / editorial review use /critique-video. For audio review use /critique-audio. For perception-side bugs translated to engineering use /critique-to-bugs (which is complementary — qualitative lens to this skill's instrumentation lens).
---

# /diagnose-render — telemetry-first render diagnosis

You are the **engineering-diagnostic** lens on a render. Not the
viewer's lens (`/critique-video`), not the listener's lens
(`/critique-audio`) — the instrumentation lens. You answer the
question: *given what the pipeline emitted while this render ran,
what does the evidence say went well, what went poorly, and which
fragility patterns or improvement opportunities does each finding
map to?*

Your output is a markdown report. Every finding is grounded in a
specific event timestamp, artifact path, or fragility ID. No
speculation. No "looks like" without a cited event.

## Inputs

- `<job_id>` — a 32-char UUID-less hex string (e.g.
  `76508d12a4a84e638fb44a45294711d3`). The job MUST have terminated
  (state `done` or `failed`). If state is `running` or `queued`,
  refuse unless `--partial` is passed; even then, flag every
  inference that depends on partial state.
- Optional `--save-only` — skip stdout report; only write the
  learning file.
- Optional `--no-save` — print only; skip the learning file.

## How to run it

### 1. Confirm the job exists and is terminal

```bash
gcloud firestore documents describe "jobs/${JOB}" \
  --project=ytfactory-prod-v3 --format=json
```

Extract: `state`, `channel`, `variant`, `slug`, `topic`, `created_at`,
`updated_at`, `final_mp4`, `error`. If `state` is `running` /
`queued`, refuse unless `--partial`.

Also list every execution + the most recent one:

```bash
gcloud run jobs executions list \
  --project=ytfactory-prod-v3 \
  --region=asia-southeast1 \
  --job=ytfactory-render-worker-v2 \
  --limit=5
```

Find the execution whose logs reference this `job_id` (the
`textPayload:"<id>"` or `jsonPayload."ytfactory.job_id"="<id>"`
filter).

### 2. Pull the three telemetry surfaces

Per `docs/render_telemetry.md` § "The three surfaces":

**A. Cloud Logging (structured events):**

```bash
gcloud logging read "jsonPayload.\"ytfactory.job_id\"=\"${JOB}\"" \
  --project=ytfactory-prod-v3 --limit=500 --freshness=24h \
  --format=json > /tmp/diag_${JOB}_events.json
```

Be aware of the field shape: `jsonPayload.ytfactory.event`,
`jsonPayload.ytfactory.category`, `jsonPayload.ytfactory.success`,
`jsonPayload.ytfactory.duration_ms`, `jsonPayload.ytfactory.meta.*`.
Do NOT query `jsonPayload.event` — that path is empty
(known mistake 2026-05-23; the dotted prefix `ytfactory.` is part of
the field name, not a path separator).

Aggregate distinct events, durations, success/failure counts.

**B. GCS artifacts (full I/O dumps):**

```bash
gsutil ls gs://ytfactory-prod-v3-artifacts/jobs/${JOB}/
```

Expected subdirs (varies by render): `script/`, `cast/`,
`prompts_refined/`, `refiner_io/`, `image_meta/`, `images/`,
`tts_chunks/`, `asr/`, `timeline/`, `music/`, `compose/`,
`telemetry/`.

Cat the small JSON artifacts; download the large ones to a temp dir
only if you need to inspect their content:

```bash
# Always cat: small JSON artifacts
gsutil cat gs://ytfactory-prod-v3-artifacts/jobs/${JOB}/script/script.json
gsutil cat gs://ytfactory-prod-v3-artifacts/jobs/${JOB}/cast/cast.json
gsutil cat gs://ytfactory-prod-v3-artifacts/jobs/${JOB}/prompts_refined/prompts_refined.json
gsutil cat gs://ytfactory-prod-v3-artifacts/jobs/${JOB}/refiner_io/refiner_io.json
gsutil cat gs://ytfactory-prod-v3-artifacts/jobs/${JOB}/image_meta/00000.json
# ... (and a sample of N panels for image prompt diversity check)
```

**C. Firestore decision_log:**

The job doc has a `decision_log[]` array. Each entry has
`stage`, `decision`, `reason`, `timestamp`. This is the
human-readable narrative of "we chose X over Y at stage Z."

### 3. Reconstruct the stage timeline

Walk events chronologically. For each stage:

| stage | started | ended | duration_ms | success | notes |
|---|---|---|---|---|---|

The standard stages (vary by `RenderSpec.kind`):
`rewrite → cast → prompts → render_plan → engine.pick → tts → asr → images → timeline → compose → upload`.

Note any:
- `stage.failed` events
- Stages with `duration_ms > 60_000` (long stages worth flagging)
- `llm.retry` events (each retry = signal of a prompt or schema issue)
- `image.refiner.fallback` events (refiner emitted a non-list shape;
  the legacy build_full_prompt path took over — F2-class symptom)
- `prompts.resolve_for_beat` with `source: legacy_build_full_prompt`
  (means the refiner output didn't match the schema for that beat)

### 4. Per-stage observation passes

Walk each stage and look for known patterns. For every observation,
cite the event timestamp + meta field that supports it.

**Rewrite stage:**
- `llm.call` events with `stage=rewrite` — count, total duration,
  retries.
- Section-body word-count delta vs target (long-form only).
- Pattern match: F3 (parallel fan-out no peer awareness),
  F8 / F19 (outline imbalance), F18 (parallel bodies blind).
- Cite: `event.timestamp`, `event.meta.{model,input_chars,duration_ms}`.

**Cast stage:**
- Read `cast/cast.json` — character count + structured fields.
- Pattern match: F2 (untyped spec.extra), O10 (structured cast schema).

**Prompts / image-refiner stage:**

This stage has the highest blast-radius silent-failure mode in the
pipeline (refiner runs but every beat falls back → render ships
floating-tee panels). The checks below are **MANDATORY** — produce a
"[REFINER FALLBACK]" finding any time any of them tip.

1. **`fallback_count` from `refiner_io/refiner_io.json` — REQUIRED
   quote.** Read this file. Quote the integer. Non-zero = the refiner
   call collapsed; that many beats fell back to the legacy
   `build_full_prompt(character_description=…)` path. The legacy path
   produces coherent prompts on shorts because the channel YAML's
   character description is rich enough to mimic refined output, so
   the rendered mp4 looks fine and hides the bug. The
   `refiner_io.json` artifact is emitted on **failure too** — its
   presence is not success-evidence; its **`fallback_count` value** is.
   - `fallback_count: 0` → shipped through refiner ✓
   - `fallback_count: n` (== beat count) → whole-batch failure;
     shorts shipped looking fine off legacy path; long-form would
     have RAISED via the F29 safety net in
     `long_form_lib._generate_panel_stills`.
   - `fallback_count` between 0 and n → per-beat partial fallback;
     check `parsed[i]` to see which beats fell back and why.
2. **`image.refiner.batch` event count.** Should equal the number of
   refiner calls (one per long-form panel batch, one per shorts
   author_beat_prompts). If zero events fired but the render
   completed → refiner code path wasn't reached at all (different
   bug from fallback). Two consecutive renders with zero events:
   F28 territory (refiner emits no telemetry).
3. **Per-event `meta.source` on `prompts.resolve_for_beat`** —
   discriminates the path the wire prompt came from:
   - `"source": "refined"` → built via `build_full_prompt(refined_visual,
     refined_scene, style_block)` (post-refactor F29 fix path)
   - `"source": "legacy_build_full_prompt"` → built via the legacy
     `build_full_prompt(character_description, key_visual, scene)`
     path (refiner output discarded or absent)
   - All beats `legacy_build_full_prompt` while
     `image.refiner.batch.success=True`: refiner ran but the gate
     in `refined_fields_for_render` rejected its output (hash
     mismatch, version skew, missing field).
4. **`final_prompt` opening shape** in `image_meta/<N>.json`:
   - Opens with verb-led key_visual (`"medium shot of …"`,
     `"over-shoulder of …"`) → refined-path
   - Opens with character description (`"A 29-year-old woman with
     shoulder-length wavy brown hair …"`) → legacy path
   - The opening string is the tell. The middle of the prompt looks
     the same on both paths because the channel style + scene get
     concatenated in either case.
5. **Pattern match: F2, F11 / F12 / F20 (image-gen silent fallback),
   F29 (long-form bypasses refiner, fixed 2026-05-24), and the
   verb-led-prompt rule** from memory
   `project_z_image_turbo_verb_led_prompts.md`. Specifically:
   - Is `negative_prompt` populated, or are negatives stuffed into
     the positive prompt as "no X / no Y / no Z" tokens?
   - What fraction of the final prompt is panel-specific vs
     identical boilerplate? If boilerplate >70%, flag.
- Cite: `image_meta/<N>.json: final_prompt`, count chars, identify
  which segments vary across panels.

**Why the explicit checklist** (2026-05-24 incident, jobs `88d98126`
+ `c4aed485`): an earlier version of this skill listed the refiner
checks under "patterns to look for" — too easy to skim past. The
diagnose-render run on job 88d98126 missed `fallback_count: 14`
because the rendered mp4 looked coherent and the operator (me)
treated visible-output as proof the refiner-routed code path
executed. It hadn't. The shorts shipped through legacy fallback;
the long-form (c4aed485) RAISED. See memory
`feedback_verify_refiner_with_fallback_count.md` for the full
post-mortem and the rule it produced.

**Images stage:**
- Count `image.gen` events. Verify each has `provider`, `seed`,
  `width`, `height`, `cache_hit`.
- Look for any `cache_hit:true` (same prompt sha256 seen before —
  if EVERY panel is a cache hit, the prompt isn't varying).
- Per-image duration_ms — z-image-turbo on L4 is typically 60-140s
  per panel; anything >180s is a yellow flag.
- Pattern match: F11, F12, F20.

**TTS stage:**
- Count `tts.chunk` events. Verify `audio_seconds` adds up to the
  expected narration length per the channel's WPM × narration_chars.
- Body capture: does `meta.input_preview` show real narration text
  or empty / `null`? If empty, F24 (Dockerfile-COPY drift) may be
  silently disabling body capture for this service.
- Pattern match: F13 / F21 (IndicF5 ref_audio_text drop — Hindi
  channels only), F24 (silent _tel_track_io disable).

**ASR stage:**
- `asr.chunk` events should equal `tts.chunk` count.
- `word_count` per chunk vs narration word count.
- Cite for caption coverage: F22 (caption_style not wired).

**Compose / timeline stage:**
- `timeline.beat_map` events — how many anchored beats,
  unanchored_count.
- `ffmpeg.call` events — count, total duration, any non-zero
  exit_codes.
- Pattern match: F4 (kwarg-drift in long_form_lib),
  F5 / F11 (writeback gates / solid-color fallback), F23 (writeback
  duration gate over-eager).

**Upload stage (if reached):**
- `final_mp4` path in Firestore + file existence in GCS.
- Pattern: if upload failed but mp4 exists, that's a credential or
  quota issue (upload-via-playwright fallback territory).

### 5. Cross-cutting findings

After per-stage passes, look at the whole render:

- **Prompt monotony** — across all `image_meta/<N>.json`, compute
  the % of `final_prompt` that's identical character-for-character
  across panels. >70% identical = flag. >85% = strong flag. This is
  the verb-led-prompt failure mode caught on 2026-05-23 (job
  76508d12...; ~85% boilerplate, 7 panels all opening with "Clean
  surface, unmark…").
- **Stage-end vs stage-start parity** — every `stage.start` should
  have a matching `stage.end` or `stage.failed`. Missing closures
  suggest a crash mid-stage that bypassed the envelope.
- **Body capture coverage** — for at least one `llm.call`,
  `tts.chunk`, `asr.chunk`, and `image.gen` event, verify
  `meta.input_preview` is populated. If any are empty, F24
  (Dockerfile-COPY drift; check the matching service's image SHA
  vs the latest with `_tel_track_io.py` COPY'd in).
- **Channel-rule compliance** — read the channel's
  `<channel>/learnings/channel.md` + `pipeline/channels/<channel>.yaml`.
  Check the render's narration against the channel's closer pattern,
  banned phrases, duration band, WPM expectations. Flag every
  mismatch.

### 6. Map findings → F-IDs and O-IDs

For every finding, mandatory tagging:

- **Fragility tag**: cite at least one F-ID from
  `/ai/known-fragility.md`. If no F-ID covers it, prefix the finding
  with **`[NEW-FRAGILITY-CANDIDATE]`** so the next session knows to
  consider adding an F-entry.
- **Fix tag**: cite at least one O-ID from
  `/ai/improvement-opportunities.md`. If no O-ID matches, suggest
  one with **`[NEW-O-CANDIDATE]`** and a one-sentence pitch.

This dual-citation is the engineering-rigor gate. Per
`feedback_humiliation_2026_05_23.md` § "On reasoning traces", citing
the catalogue forces you to *have read* the catalogue before
asserting a finding.

### 7. Emit the report

Markdown report to stdout (and learning file). Required structure:

```markdown
# Render diagnosis — <job_id>

## Job summary
- channel/variant: <channel>/<variant>
- slug: <slug>
- final state: done|failed
- duration: Hh Mm
- final mp4: <gs path or "—">
- failure stage: <stage or "n/a">

## Stage timeline
| stage | started | ended | duration_ms | success | notes |
|---|---|---|---|---|---|
...

## Findings (chronological)

### 1. <one-line title>
- **Stage:** <stage>
- **Evidence:** event @ <timestamp> meta=<{key:value pairs}>,
  artifact `<gs path>`
- **Fragility match:** F<n> — <one-line description from
  known-fragility.md>
- **Fix target:** O<n> — <one-line description from
  improvement-opportunities.md>
- **Severity:** low | medium | high (high = shipped a bad render
  silently; medium = render survived but quality degraded;
  low = cosmetic)

### 2. ...

## Cross-cutting observations

### Prompt monotony
- N panels analysed; X% character-for-character identical across
  final_prompt.
- Verb-led key_visual position: char N of final_prompt (target: chars
  0-150 for z-image-turbo per memory project_z_image_turbo_verb_led_prompts.md).
- Negatives in positive prompt: yes/no, sample tokens.
- Pattern: F<n>; fix: O8 (Z-Image-Turbo refiner rewrite).

### Body-capture coverage
- llm.call: <populated/empty>
- tts.chunk: <populated/empty>
- asr.chunk: <populated/empty>
- image.gen: <populated/empty>
- If any empty: F24 — recommend redeploying the relevant service
  with the latest image (verify the Dockerfile COPYs the
  `_tel_track_io.py` file).

### Stage-envelope parity
- N stage.start events; M stage.end events; K stage.failed.
- Missing closures: <list>.

### Channel-rule compliance
- closer match: yes/no/partial
- banned phrases detected: <list>
- duration vs channel band: <within/over/under>
- WPM: <number> (channel target: <range>)

## Recommended next actions
1. (high) <action>; targets <O-ID>; effort <S/M/L>.
2. (medium) ...
3. (low) ...

## Self-learning notes
- New fragility candidate? <yes — short title | no>
- New O-candidate? <yes — short title | no>
- Patterns NOT previously seen in this skill's learnings: <list>
```

### 8. Save learning file

```
.claude/skills/diagnose-render/learnings/<job_id>.md
```

The learning file = the report verbatim, plus a small frontmatter:

```yaml
---
job_id: <id>
channel: <channel>
variant: <variant>
slug: <slug>
state: done|failed
diagnosed_at: <iso8601>
findings_count: <n>
new_fragility_candidates: <n>
new_o_candidates: <n>
---
```

Also append a one-line entry to `learnings/_index.md`:

```
- <job_id> (<slug>) — <n findings>, <new-frag>, <new-O>
```

### 9. Self-learning hook

If the user reviews the report and corrects a finding:

1. Classify the correction per `feedback_update_docs_<topic>` rules:
   - **ONE-OFF**: a single mis-classified F-ID — fix the learning
     file, append one line to `_index.md`.
   - **CLASS-OF-BUG**: the skill's heuristic is wrong (e.g. "the
     prompt-monotony threshold should be 60%, not 70%") — update
     this SKILL.md AND append a regression note to
     `learnings/<topic>.md` AND mirror to
     `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_diagnose_render_<topic>.md`.
2. If the user flags a NEW fragility pattern, edit
   `/ai/known-fragility.md` and add it as the next F-N entry.
3. If the user flags a NEW improvement, edit
   `/ai/improvement-opportunities.md` and add it as the next O-N
   entry.

The skill grows the catalogue by usage. Without this hook, F1-F26
calcifies and the diagnosis becomes stale.

## Important rules

- **Every finding MUST cite at least one event timestamp OR artifact
  path OR F-ID.** No speculation. Per
  `feedback_humiliation_2026_05_23.md` and
  `/ai/engineering-principles.md` § "Show evidence from code. Do not
  speculate." If you can't cite, you can't claim.
- **Every finding MUST cite an F-ID and an O-ID** (or explicitly
  mark `[NEW-FRAGILITY-CANDIDATE]` / `[NEW-O-CANDIDATE]`). The dual-
  citation is the engineering-rigor gate.
- **Never declare from rev-healthy alone.** Per
  `feedback_verify_telemetry_with_preflight.md` and
  `/ai/known-fragility.md` F25: revision health proves only that the
  entrypoint imported. To claim a service is shipping a feature, you
  must quote an actual event from that service.
- **Never declare a refiner / pipeline-stage fix shipped off
  output-looks-coherent alone.** Per
  `feedback_verify_refiner_with_fallback_count.md` and the 2026-05-24
  jobs `88d98126` + `c4aed485` incident: the shorts legacy fallback
  path is rich enough to mimic refined output, so a completely-dead
  refiner ships rendered shorts that look fine. Verification MUST
  quote `fallback_count: 0` from the relevant `*_io.json` artifact
  AND confirm `image.refiner.batch` events fired AND check
  `prompts.resolve_for_beat::meta.source == "refined"` (not
  `"legacy_build_full_prompt"`). If you fired a preflight render to
  validate a refiner fix, run `/diagnose-render <job_id>` on the
  result before saying anything about whether the fix shipped — not
  "in addition to" eyeball verification, **instead of**.
- **Refuse running on a non-terminal job** without `--partial`. A
  render mid-flight has incomplete artifacts; partial diagnoses
  mislead.
- **Use `.venv/bin/python`** for any helper command — consistency
  with other ytFactory skills.
- **Never invoke `pipeline.render.*` from this skill.** This skill
  is read-only on the deliverable; it diagnoses, it doesn't
  re-render.
- **Never write to Firestore from this skill.** Read-only on
  production state.
- **Don't fabricate severities.** "High" requires the artifact to
  have actually shipped to YouTube (or be on track to). "Medium" if
  the render survived but a downstream consumer (caption, audio,
  image) was visibly degraded. "Low" if cosmetic. Match the user's
  framing in `docs/post_upload_analysis.md`.

## Learnings from prior runs

(Empty on day 1. Each invocation appends a note to
`learnings/_index.md` and a per-job file to `learnings/<job_id>.md`.
On the second run, read those learnings before starting — they tell
you what patterns this skill has already caught for this codebase.)

**Bootstrap entry — job `76508d12a4a84e638fb44a45294711d3` (2026-05-23):**
The canonical worked example. Findings included:
- F24 (Dockerfile-COPY drift) — caught from absent body-capture
  events on tts.server / asr.server before render-worker fix
  shipped.
- F2 + verb-led-prompt monotony — 7 panels, ~85% identical
  boilerplate, key_visual buried at char 800+.
- O8 — Z-Image-Turbo refiner rewrite is the structural fix.
This bootstrap is here so the first real `/diagnose-render` run on
this repo doesn't start blind.

## Why this skill is separate from /critique-to-bugs

`/critique-to-bugs` takes *human-perception markdown* (from
`/critique-video` or `/critique-audio`) as input and traces
*editorial failure → engineering cause*. The lens is **what the
viewer saw**.

`/diagnose-render` takes *instrumentation* (Cloud Logging + GCS
artifacts + Firestore) as input and traces *what the pipeline
emitted* — sometimes the pipeline emits warning signs the viewer
never saw (e.g. silent solid-color fallback, refiner returning a
dict instead of a list). The lens is **what the pipeline reported
about itself**.

Running both on the same job is the right workflow when output
quality is suspect. They surface non-overlapping findings:
critique-to-bugs catches "the closer felt rushed"; diagnose-render
catches "the body-capture telemetry was silently disabled in
tts-chatterbox because the Dockerfile didn't COPY
`_tel_track_io.py`." Different inputs, different findings,
complementary.

## Reference

- `docs/render_telemetry.md` — the source-of-truth playbook
  (Recipes A-H) this skill automates.
- `/ai/known-fragility.md` — F1-F26 pattern catalogue cited in
  every finding.
- `/ai/improvement-opportunities.md` — O1-O32 fix-target catalogue
  cited in every recommendation.
- `pipeline/observability/telemetry.py` — emit-side; the field
  shape this skill reads.
- `pipeline/observability/bodies.py` — body-capture emit shape;
  used to spot silent-fail in F24 cases.
- Memory: `feedback_humiliation_2026_05_23.md` —
  the "show evidence; never declare from rev-healthy alone" rule.
- Memory: `feedback_verify_telemetry_with_preflight.md` —
  what "telemetry is live" actually proves.
- Memory: `project_z_image_turbo_verb_led_prompts.md` — the
  prompt-monotony / verb-led rule.
- Memory: `project_dockerfile_copy_drift.md` — the F24 detection
  recipe.
