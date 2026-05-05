---
name: make-skill
description: Author a new ytFactory `/<name>` skill (or extend an existing one) that integrates cleanly with the pipeline, inherits channel rules, ships with quality gates and a self-learning loop, and surfaces engineering-efficiency wins in the existing process before adding code. Use when the user says "make me a skill that …", "I want a new /command for …", "let's automate …" (and it's an authoring/critique flow, not a hook), "spin up a /make-X variant", or proposes a recurring creative workflow that should become its own command. For harness automation ("every time X happens"), redirect to /update-config. For one-off scripts, just write the script.
---

# /make-skill — meta-skill for authoring ytFactory skills

This skill writes other skills. It enforces the 51 heuristics agreed on
2026-05-04 (see `learnings/heuristics.md`) so that every new `/make-*`,
`/critique-*`, or ops skill we add to ytFactory:

1. Plugs into the existing pipeline instead of re-implementing primitives.
2. Inherits the target channel's rules (closer, banned phrases, voice,
   aspect, footage policy, duration band) verbatim from
   `<channel>/learnings/` + `<channel>/config.yaml`.
3. Ships with **pre-render quality gates** and an explicit JSON output
   schema.
4. Carries a **self-learning loop** — each invocation appends regressions
   to its `learnings/` dir, classifies one-off vs class-of-bug, and
   updates pipeline code (not just the prompt) when the bug is structural.
5. **Always scouts for engineering efficiency** in the current process
   before adding new code — reuse, dedupe, propose simplifications.

You wear three hats: **Classifier → Architect → Author**.

---

## How to run it

### Stage 1 — Classifier (intent + dedup)

Before writing anything, lock down what the user actually wants. Ask
ONE consolidated clarification question if any of the following are
ambiguous; otherwise state your reading back in one sentence and let
the user redirect.

1. **Bucket** — `/make-*` (authoring) / `/critique-*` (post-render
   review) / ops or research / harness automation.
   - If it's "every time X happens, do Y", **stop and invoke
     `/update-config`** — that's a hook, not a skill.
   - If it's a one-shot script, suggest writing the script directly
     instead of dressing it up as a skill.
2. **Format** — Shorts (50-60s) / long-form sleep (60-120m) / sports
   doc (20-30m) / ranking / rivalry-recap / rhyme / movie / new format.
   New formats need a renderer — flag the renderer scope before you
   commit.
3. **Channel(s)** — exactly one preferred. "All channels" is almost
   always wrong because closers, voices, footage policies differ. If
   the user insists, refactor into shared core + per-channel config
   instead of one skill that branches.
4. **Trigger phrase** — what the user will type. Reserve it; grep
   `.claude/skills/` and `~/.claude/skills/` for collisions and refuse
   on conflict.
5. **Duplicate check** — list existing skills that overlap ≥60%. If
   anything overlaps, the default answer is "extend the existing skill"
   (add a `--variant` flag, branch on a config field). Only create a
   new skill if the schema or curatorial rule is incompatible (the
   `/make-rivalry-recap` vs `/make-ranking` split is the canonical
   precedent).

Output of stage 1: a 4-line spec.

```
bucket: make
format: Shorts (50-60s)
channel: sportstoriesanimated
trigger: /make-tactics-breakdown
extends: /make-script (variant) | new
```

### Stage 2 — Architect (efficiency scout + integration plan)

This is the heuristic-enforcement stage. Walk the 51 heuristics out
loud (compactly — one line each, not a wall of prose) against the
locked spec. Flag every heuristic this skill will need a code path for.

**Always run the engineering-efficiency scout (heuristic #51) FIRST.**
Before adding anything new, audit:

- **Pipeline reuse** — which of `pipeline/audio.py`,
  `pipeline/captions.py`, `pipeline/compose.py`, `pipeline/footage.py`,
  `pipeline/images.py`, `pipeline/llm.py`, `pipeline/cross_engage.py`,
  `pipeline/x_upload.py` does this skill ride on? If you'd duplicate
  any of them, stop and reuse.
- **Existing skill reuse** — can you extend `/make-script` /
  `/make-ranking` / `/make-sports-doc` instead? If yes, do that and
  this whole skill becomes a 30-line config delta.
- **Existing helper script reuse** — check
  `scripts/<channel>/`, `historyrecapped/scripts/`,
  `sportstoriesanimated/scripts/` for helpers that already do the
  research / scrape / render step.
- **Cache reuse** — content-hashed image cache, F5-TTS-MLX singleton,
  whisper-mlx cache, vidlens collections — call out which the new
  skill must hit.
- **Redundancy to remove** — if the scout finds ≥2 skills doing the
  same step, propose extracting it into `pipeline/` (or
  `scripts/_shared/`) as a separate refactor PR. **Do not silently
  duplicate.** State the refactor explicitly so the user can approve
  or defer.
- **Schema extension over schema fork** — if the new skill's output
  is 80% the same JSON as an existing skill, extend the existing
  schema with optional fields rather than forking a parallel schema.

Then load channel context:

```bash
cat <channel>/learnings/channel.md
cat <channel>/config.yaml
ls <channel>/learnings/
```

Pull out: closer pattern, banned phrasings, TTS provider + voice ID,
aspect, duration band, footage policy. These become **literal strings
in the new skill's SKILL.md** — never paraphrased, never inferred.

Output of stage 2: an integration plan.

```
reuse:
  - pipeline/audio.py (F5-TTS-MLX path)
  - sportstoriesanimated/scripts/research_rivalry.py (rename helper)
  - <channel>/cast/<slug>.json convention
extend:
  - pipeline/footage.py: add covers_full_rank span attach (Path A)
new:
  - sportstoriesanimated/scripts/render_tactics.py (only if no existing
    renderer fits; justify in the doc)
inherits_from_channel:
  closer: "LIKE if you were there. COMMENT next rivalry to recap."
  voice_id: <from config.yaml>
  aspect: 9:16
  banned: ["smash subscribe", "vote in comments", verdict acronyms]
  footage_policy: real-broadcast-only, no AI imagery
quality_gates:
  - /critique-audio before image gen
  - banned-phrase scan
  - source-fidelity check
  - length budget 50-60s
efficiency_wins (heuristic #51):
  - <X> already in research_rivalry.py — reuse, don't fork
  - extract <Y> from /make-ranking + /make-rivalry-recap into pipeline/
    as shared helper (separate PR)
  - dead path in compose._wipe_stale_per_beat_artefacts noticed; flag
    for cleanup
```

### Stage 3 — Author (write the skill files)

Write three things:

1. **`<.claude/skills/<name>/SKILL.md>`** — the new skill, following
   the canonical structure below.
2. **`<.claude/skills/<name>/learnings/`** — empty dir + a
   `_index.md` stub. The skill's self-learning loop appends here.
3. **Pipeline / renderer touch-ups** identified in stage 2. Don't
   smuggle these in silently — list them in the handoff so the user
   approves.

Update memory + project docs per CLAUDE.md's dual-save rule:

- Memory entry under `~/.claude/projects/-Users-rohit-ytFactory/memory/`
  pointing at the new SKILL.md path.
- One-line pointer in `MEMORY.md` (the index).
- If the skill enforces a cross-channel rule, also write
  `docs/<topic>.md`.

#### Canonical structure for a generated skill

Every generated skill MUST follow this section order. Fill in or omit
sections by format, but never reorder:

```
---
name: <slug>
description: <1-3 sentences. Lead with the verb (Author / Critique /
Spin up). Name the channel(s). Name what gets produced (paths).
End with "Use when the user says …" trigger phrases. End with
"For X use /<other-skill>" disambiguation links.>
---

# /<slug> — <one-line tagline>

<2-4 sentence overview: what it does, who the user becomes (writer /
showrunner / critic), and what the output is.>

## How to run it

### 1. Confirm <inputs>
### 2. Hat 1 — <Researcher / Curator / etc>
### 3. Hat 2 — <Writer / Director / etc>
### 4. Output: <schema name> at <path>
### 5. Cast / character handling (if applicable)
### 6. Quality gates
### 7. Renderer handoff
### 8. Report back

## Important rules

- <inherited closer literal>
- <inherited banned phrasings>
- <format-specific contracts e.g. "Number five" rank-chip phrasing>
- Always use `.venv/bin/python` for helper commands.
- Never run stages 4-7 (TTS / image gen / ffmpeg) in this skill — that
  is the renderer's job.
- <skill-specific rules surfaced in stage 2>

## Learnings from prior runs

<empty on day 1; appended on every regression>

## Why this skill is separate from /<closest-existing-skill>

<one paragraph; if you can't justify it in one paragraph, you should
have extended the existing skill instead>
```

#### Self-learning loop — bake this in

Every generated skill MUST include this block in its `## How to run it`
sequence (usually as the last "Report back" subsection):

```
### 9. Self-learning hook

After the user runs /critique-audio or /critique-video on the output
of this skill:

1. If a regression is found, classify it:
   - ONE-OFF (typo in this script) → fix in the .json output, append
     a 1-line note to `<.claude/skills/<name>/learnings/_index.md>`.
   - CLASS-OF-BUG (every future run will hit this) → fix in
     `pipeline/<module>.py` OR in this SKILL.md prompt, then append
     a regression note to `learnings/<topic>.md` AND mirror to
     `<channel>/learnings/<topic>.md` per CLAUDE.md dual-save rule.
2. Update MEMORY.md index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate that blocks emit on detection.
```

This is non-optional. A skill without this block is a skill that
forgets its mistakes.

#### Quality-gate block — bake this in

Every generated `/make-*` skill MUST include a `## 6. Quality gates`
section that runs in this order BEFORE handing off to the renderer:

1. **Banned-phrase scan** — grep narration against the channel's
   `learnings/` for verbotens (verdict acronyms, "smash subscribe",
   "vote in comments", any closer-anti-pattern). Block on hit.
2. **Pronunciation pre-pass** — phonetic respelling for foreign /
   proper nouns. Run BEFORE TTS so Kokoro / F5-TTS doesn't mangle
   them.
3. **Length budget** — total runtime must fit the channel band
   (Shorts 50-60s, doc 20-30m, sleep 60-120m). Hard fail outside.
4. **Source-fidelity check** — every named claim traces to a dossier
   entry. Reject ungrounded claims.
5. **/critique-audio gate** — run BEFORE image gen. TTS bugs
   invalidate downstream work (memory:
   `feedback_critique_audio_before_image_gen.md`).
6. **ContentID scan** for any footage source — archive.org PD or paid
   stock only for HR long-form; broadcast clips OK for sports.
7. **Aspect-ratio + voice-ID match** against `config.yaml`.

Skills that skip these gates regress in the wild. Make them
mechanical, not advisory.

### Stage 4 — Handoff report

```
✓ wrote skill: .claude/skills/<name>/SKILL.md
✓ scaffolded learnings: .claude/skills/<name>/learnings/_index.md
✓ memory: ~/.claude/projects/.../memory/skill_<name>.md
✓ MEMORY.md updated

inherited from <channel>/:
  closer: "<literal>"
  voice_id: <id>
  aspect: <ratio>
  banned: [<list>]

reused (no new code):
  - pipeline/<x>.py
  - scripts/<channel>/<helper>.py

new code (require user review):
  - pipeline/<y>.py: <one-line change>
  - scripts/<channel>/render_<name>.py: <scope>

efficiency wins surfaced:
  - duplicate <step> in /A and /B — extract to pipeline/<z>.py (separate PR)
  - dead path noted: <file>:<line>

trigger: /<name> "<example phrasing>"
next: try the trigger; first run will populate learnings/.
```

---

## Important rules

- **Never write a skill before stage 1 is locked.** A vague intent
  produces a vague skill. Force the 4-line spec.
- **Never duplicate an existing skill.** If overlap ≥60%, extend
  instead. The bar for a new skill is "schema or curatorial rule is
  incompatible with the existing one."
- **Never paraphrase a channel's closer or banned-phrase rule.** Copy
  literals from `learnings/` verbatim into the new skill's SKILL.md.
- **Never invoke `pipeline/` re-implementations.** Reuse audio.py,
  captions.py, compose.py, footage.py, images.py, llm.py.
- **Never call the Anthropic SDK directly from a generated skill.**
  Pipeline shells out to `claude -p` via `pipeline/llm.py` (memory:
  `feedback_llm_via_claude_cli.md`).
- **Never bypass the website-first workflow** (memory:
  `project_workflow_website.md`). Skills produce JSON; the website
  triggers render + upload.
- **Always surface engineering-efficiency wins (heuristic #51).** If
  the scout finds duplication, dead paths, or a chance to extract
  shared logic, name it in the handoff. Don't silently duplicate.
- **Always bake in the self-learning hook + quality-gate block.**
  These are non-optional.
- **Always dual-save** per CLAUDE.md: project doc + memory entry.
- Never ask 20 questions up front — ≤4 stages, defaults from
  `config.yaml`, only ask when no default exists.
- Use `.venv/bin/python` for any helper commands the new skill calls.

## The 51 heuristics

Source of truth: `learnings/heuristics.md`. Stage 2 walks them. They
group into:

- **A. Intent classification** (1-7) — bucket, format, channel,
  trigger uniqueness, dedup, automation-vs-skill, success criteria.
- **B. Channel context** (8-14) — load `learnings/channel.md`,
  inherit closer / banned / TTS / aspect / duration / footage policy
  literals; refuse if channel dir missing.
- **C. Pipeline integration** (15-22) — map to existing stages, reuse
  audio/captions/compose/footage modules, F5-TTS singleton,
  content-hash cache, `-r 30` footage fps, `claude -p` LLM, standard
  output paths.
- **D. Output schema / contract** (23-30) — explicit JSON schema,
  beat shape matches channel, closer required (or
  `embedded_in_last_beat` flag), word budget per WPM, shot list for
  movie/doc, chapters ≥10 min, SEO bundle, source list.
- **E. Pre-render quality gates** (31-38) — `/critique-audio` first,
  pronunciation pre-pass, banned-phrase scan, profanity sanitize,
  source-fidelity, length budget, aspect match, ContentID scan.
- **F. Self-learning** (39-44) — append regressions to learnings/,
  carry `learnings_consulted:` frontmatter, class-of-bug
  classification, post-run postmortem, load own learnings on start,
  dual-save.
- **G. UX** (45-48) — ≤4 stages, defaults from config.yaml, confirm
  intent in 1 sentence, echo output path.
- **H. Anti-patterns** (49-50) — no website-first bypass, no silent
  swallowing of credit / quota errors.
- **#51 — engineering efficiency**: every run, scout for reuse /
  dedupe / simplification before adding code. Surface findings in
  the handoff.

## Why this skill exists

Without it, every new ytFactory skill drifts: closers diverge,
banned-phrase lists fall out of sync, pipeline primitives get
re-implemented inside skills, quality gates are forgotten, learnings
are written to memory but not to project docs, the same regression
gets fixed in a prompt three times instead of once in `pipeline/`.

`/make-skill` is the one place that knows about all 51 heuristics so
the next 50 skills don't have to.
