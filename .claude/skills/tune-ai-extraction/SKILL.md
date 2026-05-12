---
name: tune-ai-extraction
description: Audit and patch the AI/prompt extraction quality across the entire ytFactory pipeline — every LLM call site (rewrite / cast / prompts / critic / audio_critic / imitate), every generative service (image-gen, motion, TTS, ASR, footage match), every prompt + extraction lever — score them on the 30-axis matrix (10 prompt-quality / 10 extraction / 10 orchestration), surface prioritised gaps, patch through the existing descriptor registry + contract orchestrator (never parallel forks), regression-test, append a scorecard row so the skill learns from prior runs. Use when the user says "tune the AI", "make our prompts maximum extraction", "100% efficiency", "100% extraction", "100% instruction following", "audit AI quality", "expert prompter pass", "improve generations across the board", or after a critique batch surfaces multiple class-of-bug AI issues. For one-render audio review use /critique-audio. For one-render visual review use /critique-video. For authoring a NEW format use the relevant /make-* skill.
---

# /tune-ai-extraction — pipeline-wide AI quality audit + patch loop

You become a **senior AI engineer + expert prompter** doing a systematic
extraction-maximisation pass on the entire pipeline. Not a per-render
critique (that's `/critique-audio` / `/critique-video`); not a new
authoring path (that's `/make-*`); this is the meta layer that asks
"are we getting 100% out of every model invocation, every prompt,
every extraction lever?" and patches gaps in place.

You wear three hats: **Auditor → Patcher → Telemeter**.

The skill is opinionated about HOW to patch:

- Plumbing changes go through `pipeline/render/input_registry.py`
  (descriptor registry — the single source of truth for how form
  inputs flow into spec → cfg → prompts). NO parallel registries.
- LLM call sites that lack retry/validate get wrapped in the
  `pipeline/llm/orchestrator.py::run_stage` contract pattern —
  copying `pipeline/llm/contracts/rewrite_contract.py` shape.
- Image/TTS/ASR extraction levers (negative prompts, IP-Adapter,
  pronunciation dict, modulation, source-text alignment) land via
  named handlers in `pipeline/render/input_registry.py` —
  `register_apply_handler` / `register_prompt_patch` /
  `register_transform`.
- Every patch bumps the relevant cache fingerprint so stale outputs
  re-render.
- Every patch ships with a regression test.

---

## How to run it

### 1. Confirm scope

Ask ONE clarification if the user hasn't said. Otherwise default to
`scope: full`. Lock the spec in 4 lines and proceed:

```
scope: full | rewrite | cast | prompts | critic | images | tts | asr | footage
target_axes: prompt | extraction | orchestration | all   (default all)
fix_budget: max patches per run         (default 5 — bigger numbers
                                         tank reviewability)
mode: audit-only | audit-and-patch       (default audit-and-patch)
```

`scope=full` is the right default when the user hasn't been specific —
the audit takes ~60s and surfaces the prioritised list anyway.

### 2. Hat 1 — Auditor (delegate to explore agent)

Fire ONE explore-agent task with this prompt template (already
proven in the 2026-05-12 audit; reuse don't reinvent):

```
INVENTORY every AI/LLM call site + every generative service in the
pipeline. For each, score on three axes (10 each):
  A. PROMPT QUALITY — schema, few-shot pos+neg, constraints, retry,
     channel context, token budget, output-length constraints,
     self-check, model tier
  B. EXTRACTION QUALITY — style prefix, neg-prompt, character
     consistency, kit-lock, seed pinning, steps tuning, voice ref+text,
     pronunciation dict, speed+atempo, modulation, source-text
     alignment, cache fingerprint completeness
  C. ORCHESTRATION QUALITY — cast→prompts flow, dossier→TTS flow,
     ASR→beat boundaries, critic→re-render loop, fingerprint
     comprehensiveness, stage-output inspectability
For each call site, output:
  - score X/10 per axis
  - missing items
  - top fix (one-line, file:line)
Then prioritised TOP 20 gaps ordered by:
  1. Bugs / silent extraction loss (highest impact)
  2. Missing best-practices
  3. Polish
Plus cross-cutting gaps and per-channel inconsistencies.
File:line citations everywhere. NO code writing. ~5000 words.
```

Files the auditor MUST inspect (kept current — update when we add a
new AI call site):

- `pipeline/llm/rewrite.py` (short narration rewriter)
- `pipeline/llm/rewrite_long_form.py` (long-form sectioned rewriter)
- `pipeline/llm/cast.py` (character casting)
- `pipeline/llm/prompts.py` (per-beat image prompt authoring)
- `pipeline/llm/critic.py` + `pipeline/llm/audio_critic.py`
- `pipeline/llm/imitate.py` (clone-format analysis)
- `pipeline/llm/contracts/*.py` (orchestrator contracts)
- `pipeline/llm/script_check.py` + `script_lint.py` (validators)
- `pipeline/images.py` + `pipeline/images_cloudrun.py`
- `pipeline/audio.py` + `pipeline/tts/cloudrun.py`
- `pipeline/animation.py` (motion clips)
- `pipeline/beats.py` + `pipeline/align.py` (Whisper + alignment)
- `pipeline/footage/*.py` (clip retrieval / matching)
- `pipeline/render/long_form.py::build_image_panels_video`

Wait for the audit. Read it carefully.

### 3. Hat 2 — Triage (score deltas + pick patches)

Load the prior scorecard from
`.claude/skills/tune-ai-extraction/learnings/scorecard.jsonl` (every
prior run appended one row per patch with before/after axis scores).
For each gap in the audit:

1. Has this gap been fixed before? → check scorecard. If yes and the
   audit STILL flags it, it's a regression — escalate to a
   class-of-bug fix in `pipeline/` instead of patching in place again.
2. Is the gap class-of-bug (every future render hits it) or one-off
   (specific channel/niche)?
3. Estimate cost (lines of code + risk) vs payoff (axis score
   improvement × number of affected renders).

Pick at most `fix_budget` patches (default 5). Order them by payoff/cost.

For each pick, present a one-liner to the user before patching:

```
PATCH 1/5 · long-form rewriter has no contract retry (audit AXIS A: 6/10)
  fix:    wrap rewrite_long_form() in run_stage(LongFormRewriteContract,...)
  files:  pipeline/llm/contracts/long_form_rewrite_contract.py (NEW),
          pipeline/llm/rewrite_long_form.py (call site)
  cost:   ~80 LoC + 2 contract tests
  payoff: +2 axis points, regen on schema/length/banned-phrase failure,
          unblocks audit-critic auto-feedback (top gap #6)
```

### 4. Hat 3 — Patcher (apply through the descriptor registry / contract pattern)

For each picked patch, the implementation route is RESTRICTED to one
of these patterns. NEVER a one-off if/elif chain or a parallel
registry — the entire reason for this skill is to prevent the
2026-05-12 dead-code drift from recurring.

#### Pattern A — input that affects a prompt → register a PromptPatch

```python
# pipeline/render/input_registry.py
@register_prompt_patch("audio_mode_branch")
def _audio_mode_patch(value, spec) -> PromptPatch:
    if value == "song":
        return PromptPatch(
            context_lines=[...],
            craft_rules=[...],
            schema_mode="lyrics",
        )
    return PromptPatch()
```

Then point the descriptor's `prompt_patch_fn="audio_mode_branch"` and
the rewriter calls `prompt_patches_for(spec)` and threads it into the
prompt template. Adding a new prompt-aware input is one descriptor +
one named handler, never editing 4 prompt templates.

#### Pattern B — input that affects a cfg dict → declare cfg_targets

```python
# pipeline/schemas/customization.py
def _voice_field(...):
    return CustomizationField(
        ...,
        spec_field="voice_id",
        cfg_targets=[
            CfgTarget(path=["tts_voice"]),
            CfgTarget(path=["long_form", "tts_voice"]),
        ],
        apply_handler="apply_voice_with_cloud_carveout",
    )
```

NOT an inline branch in `_apply_form_overrides`. The descriptor
registry walks targets automatically.

#### Pattern C — LLM call site that lacks retry → wrap in a Contract

Mirror `pipeline/llm/contracts/rewrite_contract.py`:

```python
class LongFormRewriteContract(StageContract):
    def gather_constraints(self, ctx: StageContext) -> list[Constraint]: ...
    def build_prompt(self, ctx, constraints) -> str: ...
    def output_schema(self) -> dict: ...
    def validate(self, ctx, raw) -> list[Constraint]: ...
    def regen_prompt(self, ctx, raw, violations) -> str: ...
```

Then call site uses `run_stage(LongFormRewriteContract(), ctx,
llm_call=llm.call_claude_cli)`. Free retry + validation + regen.

#### Pattern D — extraction lever (image neg-prompt, TTS pronunciation,
ASR alignment) → land in the consuming module + bump fingerprint

```python
# pipeline/images.py — new neg-prompt class:
NEGATIVE_PROMPTS = (
    "extra fingers, mutated hands, deformed face, extra limbs, "
    "watermark, text artifact, double exposure, ..."
)
# Bump cache fingerprint version:
_PROMPT_VERSION = 4  # was 3 — invalidate every cached image
```

The fingerprint bump is mandatory: an extraction lever change that
doesn't bust cache will silently NOT improve any cached render.

#### Forbidden paths

- Editing prompts inline without the descriptor registry / contract
  pattern. (Drifts immediately.)
- Adding a new `if x == "song": ...` branch anywhere. (Use
  `prompt_patch_fn` instead.)
- Bypassing `pipeline/llm/cli.py::call_claude_cli`. (Skips the
  dispatcher's structured-output + tracing layer.)
- Changing extraction levers without bumping the fingerprint.

### 5. Quality gates (BEFORE marking patch done)

Mechanical checks per patch:

1. **Contract coverage** — `grep -L "run_stage\|StageContract" pipeline/llm/<file>.py`
   for every LLM call site MUST be empty (every call goes through a
   contract). Run after every Pattern-C patch.
2. **Cache fingerprint freshness** — for any extraction-lever change,
   verify the relevant `_FP_VERSION` / `_PROMPT_VERSION` constant
   bumped. Otherwise the cache silently negates the improvement.
3. **Schema round-trip** — every new prompt's JSON schema must
   round-trip through the existing validator (e.g. `script_lint`,
   `script_check`). New schema fields can't break old renders.
4. **Regression test** — every patch ships a unit test in
   `tests/test_tune_ai_<patch_id>.py`. Test must name the gap
   (docstring), the before/after scores, and assert the new behavior.
5. **Descriptor-registry drift** — `python -c "from pipeline.render
   import input_registry; print(len(input_registry._TRANSFORMS),
   len(input_registry._APPLY_HANDLERS), len(input_registry._PROMPT_PATCHES))"`
   counts MUST match what the audit recorded. A new handler that
   doesn't show up means a typo in the registration decorator.
6. **Render dry-run** — for any patch touching the long-form path,
   stub-mode dry-run via `tests/test_renderer_matrix.py` to confirm
   the spec→cfg→prompt chain still resolves on every channel × niche
   combo. Failure here means the patch broke an unrelated combo.

Hard fail any gate; the patch doesn't ship until green.

### 6. Hat 3 — Telemeter (append scorecard row + learning)

After every patch (whether it green-gates or not), append one JSON-
lines row to
`.claude/skills/tune-ai-extraction/learnings/scorecard.jsonl`:

```json
{
  "ts": "2026-05-12T...",
  "patch_id": "lf-rewriter-contract",
  "scope": "rewrite_long_form",
  "audit_run": "<audit-agent-id>",
  "axis_before": {"prompt": 8, "extraction": 5, "orchestration": 6},
  "axis_after":  {"prompt": 9, "extraction": 7, "orchestration": 9},
  "files_changed": ["pipeline/llm/contracts/long_form_rewrite_contract.py", ...],
  "fingerprint_bumped": ["_PROMPT_VERSION"],
  "regression_test": "tests/test_tune_ai_lf_contract.py",
  "ship_status": "green" | "gate-failed:<which>" | "user-rejected",
  "regression_count": 0,
  "notes": "..."
}
```

Then update `learnings/_index.md` with a one-line pointer per shipped
patch (kept newest-on-top, max 50 entries — older ones move to
`learnings/archive/`).

When a CLASS-OF-BUG patch ships (i.e. a structural fix in `pipeline/`
that prevents future regressions), ALSO write
`docs/<topic>.md` per CLAUDE.md's dual-save rule and link from
`MEMORY.md`.

When the SAME gap appears in three consecutive audit runs after being
"patched", escalate: the prior patches were band-aids. Open a fresh
plan.md with a deeper architectural fix and STOP appending point
patches.

### 7. Self-learning hook

After the user runs the next render + `/critique-audio` /
`/critique-video` on it:

1. If the critique surfaces a NEW class-of-bug (one that wasn't
   caught by the prior audit), log it in
   `learnings/missed_classes.md` with file:line of the call site
   that the audit's heuristic should have flagged. The audit prompt
   in step 2 gets a new line added to its score-axis list. Audit
   prompts are versioned — bump the prompt version comment when
   amending.
2. If the critique surfaces a REGRESSION (a patch made some axis
   worse), pull that patch's row from scorecard.jsonl, set
   `regression_count: <prior>+1`, and add a new row that REVERTS
   plus a learnings note. After 2 regressions on the same patch
   pattern, ban it (audit prompt MUST flag any future application
   of that pattern).

Pre-render quality gate (escalation rule): if a class-of-bug fires 2×
in scorecard.jsonl without being patched, the next audit MUST mark
it `severity: blocker` and the user MUST patch before proceeding.

### 8. Report back

```
✓ audit done — N gaps surfaced, top 5 patched, ? deferred
✓ patches:
    1. <patch_id> — axis +X, gates green, ships
    2. <patch_id> — axis +Y, gates green, ships
    ...
✓ scorecard: .claude/skills/tune-ai-extraction/learnings/scorecard.jsonl (+5 rows)
✓ learnings/_index.md updated
✓ memory + MEMORY.md updated for class-of-bug patches
✗ deferred (with reason): N gaps
  - <gap>: <reason — e.g. needs IP-Adapter rewire, separate PR>

next: re-run /tune-ai-extraction after the next render batch to
      verify no patch regressed and the deferred gaps stayed put.
```

---

## Important rules

- **Never patch a gap inline without the descriptor registry / contract
  pattern.** The whole reason for this skill is that hand-coded if/elif
  branches drift and become dead code (2026-05-12 incident). If a gap
  doesn't fit Patterns A-D, stop and surface it for an architecture
  decision rather than smuggling in a one-off.
- **Never bypass `pipeline/llm/cli.py::call_claude_cli`.** Every LLM
  invocation goes through the dispatcher (Azure / Anthropic SDK / CLI
  routing + structured-output + tracing).
- **Never edit prompt strings without bumping the cache fingerprint.**
  Caches are content-hashed but the fingerprint must include every
  input that affects output, including prompt VERSION. Forgetting this
  is the #1 silent-no-op pattern.
- **Never ship a patch without a regression test.** A patch without a
  test is a future regression no one will catch until a render breaks.
- **Always run the audit FIRST.** Even when the user names a specific
  gap. The audit grounds the patch in the current scorecard and flags
  related issues you'd otherwise miss.
- **Always reuse the descriptor registry's named-handler registries**
  (`_TRANSFORMS` / `_APPLY_HANDLERS` / `_PROMPT_PATCHES`) — do not
  introduce a parallel registry. Naming convention:
  `<input>_<intent>` (e.g. `audio_mode_branch`,
  `apply_voice_with_cloud_carveout`).
- **Always dual-save** for class-of-bug patches per CLAUDE.md
  (project doc + memory entry).
- **Always include a fingerprint-bump when changing extraction levers.**
  Even if the prompt body looks identical, a logic change downstream
  (e.g. new neg-prompt, new pronunciation transform) means cached
  outputs no longer match — bump.
- **Never patch the SAME gap twice with the same pattern.** Two
  regressions on one pattern is a signal the abstraction is wrong;
  escalate to architecture, not yet-another-patch.
- **Use `.venv/bin/python`** for any helper invocation.
- **Use the explore agent** for the audit step (proven in 2026-05-12
  pass — `~5000 words in ~60s`, comprehensive coverage). Do NOT do the
  audit manually with grep — you'll miss things.
- **Use the rubber-duck agent** for any patch that touches >2 files OR
  introduces a new contract / new descriptor handler. Cheap insurance.

## Learnings from prior runs

(Empty on day 1. Each run appends a row to
`learnings/scorecard.jsonl` and a one-liner to `learnings/_index.md`.
Class-of-bug fixes also leave a `learnings/<topic>.md` and a
`docs/<topic>.md` pointer.)

## Why this skill is separate from /critique-audio /critique-video

`/critique-audio` and `/critique-video` look at ONE render's output
and report what was wrong with that specific mp4. They're per-render,
post-hoc, descriptive.

`/tune-ai-extraction` looks at the PIPELINE — every prompt, every
model invocation, every extraction lever — and asks "are we wired to
get max quality on EVERY future render?". It's pre-render, structural,
prescriptive.

A `/critique-video` finding like "beat 17 has a deformed hand" is a
per-render symptom. The corresponding `/tune-ai-extraction` patch is
"add `extra fingers, mutated hands` to every channel's neg-prompt
class and bump `_PROMPT_VERSION` so cached images regenerate".

The two skills feed each other: critique findings populate
`learnings/missed_classes.md` which sharpen the next audit's heuristic
list; audit patches show up as quality lifts on the next critique
batch. Run them in alternation: critique batch → tune-ai-extraction
pass → next render batch.

---

## Cloud pre-render hook (mandatory for any patch that ships a render
config change)

Before any patch that touches `pipeline/render/long_form.py`,
`pipeline/render/shorts.py`, or `cloud/render-worker-v2/entrypoint.py`,
do the routing assertion documented in
[`docs/cloud_prerender_hook.md`](/Users/rohit/ytFactory/docs/cloud_prerender_hook.md):
read the affected channel `config.yaml` and assert `tts_provider` +
`image_provider` start with `cloudrun_` (with the documented
local-only carve-outs).

Patches that change `_PROMPT_VERSION` / cache fingerprints DO NOT need
the routing assertion (they're prompt-side).
