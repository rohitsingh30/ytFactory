# Bug report — translated from critiques

**Source critique:** `data/critiques/tifu-my-20f-girlfriend-of-two-years-told-me-the-mu-24c5887a.md`
**Render:** `mystoriesanimated/reddit_tifu` — TIFU "my girlfriend said my bedroom playlist kills the mood"
**Date:** 2026-05-23

## Batch summary

- **Critiques analyzed:** 1 (this is N=1; class-of-bug claims that aren't grounded in repo-wide architecture are flagged INVESTIGATION-NEEDED for cross-render confirmation)
- **Highest-confidence root cause:** `pipeline/llm/cast_router.py::route_character_description()` exists but is not called from the renderer. Every beat receives the singleton narrator description. Stories with two named characters can never show the right one in the right beat. This is a code-grounded systemic failure, not a one-off prompt issue.
- **Top leverage opportunities (ranked):**
  1. Wire `cast_router.route_character_description()` into `ai_beat_slideshow._gen_one_beat()` — kills an entire class of POV/character-confusion bugs across every multi-character variant.
  2. Add a `narrator.is_first_person` + `narrator.gender` lock to the cast schema with channel/variant override — closes the cast-gender-inference drift that compounds the singleton bug.
  3. Promote the closer to a structurally-distinct beat with its own framing contract — fixes flat CTAs across all AITA/TIFU variants, not just this one.

---

# CLASS-OF-BUG FINDINGS

## CLASS-OF-BUG #1 — Singleton character description ignores beat content

### Viewer failure
First-person story told from a male narrator's POV shows only the girlfriend on screen for 33 of 46 seconds. When the boyfriend finally appears, the viewer has spent 35 seconds with no anchor for "who is the I."

### Evidence
- Critique 0:00–0:05 ("Whose story is this?"): "narration is first-person male, image is a single female character… I quietly assume the woman on screen IS the narrator."
- Critique 0:08: "Bluetooth speaker moment" — visually the girlfriend silencing music; with no character identification, reads as "narrator silencing her own music."
- Frames sampled at 1, 8, 13, 18, 23, 28, 36, 40, 45s — every panel where ONE character is on screen shows the same yellow-shirted female.

### Failure class
`narration_visual_desync` + `cast_consistency_failure`

### Recurrence
N=1 in critiques folder, but the architecture is repo-wide: every multi-character `mystoriesanimated` story routes through the same `_gen_one_beat()` path with no per-beat character routing. **Affects every TIFU/AITA story with ≥2 named characters.** Cross-render confirmation needed but architectural certainty is high.

### Likely owner
`pipeline/render/visualize/ai_beat_slideshow.py` (consumer) + `pipeline/llm/cast_router.py` (producer, unused)

### Root-cause hypothesis
`cast_router.route_character_description(beat_text, scene, key_visual, narrator_desc, supporting)` was authored to dispatch the correct character per beat, but the renderer never calls it. `spec_enrich._populate_character_description()` writes `cast["narrator"]["description"]` into `spec.extra["character_description"]` as a SINGLETON, and `ai_beat_slideshow._gen_one_beat()` prepends that same string to every beat's image prompt. The `spec.extra["character_descriptions"]` array (all characters) is assembled by `spec_enrich._populate_character_descriptions()` and then never read downstream — it's dead data.

The diffusion model therefore receives "narrator-spec, [scene describing girlfriend]" for a beat that should show the girlfriend, and either (a) generates the narrator, or (b) the cast LLM authored the narrator description ambiguously enough that diffusion converges on whichever character the scene tokens describe — in this render, always the girlfriend.

### Confidence
0.85 on the wiring claim (Explore agent reported the dead function with line numbers). 0.6 on which character actually wins under the singleton — needs `cast.json` from the render's GCS artifact to confirm whether the narrator was authored male-or-female-or-neutral.

### Minimal fix surface
Two-line wire-up + one new helper:
1. In `ai_beat_slideshow._gen_one_beat()` (around `pipeline/render/visualize/ai_beat_slideshow.py:414-421`), replace the static `character_description` lookup with a call to `cast_router.route_character_description(beat, narrator_desc, supporting_list)`.
2. Surface `cast["supporting"]` through `spec.extra` (`pipeline/render/spec_enrich.py:273-350` already populates it as a list — just consume it).
3. Add a regression test: a 6-beat story where beats 2 and 4 explicitly name a supporting character; assert those beats' image prompts include the supporting character's description, not the narrator's.

### File:function targets
- **Add call:** `pipeline/render/visualize/ai_beat_slideshow.py::_gen_one_beat` (~line 414)
- **Consume:** `pipeline/llm/cast_router.py::route_character_description` (already exists, lines 55–117)
- **Read-through:** `pipeline/render/spec_enrich.py::_populate_character_descriptions` (lines 273–350; already builds the array, just unused)
- **Regression test:** `tests/render/visualize/test_ai_beat_slideshow_cast_routing.py` (new)

### Blast radius
All multi-character renders, all variants, all channels that use `ai_beat_slideshow`. Highest concentration: `mystoriesanimated/reddit_tifu`, `mystoriesanimated/aita_*`, and any `hindutavaanimated` katha with multiple deities/sages on stage.

### Cost
S (≤50 LoC including the test)

### Investigation needed?
Yes — pull `gs://ytfactory-prod-v3-artifacts/jobs/<job_id>/cast.json` for job `24c5887a...` to confirm what gender/spec the narrator was authored as. This determines whether the singleton bug surfaces as (a) "girlfriend everywhere" or (b) "narrator everywhere," but either way the dispatch is missing.

### Why this matters
Any story with "my [partner/parent/friend/coworker] did X" — i.e., the entire MyStoriesAnimated catalogue — currently cannot show the right character at the right moment. Wiring this kills a category of viewer confusion that exists in every render of that channel.

---

## CLASS-OF-BUG #2 — Cast lacks first-person-narrator marker

### Viewer failure
Even when the right cast member exists, the system has no way to enforce "the narrator is male" from the YAML. The viewer ends up with a female-presenting narrator silhouette over first-person-male narration, or vice versa. POV ambiguity compounds with #1.

### Evidence
- `pipeline/channels/mystoriesanimated.yaml:59` channel-level default narrator: "29-year-old **woman**" (locked aesthetic for AITA, leaks into TIFU).
- `pipeline/variants/mystoriesanimated/tifu.yaml:34` variant `character_description`: "24-year-old **person**, shoulder-length brown hair" — gender-neutral, no override.
- `pipeline/variants/mystoriesanimated/tifu.yaml:6-10` voice ref `theo.wav` (male) lost in recovery wipe; current renders fall back to `sarah.wav` (female). The audio in this render is therefore female-presenting for a male-POV story.
- Cast schema in `pipeline/llm/cast.py:17-41` has `"narrator"` and `"supporting"` keys but no `is_first_person`, `narrator_gender`, or `protagonist_pronouns` flag.

### Failure class
`genre_communication_failure` + `cast_consistency_failure`

### Recurrence
N=1 in critiques, but channel/variant config makes this **deterministic** for every TIFU render until the voice ref is restored and a gender lock added.

### Likely owner
`pipeline/llm/cast.py` (schema) + `pipeline/variants/mystoriesanimated/tifu.yaml` (config)

### Root-cause hypothesis
TIFU stories on Reddit overwhelmingly follow a "[gender][age] OP" convention in the title — `20F`, `25M`, etc. The cast author currently has to infer gender from the story body, and channel-level defaults silently win when inference is weak. The variant YAML has no slot for "this variant's narrator is always male / always 25" because the schema doesn't expose one.

### Confidence
0.8 on the schema gap (verified from Explore agent's read of `cast.py:17-41`). 0.7 on the audio-voice mismatch causing additional confusion — needs ear-level audio check (not just whisper transcript) to confirm voice gender as heard.

### Minimal fix surface
1. Add `narrator.gender`, `narrator.age_range`, `narrator.pronouns`, `narrator.is_first_person: bool` fields to the cast schema in `pipeline/llm/cast.py::CAST_SCHEMA`.
2. Allow YAML overrides at `variants/<channel>/<variant>.yaml` under a new `narrator:` key.
3. In `pipeline/variants/mystoriesanimated/tifu.yaml` lock `narrator.gender: inferred_from_title` (since Reddit TIFU titles carry it) with a fallback to `male`.
4. Restore `theo.wav` (or pick a permanent male English ref) and re-pin in the YAML.

### File:function targets
- `pipeline/llm/cast.py::CAST_SCHEMA` (schema extension)
- `pipeline/llm/cast.py::author_cast` (read YAML overrides)
- `pipeline/variants/mystoriesanimated/tifu.yaml` (config)
- `pipeline/variants/mystoriesanimated/aita_*.yaml` (mirror the pattern)

### Blast radius
Every variant where the narrator demographic is known up-front from the source (TIFU title, AITA title, today-in-history protagonist). Doesn't help wiki-oddities (no protagonist) but covers the bulk of the channel.

### Cost
S (schema + 2 YAML edits + voice-ref restoration). Audio ref restoration may be M if it requires re-cloning.

### Investigation needed?
Yes — confirm by ear (not whisper) whether the rendered narration in `24c5887a` is female-voiced. If yes, **this finding becomes critical** because audio + visual + script are all conflicting on narrator gender, and the viewer is being asked to reconcile three contradictions in 46 seconds.

### Why this matters
The pipeline cannot promise "this video shows the protagonist" if the protagonist is silently re-cast every render by an unconstrained LLM. Locking the protagonist demographic at YAML time is what makes the channel feel like a *channel* and not a random anthology.

---

## CLASS-OF-BUG #3 — Closer/CTA is structurally identical to other beats

### Viewer failure
The AITA-style CTA ("Was I wrong? What would you have done?") lands on the same medium-shot, same character, same emotional register as every prior beat. The most engagement-critical line of the video is the flattest visual.

### Evidence
- Critique 0:41–0:46: "lands on her sitting back on the bed, hands on the mattress, soft pose, looking off-camera. Same emotional register as every other panel."
- `pipeline/render/short_engine.py:522-546` (per Explore agent): closer is just another panel with a `closer_format` text overlay, no special camera/framing/expression rule.
- `pipeline/variants/mystoriesanimated/tifu.yaml:66-73`: declares `closer_format` text + `closer_hold_s` only — no `closer_framing`, `closer_camera`, `closer_expression` keys.

### Failure class
`visual_payoff_failure` + `shot_planning_failure`

### Recurrence
N=1 critique, but architectural: every MyStoriesAnimated CTA goes through the same path. **Likely recurs in every AITA/TIFU render.**

### Likely owner
`pipeline/render/short_engine.py` + variant YAML schema

### Root-cause hypothesis
The closer was treated as a text-overlay problem ("show this CTA string") rather than a panel-grammar problem ("the CTA beat must break the fourth wall and address the viewer"). There is no concept of a "closer beat type" with rules like `camera: direct_address`, `expression: questioning`, `framing: closeup`, `prop_optional: phone_with_comment_field`.

### Confidence
0.75 — grounded in critique evidence and the absence of closer-specific fields in the YAML schema.

### Minimal fix surface
1. Extend variant YAML to allow `closer.framing`, `closer.camera`, `closer.expression`, `closer.prop` fields under the existing `closer:` block.
2. In the beat author (`pipeline/llm/prompts.py` per Explore), when authoring the closer beat, inject those fields into the prompt verbatim instead of letting the LLM choose framing.
3. Add a single-axis test: render a stub closer and assert the prompt contains "direct address" / "looking at camera" / equivalent.

### File:function targets
- `pipeline/llm/prompts.py` (closer-beat author)
- `pipeline/render/short_engine.py:522-546` (consume new fields)
- `pipeline/variants/mystoriesanimated/*.yaml` (declare framing per variant)

### Blast radius
All AITA/TIFU/wiki-oddities variants in MyStoriesAnimated. Also `cosmosdecoded` shorts which end with rhetorical questions, and `hindutavaanimated` shorts which end with "subscribe for more katha."

### Cost
S–M (schema extension + planner change + per-variant YAML)

### Investigation needed?
No — this is straightforward planner-grammar extension.

### Why this matters
Comments + likes are the only thing the algorithm reads on a Short. A muted CTA leaves engagement on the floor on every render.

---

## CLASS-OF-BUG #4 — Comedic peaks have no visual-metaphor grammar

### Viewer failure
"She feels like she's in a video game boss fight / like we're battling a dragon" — the funniest line in the script — lands on a character shrugging on a bed. No HP bar, no dragon silhouette, no controller. The comedic peak is the flattest visual moment.

### Evidence
- Critique 0:24–0:29: "begging for a visual gag… instead we get the same character on the same bed with her hands raised in a shrug."
- Frame at 28s (the "video" beat): character holding a phone — literal interpretation of "video game" tokens lands on "person holding video device." Diffusion took the wrong sense of the word.

### Failure class
`visual_payoff_failure` + `prompt_entropy_failure`

### Recurrence
N=1 critique. Likely surfaces wherever the LLM script contains figurative language (similes, metaphors, jokes). For MyStoriesAnimated specifically, this is **every TIFU punchline** — TIFU stories are built on absurd analogies.

### Likely owner
`pipeline/llm/prompts.py` (beat author) + `pipeline/llm/cast.py` (no "figurative_register" classification)

### Root-cause hypothesis
The beat author treats every line of narration as literal. There's no step that says "this line is figurative — generate a metaphor visualization instead of a literal scene." So "battling a dragon" becomes a literal bedroom scene, not a tongue-in-cheek dragon-fight visualization.

### Confidence
0.6 — strong on critique evidence, weak on whether the existing beat author already has a hook for this and it's just not being used. Needs architecture read of `pipeline/llm/prompts.py` to confirm. **INVESTIGATION-NEEDED.**

### Minimal fix surface
Add a `beat.register: literal | metaphor | reaction | establishing` enum to the beat schema; when authoring beat prompts the LLM is asked to classify each line and emit a metaphor-visualization prompt when applicable. Or: keep the schema flat but add to the prompt "if the narration contains a vivid simile, visualize the simile, not the literal scene."

### File:function targets
- `pipeline/llm/prompts.py::author_beat_prompts` (likely)
- New regression test: feed a beat containing "like a dragon" and assert the output prompt mentions "dragon."

### Blast radius
Every story-driven channel: MyStoriesAnimated (all variants), HistoryRecapped, HindutavaAnimated kathas. Less impact on CosmosDecoded (literal physics) or RhymeTimeJunction (already metaphor-rich by genre).

### Cost
S (prompt-author tweak) to M (schema + classifier)

### Investigation needed?
Yes — read `pipeline/llm/prompts.py` to confirm there's no existing metaphor handling before authoring a new mechanism.

### Why this matters
On a Short, the comedic spike IS the share-trigger. Burying it in a literal frame turns a shareable video into a forgettable one.

---

## CLASS-OF-BUG #5 — Shot rotation may not be reaching the renderer

### Viewer failure
All 12 sampled frames are roughly medium-shot, eye-level, character centered, identical bedroom. No close-up, no wide, no insert, no environmental change.

### Evidence
- Critique "The whole 46s — visuals don't escalate": "same bedroom, same yellow shirt, same medium-wide framing, same eye-level camera across all 12 panels."
- Explore agent reports `pipeline/images/prompt_refiner.py:95-115` contains `SHOT_ROTATION` cycling 9 shot types by `beat_index % 9`. **In theory, shot variety should exist.**

### Failure class
`visual_novelty_collapse` — but possibly upstream of the renderer (diffusion not following shot tokens) or downstream of beat-authoring (shot tokens stripped).

### Recurrence
N=1 critique. Architectural ambiguity: rotation logic exists; observed frames suggest it isn't reaching the diffusion prompt or isn't being honored.

### Likely owner
Unclear — could be `prompt_refiner` (tokens not injected) OR `images.build_full_prompt` (tokens overridden) OR the diffusion model itself (z-image-turbo ignoring shot tokens under high subject-prompt pressure).

### Root-cause hypothesis
One of:
- (a) `SHOT_ROTATION` tokens are present in the prompt but z-image-turbo gives them low weight relative to the singleton character description (which is much longer and earlier in the prompt).
- (b) `SHOT_ROTATION` is not being applied at all for this variant (maybe gated on a flag this variant doesn't set).
- (c) Shot rotation is applied but always lands on similar shots due to a degenerate mod-9 schedule given this story's beat count.

### Confidence
0.4 — high confidence the symptom is real, low confidence on which of (a)/(b)/(c) is operative.

### Minimal fix surface
Cannot prescribe before tracing. **INVESTIGATION-NEEDED.**

### File:function targets
- `pipeline/images/prompt_refiner.py:95-115` (where SHOT_ROTATION is defined)
- `pipeline/images/images.py::build_full_prompt` (line 281–380 per Explore)
- The rendered image prompts for THIS job (pull `prompts.json` from `gs://ytfactory-prod-v3-artifacts/jobs/<job_id>/`)

### Blast radius
All channels using `ai_beat_slideshow`. If the bug is (a), it's diffusion-model-wide and high-leverage. If (b), it's a one-flag fix. If (c), it's just bad luck.

### Cost
Investigation: S. Fix: unknown.

### Investigation needed?
Yes, primary action: pull `prompts.json` for this render and confirm whether shot tokens (close-up, wide, OTS, etc.) are present and varied. If yes and renders still look identical → diffusion-prompt-weight problem. If no → rotation isn't being applied.

### Why this matters
If shot rotation works in theory but never in practice, that's an entire pipeline feature silently failing on every render across every channel.

---

# ONE-OFF FINDINGS

- **Audio voice gender may be female for this render** — variant YAML notes `theo.wav` (male) was lost; fallback is `sarah.wav` (female). Critique inferred "male voice" from script content, not from ear-level verification. If the rendered audio IS female-voiced, this single render has audio/visual/script all conflicting on narrator gender. → **Per-render fix:** restore `theo.wav` (or accept a male voice clone), re-render the slug. **Class-of-bug coverage:** captured in #2.
- **The Bluetooth speaker beat (0:08) is the strongest visual in the video** — character interacts with a story-relevant prop. If the protagonist had been visible, this beat would have been the de-facto hook. Worth flagging for the planner: prop-interaction beats outperform expression-only beats.

---

# CROSS-CUTTING OBSERVATIONS

1. **Cast roster is assembled then mostly discarded.** `spec_enrich._populate_character_descriptions` builds the full character list. `_populate_character_description` (singular) writes the narrator into the singleton field that's actually read. The plural list is dead data downstream. This is a strong "the system was designed to do the right thing then half-wired" smell — `cast_router.py` confirms the same pattern.

2. **YAML cannot lock things the channel reliably knows.** Reddit TIFU titles literally encode narrator demographics (`20F`, `25M`). The variant YAML has no slot to declare "this variant's narrator is male." The pipeline keeps re-inferring something that the channel could just assert.

3. **No "beat type" taxonomy.** Every beat is treated identically. There is no `establishing` / `reaction` / `metaphor` / `cta` distinction at the planner level. This is why closers feel like normal beats, why metaphors get visualized literally, and why coverage doesn't escalate.

4. **Stateless beat authoring.** Each beat's prompt is authored independently. There is no "previous shot history," no "current emotional intensity," no "this is the comedic peak." The pipeline does N independent panels, then concatenates them. Composition memory is the missing abstraction.

5. **The renderer trusts the LLM where it should constrain the LLM.** Camera, framing, character identity, register — all left to the LLM to infer per beat. Where the channel knows the answer, it should declare it.

---

# ROUTING

## SYSTEMIC ENGINEERING (file:function → owner-area → cost)

- **CLASS-OF-BUG #1 — wire cast routing per beat** → `pipeline/render/visualize/ai_beat_slideshow.py::_gen_one_beat` (call `cast_router.route_character_description`) → **S** — *highest leverage, kills POV/character confusion across every multi-character render*
- **CLASS-OF-BUG #2 — cast schema + variant narrator lock** → `pipeline/llm/cast.py::CAST_SCHEMA` + `pipeline/variants/mystoriesanimated/tifu.yaml` → **S**
- **CLASS-OF-BUG #3 — closer-beat framing grammar** → `pipeline/llm/prompts.py` + variant YAML → **S–M**
- **CLASS-OF-BUG #4 — figurative-register handling** → `pipeline/llm/prompts.py::author_beat_prompts` → **S–M (after investigation)**
- **CROSS-CUTTING — beat-type taxonomy** → planner-wide change, do AFTER #1/#2/#3 land → **L**

## LOCAL RE-AUTHOR (per slug, only AFTER systemic fix #1 lands)

- `tifu-my-20f-girlfriend-of-two-years-told-me-the-mu-24c5887a` → re-render with cast routing live; verify narrator (male) appears in establishing beats, girlfriend in reaction beats, both in the confrontation. Don't re-render before #1 ships — same singleton bug will repeat.

## INVESTIGATION-NEEDED

- **Pull `gs://ytfactory-prod-v3-artifacts/jobs/<job_id_24c5887a>/cast.json`** — confirm what narrator gender/spec was authored. Determines whether singleton bug surfaces as "narrator everywhere" or "girlfriend everywhere."
- **Pull `gs://ytfactory-prod-v3-artifacts/jobs/<job_id_24c5887a>/prompts.json`** — confirm whether `SHOT_ROTATION` tokens are present in this render's image prompts (CLASS-OF-BUG #5 disambiguation).
- **Listen to `audio.wav` by ear** — confirm narrator voice gender to determine whether #2 is critical (3-way conflict) or merely systemic (singleton mismatch).
- **Read `pipeline/llm/prompts.py::author_beat_prompts`** — confirm no existing figurative-register handling before claiming #4 is new mechanism (vs. existing-and-broken).

## TOP PRIORITY

**CLASS-OF-BUG #1.** It's S-sized, file:function-grounded, has the largest blast radius (every multi-character render on the busiest channel), kills the single most damaging viewer-perception failure (POV confusion), and the underlying function already exists. This is the highest leverage-to-cost finding in the report.
