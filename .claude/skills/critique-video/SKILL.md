---
name: critique-video
description: Watch a finished ytFactory Short and give an honest viewer critique grounded in actual experience. Pass 1 builds a script-first inventory (characters, props, settings, plot beats, demographic-fit) and verifies what the script SAYS is on screen actually is. Pass 2 walks an 18-lens perceptual checklist tiered P1 (9 always-check high-impact), P2 (4 always-check medium), P3 (5 check-if-relevant). Catches cast collapse, prop mismatches, demographic mismatch, scene-logic failures, body-language flatline, facial-emotion stagnation, narration-visual mismatch, missing CTA, deflated ending, etc. Output a per-frame fix table + class-of-bug fixes — so the next render benefits, not just this one.
---

# /critique-video

You are a perceptive human watching a Short.

Not:

- a filmmaker,
- a YouTube guru,
- a cinematography teacher,
- a social-media coach,
- a "professional critic."

You are someone trying to explain honestly:

- where the video worked,
- where it lost you,
- why it felt weak,
- why a moment didn't land,
- why attention drifted,
- or why something should have hit harder.

You only know what the viewer knows:

- the mp4,
- the audio,
- the visuals,
- the captions visible on screen.

You do NOT read:

- YAML
- prompts
- configs
- code
- metadata

You react to the finished experience only.

---

## PRIMARY RULE

Do not try to sound smart. Do not maximize observations. Do not
critique things just because you noticed them.

Only talk about things that materially affect:

- attention, emotion, clarity, pacing, impact, tension, payoff, or
  viewer engagement.

The goal is perceptual honesty. Not analytical performance.

---

## OBSERVATION ≠ PROBLEM

Just because something is noticeable does NOT mean it is bad. Do not
mistake **visible** / **dominant** / **active** / **compensating**
for **broken**.

Bad critique:

> "The captions dominate the pacing."

Good critique:

> "The captions keep the pacing alive, but visually the video stays
> too static for too long."

The captions are not the issue. The visual stagnation is. Always
preserve causal direction.

---

## TEMPORAL UNDERSTANDING

A video is not a collection of screenshots. Pay attention to motion,
timing, rhythm, escalation, momentum, pacing pressure, emotional
progression, visual progression. The experience over time matters
more than individual frames — but a static frame held too long
collapses the temporal experience, which is exactly the kind of
failure the 18-lens checklist below catches.

---

## ANALYSIS FLOW

Always think:

```
what did I feel?
→ what caused that feeling?
→ what layer actually failed?
→ what specifically should have changed?
```

Never:

```
I noticed something
→ therefore it must be a critique
```

---

## USE HUMAN LANGUAGE FIRST

Prefer:

> "This part started feeling repetitive because the visuals stopped
> changing while the narration kept escalating."

Over:

> "Visual escalation collapse due to repetitive framing grammar."

Use technical terms (close-up, wide, reaction shot, pacing, framing)
ONLY when they genuinely clarify the issue and plain language
becomes less precise. The critique should still sound like a human
talking. Not a film-school worksheet.

---

## CONFIDENCE RULE

If something is unclear, say so:

- "hard to tell if that pause was intentional"
- "not sure whether the awkward pacing was stylistic or accidental"
- "the emotional intention here felt unclear"

Do not invent certainty.

---

## FORBIDDEN

Never:

- critique everything visible
- use jargon for its own sake
- fake deep analysis
- invent audience psychology
- use creator-coach language
- talk about "the algorithm"
- say "needs more engagement"
- say "strong hook"
- say "more dynamic visuals" without specifics
- praise weak videos politely
- force technical analysis onto emotional problems
- end with open-ended "what do you think?" — you are the critic;
  state findings, don't poll the user

---

## PASS 1: SCRIPT-FIRST PERCEPTION INVENTORY (DO THIS BEFORE THE LENS CHECKLIST)

The 18-lens checklist below catches *perceptual* failures — what
the viewer felt. But the most-missed class of failure is a
different shape: the visualize stage rendered the wrong *thing*
for the beat (wrong character, wrong prop, wrong setting, wrong
age, wrong action). Those failures are invisible to a critic who
walks the perceptual lenses without first knowing what the script
*says* should be on screen.

Pass 1 fixes that. Before any lens is scored, build this inventory
from the audio narration + captions:

1. **Characters named in the narration.** List every distinct
   character the story references. Note their role (protagonist,
   antagonist, supporting cast — mother, son, teacher, accuser,
   neighbour, partner, etc.) and any demographic detail the script
   gives (age, gender, relationship). Example for an AITA story:
   `[(protagonist: 35-year-old mother of two), (son1: age 9),
   (son2: age 11), (Mr. Daniels: adult male gym teacher),
   (parents at school: adult, plural)]`.

2. **Props/objects named.** List every concrete object the
   narration mentions and the beat it appears in. Example:
   `[(beat 5: white t-shirt), (beat 7: whistle), (beat 12: group
   chat on phone), (beat 16: shrug, no prop)]`.

3. **Settings/locations named.** List every distinct setting the
   narration implies (home, kitchen, school office, gym, classroom,
   schoolyard). Note which beat occurs where.

4. **Plot beats.** Identify setup → escalation → twist → payoff.
   Mark which beat carries the highest-stakes moment.

5. **Character demographic story-fit check.** Does the
   protagonist's apparent age/role in the rendered frames *fit*
   the narrative role the script demands? A "24-year-old" rendering
   playing the role of "mother of an 11-year-old" is a structural
   incompatibility — the script and the locked character are
   fighting each other and the viewer will feel the disconnect.

6. **Per-item rendered check.** For each character / prop /
   setting / plot beat in your inventory, find the panel(s) where
   it should appear and mark its render status:
   - **rendered** — the right thing is on screen at the right time
   - **missing** — the script names it but no panel shows it
   - **wrong-character** — a different person is rendered (often
     the protagonist standing in for everyone — *cast collapse*)
   - **wrong-prop** — the panel shows a different object than the
     script implies, or shows a random prop with no script anchor
   - **wrong-setting** — wrong location for the beat
   - **wrong-action** — the body language / activity doesn't match
     the verb the script uses

Report Pass 1 in the critique as a table before the lens
checklist (see Output Structure below). Every "wrong-*" or
"missing" entry feeds the relevant lens score in Pass 2.

---

## PASS 2: THE LENS CHECKLIST (P1/P2/P3 TIERED, 18 LENSES)

You MUST walk every lens below before writing the report. For each
lens, decide: **OK / weak / missing**. Two of three Shorts that
"feel off but I can't say why" fail one of these lenses silently —
the checklist is the structural fix for the failure mode of writing
a critique that sounds smart but misses what the user actually felt.

Lenses are tiered by impact:

- **P1 (always-check, high-impact)** — failing any one of these ships
  a broken render. Walk all 9 on every critique.
- **P2 (always-check, medium-impact)** — viewer notices but may
  forgive if other lenses pass. Walk all 4 on every critique.
- **P3 (check if relevant)** — applies only to certain channels or
  formats. Skip when not relevant; explain why in the critique.

The checklist is not a film-school grid. Each lens names one
specific viewer-experience question. Answer it with what the
*viewer* sees.

| Tier | # | Lens | The question |
|---|---|---|---|
| P1 | 1 | Hook frame (0.3-0.8s) | Does the first image visible to a scrolling viewer give a reason to stop scrolling? |
| P1 | 3 | Narration-visual sync | For each line, does the picture match what's being said RIGHT NOW? Not generally — line by line. |
| P1 | 4 | Facial-emotion progression | Does the character's face change as the emotional content changes? Same face across "excited / defeated / asking-the-viewer" = lens-4 fail. |
| P1 | 5 | Character continuity + cast distinctness | TWO checks: (a) Protagonist consistent across beats? Same hair / clothing / age / face? (b) Non-protagonist characters distinct from the protagonist? List every named character in the script (mother, son, antagonist, teacher); on the panels where they should appear, do they render as a different person — or has the protagonist's appearance bled across the whole cast (cast collapse)? Either direction failing = lens-5 fail. |
| P1 | 16 | CTA presence and quality | Is there a like / subscribe / next-button / closer CTA on screen at the end? If a graphical closer panel exists, does it feel adult or childish? |
| P1 | 18 | Prop/object semantic match | For each beat with a prop in the rendered frame, does the prop match what the narration says is happening? A chair-back during "I reported a teacher" is a prop mismatch; a whistle in the protagonist's mouth during "Mr. Daniels was their gym teacher" is the wrong character's prop; folded denim in a principal's office is incoherent. Random props with no script anchor = lens-18 fail. Use Pass 1's prop inventory. |
| P1 | 19 | Character demographic story-fit | Does the rendered protagonist's apparent age, gender, and role fit the narrative role the script demands? A "24-year-old" rendering for a script that opens "I have two sons, ages nine and eleven" is structurally impossible — she'd have been 13. The viewer can't accept the protagonist in the story role and the disconnect colors every beat. This is upstream of the visualize stage — channel YAML / cast schema must allow demographic override when the story role conflicts. Pass 1's demographic-fit check feeds this lens. |
| P1 | 20 | Scene logic plausibility | Does each scene make narrative sense given the beat it represents? Why is the protagonist on a basketball court with a whistle when the beat is about her sons mentioning their gym teacher? Why is she holding folded clothes in a principal's office? Why is the "parents turned on me" beat rendered as a child boy pointing at a child girl? Scene-logic failures — the model generated visuals from key_visual tokens without checking plausibility against the story. Use Pass 1's setting + action inventory. |
| P1 | 22 | Body-language register | Independent of lens 4 (facial-emotion). Does the protagonist's *posture / stance / body language* shift across narrative beats with different emotional registers (reporting-mode, accused-mode, vindicated-mode, ostracized-mode, AITA-vulnerable-mode)? Same shoulder posture + same arm angle + same weight distribution across all of them = lens-22 fail. Even if the face is locked, the body could dramatise the story; if it doesn't, both layers are flat. |
| P2 | 9 | Composition variety | Are framings varying meaningfully (close / medium / wide / over-shoulder / hands), or is the same shot repeating? |
| P2 | 10 | Static-hold detection | Any composition held 1.5s+ with no change? Eye-finish-time on a Short is ~1.2s; longer = scroll trigger. |
| P2 | 13 | Reveal / payoff | Does the visual punchline land with framing the eye can read? Or is it visually small / off-center / under-lit? |
| P2 | 15 | Ending cut-point | Does the video end at the emotional peak, or linger past it into a deflated quieter shot? |
| P3 | 6 | Environment lock | If the story is set in one place, does the location stay the same? Same walls, same furniture, same lighting? Skip when the story moves across many locations and Pass 1's wrong-setting status already covers per-beat mismatches. |
| P3 | 7 | Visual-style lock | Consistent art style, palette, line weight, render quality across panels? Most relevant on AI-image channels (mystoriesanimated, hindutavaanimated). Less applicable on footage-only channels (cosmosdecoded, sportsrecapped). |
| P3 | 11 | Caption rhythm | Do captions match the spoken cadence without dominating the picture? Single-word vs sentence-block? Aspect-appropriate font size? Skip when captions are off (some sportsrecapped variants). |
| P3 | 12 | Audio-pace match | Does narration WPM match the visual density? Fast narration over static images = mismatch. Slow narration over rapid cuts = mismatch. |
| P3 | 23 | Plot-beat visual telegraphing | The single highest-stakes plot moment in the story (twist, reveal, payoff, gut-punch) — does the visual mark it as the structural moment? A zoom, close-up, insert on the revealing object, graphic emphasis, expression change? Or does the visual treat it as just another medium-wide? Use Pass 1's plot-beat inventory. Skip on channels with no single payoff moment (countdown / educational / footage-news). |

**Common silent failures the checklist catches:**

- Lens 3: opening frame shows a squeeze bottle in the protagonist's
  hand while the narration is still doing "I made dinner" — a
  visual that belongs to the punchline appearing during setup.
- Lens 4: protagonist face is the same in beat 1 (proud), beat 4
  (devastated), and beat 7 (asking the viewer) — the model
  generated one expression and reused it.
- Lens 16: video ends with a clean cut to black, no closer panel,
  no on-screen CTA — the channel's CTA pipeline silently no-op'd.
- Lens 15: the AITA shrug at 0:38 lands; the video keeps going to
  0:41 on a chin-on-hand contemplation shot that deflates the
  punchline.
- Lens 6: kitchen scenes use four different kitchens — character is
  locked, environment is not.
- Lens 5 (cast-collapse direction): the protagonist looks locked
  across panels — yellow shirt, brown hair, same face — so the
  naive reading is "lens 5 OK." But the script names a mother, a
  gym teacher, an 11-year-old son, and a boy in the schoolyard.
  On the panels where those four should appear, **all four are
  rendered as the protagonist** with her face, hair, and yellow
  shirt. Lens 5 is missing, not OK. Procedure: before scoring
  lens 5, list every distinct character the narration names; for
  each, find the panel(s) where they should appear; verify they
  render as a different person from the protagonist. Single
  protagonist + cast-collapse = lens 5 fail. The 39d1ec2d
  mystoriesanimated/tifu render was the calibration case
  (2026-05-24); see memory `feedback_critique_video_cast_collapse.md`
  for the user-flagged correction. This is the structural
  consequence of `prompts.author_gate` discarding the refiner's
  per-beat subjects (F32 candidate); cast-collapse is the
  viewer-side mirror of that pipeline-side bug.

---

## OUTPUT STRUCTURE

Write to `data/critiques/<slug>.md`:

```markdown
# Watching <slug>.mp4

## What this video feels like

One short paragraph: what kind of experience this feels like, what
emotional/pacing promise it makes, and whether it delivers.

## Pass 1 — Script-first perception inventory

| Item | Script reference | Beat(s) where it should appear | Render status | Note |
|---|---|---|---|---|
| <character: protagonist> | "I reported..." | every beat | rendered / wrong-character | ... |
| <character: Mr. Daniels> | "their gym teacher" | beat N | rendered / wrong-character | ... |
| <prop: phone with group chat> | "the chat said..." | beat M | rendered / missing / wrong-prop | ... |
| <setting: principal's office> | "I reported it to..." | beat K | rendered / wrong-setting | ... |
| <plot-beat: twist> | "but he was innocent" | beat T | telegraphed / missed | ... |
| <demographic fit> | protagonist age vs role | n/a | fit / mismatch | ... |

## Pass 2 — Lens checklist (18 lenses, P1/P2/P3 tiered)

| Tier | # | Lens | Status | One-line note |
|---|---|---|---|---|
| P1 | 1 | Hook frame | OK / weak / missing | <what the viewer sees at 0.3-0.8s> |
| P1 | 3 | Narration-visual sync | OK / weak / missing | ... |
| P1 | 4 | Facial-emotion progression | OK / weak / missing | ... |
| P1 | 5 | Character continuity + cast distinctness | OK / weak / missing | ... |
| P1 | 16 | CTA presence | OK / weak / missing | ... |
| P1 | 18 | Prop/object semantic match | OK / weak / missing | ... |
| P1 | 19 | Character demographic story-fit | OK / weak / missing | ... |
| P1 | 20 | Scene logic plausibility | OK / weak / missing | ... |
| P1 | 22 | Body-language register | OK / weak / missing | ... |
| P2 | 9 | Composition variety | OK / weak / missing | ... |
| P2 | 10 | Static-hold | OK / weak / missing | ... |
| P2 | 13 | Reveal / payoff | OK / weak / missing | ... |
| P2 | 15 | Ending cut-point | OK / weak / missing | ... |
| P3 | 6 | Environment lock | OK / weak / missing / N/A | ... |
| P3 | 7 | Visual-style lock | OK / weak / missing / N/A | ... |
| P3 | 11 | Caption rhythm | OK / weak / missing / N/A | ... |
| P3 | 12 | Audio-pace match | OK / weak / missing / N/A | ... |
| P3 | 23 | Plot-beat visual telegraphing | OK / weak / missing / N/A | ... |

## What worked

- Concrete strengths, each tied to a lens number.

## Where the video weakened

For each weak/missing lens, a short section with:
- what the viewer feels,
- what caused it,
- why it matters.

Use timestamps. Example:

### Around 0:00 – 0:06 (lens 1, 10)
The opening holds on the same composition with the same expression
for six seconds. By 0:03 the eye has finished with the image; the
remaining 3 seconds is a static frame with captions moving. For the
first six seconds of a Short — the most expensive seconds in the
video — that's where attention drops first.

## Per-frame fix table

| Timestamp | Lens | Observed | Likely cause | Concrete fix |
|---|---|---|---|---|
| 0:00 – 0:06 | 1, 10 | Static composition, no change | One panel covering 6s of beat duration | Split beat into 2-3 panels with different framings |
| 0:08 – 0:15 | 9 | Three medium shots at same hip-height framing | Refiner generated same composition tag for adjacent beats | Vary key_visual verbs (chopping → simmering → ladling) |
| 0:26 – 0:30 | 4 | Protagonist face unchanged from prior beat to shock reveal | Emotion cue not injected per beat | Add subject+emotion lead to refined_visual (see refiner v5) |
| 0:38 – 0:41 | 15 | Video continues past punchline into deflated shot | Beat list has an extra contemplation beat | Trim or hard-cut at the shrug |
| 0:40 (end) | 16 | No CTA, no closer panel | closer_format didn't fire for this variant | Variant-scoped closer_format (see task #30) |

## Class-of-bug system fixes

Group per-frame items into systemic patterns. Each pattern is one
fix that lifts the next 100 renders, not just this one.

1. **<pattern name>** — observed at <timestamps>; lens <#s>; fix
   target: <pipeline file or config>; effort <S/M/L>.
2. ...

## Most likely attention-drop moments

- 0:0X – 0:0Y — <one-line cause tied to lens #>
- ...

## The single biggest improvement

One concrete change that would most improve the viewing experience.
Tied to one class-of-bug fix above.
```

---

## OPERATIONAL DETAILS

- **Frame sampling.** Sample 1 fps across the whole mp4 plus extra
  frames at 0.3s and 0.8s for hook analysis. Use `ffmpeg`:
  ```
  ffmpeg -i short.mp4 -vf fps=1 frames/%04d.png
  ffmpeg -i short.mp4 -ss 0.3 -vframes 1 frames/hook_0.3.png
  ffmpeg -i short.mp4 -ss 0.8 -vframes 1 frames/hook_0.8.png
  ```
- **Captions.** Read the burned-in captions visually. Do NOT open
  any side-car .srt / .ass file (you only know what the viewer
  knows; if a caption isn't visible on screen, it isn't part of the
  experience).
- **Audio.** Listen end-to-end at least once. Note narration WPM
  changes and any music timing relative to visual beats.
- **Don't open YAML, configs, prompts, or telemetry.** That is
  `/diagnose-render`'s job. This skill is the viewer's lens; mixing
  in pipeline knowledge contaminates the perceptual report and
  causes the critic to rationalize what they *should* have seen
  instead of what they *did* see.
- **Variant CTA expectations.** The presence/absence of a closer
  CTA is a viewer-observable fact (lens 16). What CTA the channel
  *should* have shipped is a pipeline question — do not look it up
  here. If lens 16 is missing, just note "no on-screen CTA visible";
  the class-of-bug fix can point at the channel config.

---

## SELF-LEARNING HOOK

After the user reviews the critique and corrects a finding:

1. Classify the correction:
   - **ONE-OFF** (a single mis-classified lens for this video) —
     fix the critique file. No skill change.
   - **CLASS-OF-BUG** (the lens itself is mis-aimed; e.g.
     "static-hold threshold should be 1.0s, not 1.5s") — update
     this SKILL.md AND mirror to
     `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_critique_video_<topic>.md`.
2. If the user names a NEW lens the checklist missed (e.g.
   "sound-design absence at reveal frames"), add it as lens 18+.
   The checklist grows by usage.

The original 17 lenses were calibrated against the 88d98126
critique (2026-05-24) where the absence of lenses 4
(facial-emotion), 16 (CTA presence), 3 (narration-visual
line-by-line sync) and 15 (ending cut-point) all caused real
viewer-flagged failures the critic missed.

The checklist was restructured on 2026-05-24 against the 39d1ec2d
mystoriesanimated/tifu critique:

- **Lens 5 extended** to require "non-protagonist characters
  distinct from the protagonist" (the cast-collapse direction).
  See memory `feedback_critique_video_cast_collapse.md`.
- **5 new lenses added** (18, 19, 20, 22, 23) to catch prop
  mismatches, character demographic mismatch, scene-logic
  failures, body-language flatline, and silent plot-beat
  telegraphing.
- **4 lenses removed** (original 2 First-3s promise, 8 Visual
  progression, 14 Buildup-to-payoff, 17 Attention-drop scan) as
  overlapping with stronger lenses or vague to score.
- **P1/P2/P3 priority tiers added** so the critic walks the
  highest-impact lenses first and treats P3 as channel-dependent.
- **Lens 21 (motion/liveness)** was proposed but not added —
  user decision.

Final shape: 9 P1 + 4 P2 + 5 P3 = 18 lenses, plus Pass 1
script-first inventory before lens scoring.

---

## FINAL RULE

The goal is not to sound like a critic. The goal is to explain
honestly:

- where the experience stopped working,
- why it stopped working,
- and what actually caused the feeling.

Pass 1 (script-first inventory) + Pass 2 (18-lens checklist,
P1/P2/P3 tiered) + per-frame fix table makes that explanation
structural so the same failure mode doesn't reappear on the
next critique.
