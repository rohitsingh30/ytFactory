---
name: make-hindutava-long
description: Author a 10-15 min animated Hindi mythology long-form for HindutavaAnimated — Mahabharat / Ramayan / Puraan / Krishna-leela developed across 8-12 chapters with proper setup, character development, escalation, climax, and lesson (vs the staccato Shorts format). Amar Chitra Katha AI panels via Cloud Run FLUX.2 klein with Ken-Burns motion, IndicF5 cloud TTS with hindi-female-iitm-anchor voice-clone, sentence-level Devanagari captions, soft tabla/sitar bed, two embedded support asks. Produces hindutavaanimated/narrations/<slug>.json + chapters/<slug>.json + optional cast/<slug>.json, then hands off to historyrecapped/scripts/render_long_form.py --channel hindutavaanimated --aspect 16:9. Use when the user says "make me a hindutava long-form", "10 min Krishna leela video", "long-form Mahabharat", "animated mythology long-form", or names a Mahabharat/Ramayan/Puraan episode and wants it "long-form". For 50-60s Shorts use /make-hindutava-short. For 50-70 min footage-only kathaa use /make-katha.
---

# /make-hindutava-long — 10-15 min animated Hindi mythology long-form for HindutavaAnimated

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

This skill authors **one fully-developed Hindu mythology episode** as a
10-15 min animated long-form video for **HindutavaAnimated**. Unlike
the Shorts format (`/make-hindutava-short`, 50-60s, 13 staccato beats,
hook → twist → centerpiece → CTA), this skill builds the story across
**8-12 chapters with proper setup, character interiority, escalation,
climax, aftermath, and lesson** — the way Amar Chitra Katha
narrates a chapter end-to-end.

You wear three hats: **Mythology Researcher → Kathaa-Vyaas (long-form
writer) → Visual Director (panel notes for AI image gen with Ken-Burns
motion)**.

The output is two or three JSON files —
`hindutavaanimated/narrations/<slug>.json` (always),
`hindutavaanimated/chapters/<slug>.json` (always — chapter outline +
panel list), and `hindutavaanimated/cast/<slug>.json` (only if the
story has named characters that need a per-story face lock). Stages
4-7 (TTS, image gen, compose) live in `pipeline.render.long_form` via
the existing legacy renderer (and post-2026-05-14 also in
`pipeline.render.long_engine` via the new pluggable engine — flip
via `YTFACTORY_USE_ENGINES=1`; see
[`docs/render_engines_2026.md`](../../../docs/render_engines_2026.md)).
`historyrecapped/scripts/render_long_form.py`, not in this skill.

## How to run it

> **CLOUD-TTS RULE (inherited from /make-hindutava-short 2026-05-07):**
> The channel YAML's `tts_provider:` MUST be a `cloudrun_*` engine.
> Local providers are automatic-fallback paths only. Voice catalog
> name resolution lives in `pipeline/voice_catalog.py` — channel
> declares `tts_voice: hindi-female-iitm-anchor` and the renderer
> resolves it to `pipeline/voice_refs/hindi-female-iitm-anchor/ref.wav`
> + transcript on synthesis.

> **SCOPING RULE (inherited):** Stage-1 / scope-confirmation MUST use
> `AskUserQuestion` with **2-4 prefilled options** grounded in
> shipped Shorts exemplars + canonical not-yet-covered Mahabharat /
> Ramayan / Puraan episodes (Krishna-Govardhan, Bheeshma-Iccha-Mrityu,
> Draupadi-Cheer-Haran, Ravan-Das-Mukh, Hanuman-Lifting-Sanjeevani,
> Parashuram-Axe, Shabri-Ke-Ber, Kaaliya-Naag-Mardan, etc.). Never ask
> the user to type the episode — propose 3-4. Source of truth:
> [`docs/skill_prefilled_options.md`](/Users/rohit/ytFactory/docs/skill_prefilled_options.md).

### 1. Confirm episode + epic

Lock the **5-line spec** before writing anything (one more line than
Shorts because long-form needs an explicit chapter-count target).

- **Epic** — `mahabharat` | `ramayan` | `bhagavat-puraan` |
  `shiv-puraan` | `vishnu-puraan` | `krishna-leela`. Default to
  `mahabharat` if unspecified.
- **Episode** — the dramatic moment / story-arc (e.g.
  `krishna-govardhan-leela`, `bheeshma-iccha-mrityu`,
  `draupadi-cheer-haran`, `lakshman-rekha-and-haran`).
- **Slug** — `<character>-<episode>-long` lowercase-hyphen. The
  `-long` suffix distinguishes it from a Shorts variant of the same
  episode (e.g. `krishna-govardhan-dharan` Short vs.
  `krishna-govardhan-leela-long` long-form). NO dates in the slug.
- **Lesson hindi** — the one-line takeaway in pure Hindi that lands
  in the final chapter. Write this FIRST — the whole arc bends
  toward it.
- **Chapters** — target 8-12 chapters. Pick the count that fits the
  episode's natural beats (a Mahabharat parva-spanning event might
  need 12; a single-day krishna-leela might be 8).

Output of stage 1:

```
epic:     krishna-leela
episode:  krishna-govardhan-leela
slug:     krishna-govardhan-leela-long
lesson:   "जब अहंकार बरसता है — तो विश्वास पर्वत बन जाता है।"
chapters: 10
```

**Refuse** if the user names a SUNG-tradition text framed as paath
(Hanuman Chalisa, Sundarkand sung doha-chaupai, full aartis, full
bhajans). For animated long-form, the audience expects a *narrated
dramatic story*, not a chanted paath. Sundarkand-derived dramatic
arcs (e.g. "Hanuman lifting Sanjeevani" the story, not "the Sundarkand
chaupai") are fine. Mention this distinction if ambiguous.

### 2. Hat 1 — Mythology Researcher: build the long-form dossier

For the chosen episode, gather:

- **Canonical source** — Gita Press editions preferred; Wikipedia +
  Bhagavata-Puraan summary acceptable as secondary outline. Cite the
  parva / kaand / adhyay or sarga in `metadata.epic_section` (e.g.
  `"Bhagavat Puraan, Dashama Skandha, Adhyay 24-25 — Krishna lifts
  Govardhan parvat to shelter Vrindavan from Indra's deluge"`).
- **Characters on screen** — proper-noun list (4-10 names typical for
  long-form). Goes into `metadata.characters_on_screen` AND drives the
  cast file for any face-locked character.
- **Chapter outline** — 8-12 chapter titles in Devanagari (Hindi) +
  English (for SEO chapter markers). Each chapter has a **1-line
  Hindi summary** + a **dramatic question** the chapter answers
  (this drives narrative tension). Example:

  ```
  ch 0 — "वृन्दावन की धरती"        / "The Land of Vrindavan"
         summary: नंदगाँव में कृष्ण की दिनचर्या और गोकुल की पुरानी पूजा-परंपरा
         question: ब्रज की पुरानी परंपरा क्या थी?
  ch 1 — "इंद्र की पूजा"            / "The Worship of Indra"
         summary: हर साल वर्षा-ऋतु से पहले होने वाला इंद्र-यज्ञ
         question: गाँव वाले इंद्र को क्यों पूजते थे?
  ...
  ch 9 — "विश्वास का पर्वत"        / "The Mountain of Faith"
         summary: कथा का पाठ — असली शक्ति कहाँ है
         question: क्या सीख देती है ये कथा?
  ```

- **Visual centerpieces** — the 2-3 ICONIC FRAMES the long-form pivots
  on. Unlike Shorts (one centerpiece beat), long-form has multiple
  payoff moments: e.g. for Govardhan it's
  (a) Krishna's challenge to the village ("इंद्र नहीं — गोवर्धन"),
  (b) the parvat lifting,
  (c) Indra bowing in surrender. Mark the corresponding panels with
  `scene_kind: "centerpiece"` and a detailed `notes` field.

**Refuse to fabricate.** Devotional viewers fact-check long-form
content even more than Shorts. If you can't ground a chapter in
canonical text in 2 min of research, drop it or merge it into a
neighbouring chapter.

### 3. Hat 2 — Kathaa-Vyaas: write the long-form narration

**Word budget:** 1600-2400 Hindi words total for 10-15 min at
IndicF5's measured cadence (~155-165 wpm with the chunker's per-
paragraph speed normalisation). Don't trust intuition — the budget
gate in §6 is mechanical.

**Chapter shape** (for each of the 8-12 chapters):

| Element | Word count | Role |
|---|---|---|
| Chapter title screen narration | 5-15 words | "अध्याय एक — वृन्दावन की धरती" — TTS reads the title at the chapter's open |
| Scene-setting | 30-60 words | place, time, weather, mood |
| Character interiority | 30-80 words | what's the protagonist thinking / feeling |
| Dialogue + action | 50-150 words | direct speech (not too much), action beats, dramatic peaks |
| Transition to next chapter | 10-30 words | a hanging question, a foreshadow, or a pause |

**NO staccato beat structure.** No 12-beat hook → twist → centerpiece
→ CTA. Long-form means natural storytelling cadence — short sentences
for action, long sentences for reflection, pauses where a real
narrator breathes.

**Opening — proper scene-setting, NOT a hook formula.** Open with
where we are, when, who. The "जब X ने Y कर दिया" Shorts hook formula
is **kill-on-sight** here — the user explicitly asked for "proper
explanation, long story with proper continuation" 2026-05-07. A 10-
min video doesn't need a 1.5s hook; it needs a confident scene-
setting that earns the viewer's commitment.

Example openers (Mahabharat / Ramayan / Krishna-leela registers):
- "व्रज-भूमि। सावन का महीना। वर्षा-ऋतु के बादल अभी आसमान में नहीं
  आए हैं — पर पूरा गोकुल तैयारी में लगा है।"
- "हस्तिनापुर का राजमहल। रात गहरी है। एक बूढ़ा योद्धा अपनी शय्या
  पर लेटा है — पर ये साधारण शय्या नहीं।"
- "आदित्य-कुल का अंतिम राजा वाल्मीकि-आश्रम पहुँचा। राजसी पोशाक
  में, पर आँखें भीगी हुईं।"

**Two embedded `[ASK]` markers in the narration.** Place exactly two
inline support asks — one at ~25-30% (3-4 min in) and one in the
final chapter just before the closing lesson. Inline format:

```
[ASK1] अगर ये कथा अब तक पसंद आ रही है — एक छोटा सा लाइक करें, और
चैनल को सब्सक्राइब करें ताकि अगली कथा की सूचना मिल जाए। [/ASK1]
```

The renderer detects `[ASK1]` / `[ASK2]` and inserts a 1.5s
ambient-bed-louder beat at that moment. Inherited from
historyrecapped's `long_form_support_asks.md` — 2 asks per long-form,
not 5.

**Closer — 3-paragraph rule (kept from Shorts):**

```
<final-chapter lesson, alone>

<CTA — Devanagari only>

<blessing>
```

CTA literal: `अगर ये पूरी कथा पसंद आई हो — लाइक करें, कमेंट में
लिखें, सब्सक्राइब करें।`
Blessing: `जय श्री कृष्णा।` (Bhagavat / Krishna-leela / Mahabharat
default), `जय श्री राम।` for Ramayan, `हर हर महादेव।` for Shiv-
Puraan, `जय श्री हरि।` for Vishnu-Puraan.

**Banned phrasings (kill-on-sight):**

- Sanskrit-only narration / verse-after-verse paath. One-line shloka
  quotation inside Hindi prose is OK.
- "नमस्कार दोस्तों" / "हैलो दोस्तों" / "हाय दोस्तों" / "क्या आप
  जानते हैं" — YouTube-vlogger openers. Long-form opens with the
  scene, never a host greeting.
- "smash subscribe" / "vote in comments" / verdict acronyms.
- Latin tokens in narration body — Devanagari only. The Shorts
  closer-drop bug 2026-05-07 (IndicF5 silently drops trailing chunks
  on Latin script in Hindi). लाइक/कमेंट/सब्सक्राइब in Devanagari.
- Quoted descriptive phrases in panel notes (diffusion-text-leak
  class-of-bug). All on-image text overlaid by `pipeline.captions`,
  never baked into FLUX.2 klein.
- Devanagari in image prompts (FLUX.2 klein collapses conjuncts).
  Single ॐ glyph is the documented exception.

**Prosody table (`narration_prosody`)** is OPTIONAL for long-form —
the chunker auto-derives per-paragraph speeds from the table when
present, but a 180-paragraph long-form is too tedious to author one
sentence at a time. Author prosody hints ONLY for:
- chapter-opening title-card sentences (slow + weighty, speed 0.85)
- climax beats (varied — slow on the awe moment, brisk on the action)
- the embedded `[ASK]` lines (slightly brisk, 1.0)
- the final lesson + blessing (slow, weighty, 0.78)

Other paragraphs default to speed 1.0 and the chunker's
post-paragraph silence is capped at 0.10s per the Shorts learning.

### 4. Hat 3 — Visual Director: panel notes

Long-form has ~60-90 panels for 10-15 min (1 panel per 8-15s of
audio, varying by scene density). Group into chapters; each chapter
has 4-12 panels.

For each panel, author:

- `i` — global index (0..N-1)
- `chapter` — which chapter this panel belongs to
- `narration_paragraphs` — which narration paragraph indices this
  panel covers (typically 1-3 consecutive paragraphs)
- `duration_s` — auto-computed by renderer from word-count proportion
- `scene_kind` — `scene_setting` | `character_interior` |
  `dialogue` | `action` | `escalation` | `centerpiece` | `aftermath`
  | `chapter_title` | `lesson` | `cta` | `blessing`
- `shot_size` — `wide` | `medium` | `close` | `extreme_close` —
  drives Ken-Burns motion (wide → slow zoom-in, close → slight
  pan, extreme-close → static or breath-pulse)
- `notes` — Amar Chitra Katha panel description (visual content
  only, no quoted phrases, no caption-region references, no
  Devanagari)

**Visual variety check** (gate in §6): no two adjacent panels share
the same `scene_kind`. Long-form viewers tune out fast on repetition.
Vary the shot size every 2-3 panels. Vary the dominant character
visible. Vary the framing (left-leading / centered / right-leading).

**Centerpieces — 2-3 per long-form** (vs 1 per Short). Mark them
with `scene_kind: "centerpiece"` AND `notes` containing "ICONIC
FRAME". These get extra hold time + larger Ken-Burns motion budget.

**Diffusion safety rules** (literal from
`hindutavaanimated/learnings/diffusion_quoted_phrase_leak.md` and
`non_latin_text_via_compose.md`):

1. **No quoted phrases in `notes`.**
2. **No caption / title / text-overlay region references.**
3. **No Devanagari.** (single ॐ exception)
4. **Reduce ensembles** to 1-2 named characters + silhouette wall
   for >3-figure scenes.
5. **No "broken/torn/shattered" applied to clothing/objects.** Use
   "wreckage scattered around" instead.

### 5. Cast handling

You DO need a `hindutavaanimated/cast/<slug>.json` for any long-form
with 2+ named characters that recur across chapters. Same schema as
Shorts cast files (description / palette / default_emotion / seed).
Long-form character drift is more visible than Shorts because the
viewer sees the character across 60+ panels — the seed-locked
description must be airtight.

For Mahabharat / Ramayan multi-character ensembles:
- 4-6 named characters typical (e.g. Krishna + Yashoda + Nanda +
  Indra + Surdas-narrator-stand-in for Govardhan-leela)
- Each gets a unique `seed` (108, 432, 216, 729, 1080, 432×3 — auspicious
  multiples)
- For the protagonist, lock attire + age + skin colour + signature
  prop (peacock feather, conch, kavach, bow) — this is the
  appearance_lock that propagates into every panel-prompt referencing
  the character

### 6. Quality gates (run BEFORE renderer — mechanical)

Each gate blocks emit on hit. Run in order.

1. **Banned-phrase scan** — Sanskrit-only verses, vlogger openers,
   "smash subscribe", verdict acronyms, Latin tokens in body. Block
   on hit.
2. **Hindi-only assertion** — narration body must be ≤1% ASCII
   word characters (only allowed: per-chapter English title in the
   `chapters[].title_en` field, never in the narration string).
   Use `\w` UNICODE per `devanagari_unicode_regex.md`. Block on hit.
3. **Pronunciation pre-pass** — every proper noun in
   `metadata.characters_on_screen` ∪ tatsama words in narration
   either matches `pipeline/tts/text_normalize.py:_HINDI_TATSAMA_RESPELLINGS`
   OR appears in `narrations/<slug>.json:pronunciation_dict`. Note:
   IndicF5 cloud path skips respellings (already wired); this gate
   is informational for the Kokoro-fallback path only.
4. **Length budget** — Hindi word count must satisfy
   `duration_target_s × 160 / 60` within ±15%. duration_target_s =
   600-900s per channel config `long_form_animated.duration_band`.
   Outside that, hard-fail.
5. **Chapter structure** — exactly 8-12 chapters; each chapter has
   non-empty `title_hindi` + `title_en` + `summary` + `panels` of
   length ≥4. Block on violation.
6. **Two `[ASK]` markers** in narration — exactly two `[ASK1]…[/ASK1]`
   and `[ASK2]…[/ASK2]` blocks. Block on count != 2.
7. **CTA literal match** — final-chapter narration must contain
   "लाइक करें" + "कमेंट में लिखें" + "सब्सक्राइब करें" + the
   epic-appropriate blessing line. Variants block emit.
8. **Closer 3-paragraph rule** — final-chapter narration ends with
   `lesson \n\n CTA \n\n blessing`. Em-dash-fused closers fuse on
   the TTS phoneme pass.
9. **Centerpiece-hold sanity** — at least 2 panels marked
   `scene_kind: "centerpiece"`; each centerpiece panel covers ≥2
   consecutive narration paragraphs (longer hold for the iconic
   visual).
10. **Visual variety** — no two adjacent panels share `scene_kind`.
    Block on violation.
11. **Diffusion-prompt safety scan** — every `panels[i].notes` field
    is checked for: quoted phrases, "caption", "title", "text",
    "writing", "where text", "negative space", any Devanagari char
    (except single ॐ glyph). Block on hit.
12. **Aspect + voice + image-provider match** — assert
    `config.yaml:long_form_animated.output_resolution == [1920, 1080]`,
    `tts_provider == "cloudrun_indicf5"`,
    `tts_voice == "hindi-female-iitm-anchor"` (or another catalog
    name resolving to a valid Hindi female voice),
    `image_provider == "cloudrun_flux2_klein"`. Drift blocks emit.
13. **Source-fidelity check** — `metadata.epic_section` cites canonical
    parva / kaand / adhyay / leela; each chapter's `summary` is
    grounded in the dossier (no fabricated chapter content).

### 7. Renderer handoff

```bash
set -a && . .env && set +a && \
  .venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_long_form.py -- \
    --channel hindutavaanimated \
    --slug <slug> \
    --aspect 16:9
```

The renderer reads `hindutavaanimated/config.yaml:long_form_animated`
for output_resolution, caption_mode, ken_burns flag, music_bed_db,
closer_hold_s. It will:

1. **Load** narration + chapters + cast from
   `hindutavaanimated/{narrations,chapters,cast}/<slug>.json`.
2. **Synth TTS** via Cloud Run IndicF5 with voice-clone anchoring
   (already default in `_synth_cloudrun_chunked`). The chunker
   handles 1600-2400 word narrations naturally — splits on `\n\n`,
   chunk 0 uses the catalog ref, chunks 1..N clone chunk 0's
   speaker. Falls back to local Kokoro hf_alpha on cloud failure.
3. **Whisper-transcribe** the audio with chunked + forced
   `language='hi'` per `whisper_chunked_hindi_forced.md`.
4. **Force chapter + panel boundaries** from `chapters/<slug>.json`
   (the chapter-aware analogue of the Shorts authored-beats
   preservation in `pipeline/render/shorts.py:_load_forced_narration_lines`).
5. **Generate panels** via Cloud Run FLUX.2 klein (Amar Chitra Katha
   style prefix auto-prepended); falls back to local mflux on cloud
   failure. Cast tokens + appearance_lock from `cast/<slug>.json`
   inlined per panel. Per-render circuit breaker on cloud failure.
6. **Apply Ken-Burns motion** per panel via existing
   `pipeline/footage.py` zoompan helpers. `wide` → slow zoom-in,
   `medium` → slight pan, `close` → static-with-breath-pulse,
   `extreme_close` → static.
7. **Render sentence-level Devanagari captions** via
   `pipeline.captions` in `caption_mode: sentence`. Each caption
   shows one full Hindi sentence at a time, yellow italic on
   semi-transparent dark band, mute-mode legible.
8. **Mix BGM** at -28 dB under narration with 1.5s fade in/out at
   chapter boundaries (existing `pipeline/audio.py:mix_bgm_under_narration`).
   Channel ships an optional `hindutavaanimated/music/<slug>.wav`
   slot for per-episode beds; otherwise uses the channel default
   tabla/sitar instrumental.
9. **Insert support-ask beats** at `[ASK1]` / `[ASK2]` markers —
   1.5s of BGM-louder, captions held, no narration overlay (the
   ask itself is in the narration; the beat just gives it visual
   breath).
10. **Overlay closer panel** (`closer_format: "जय श्री कृष्णा 🙏
    SUBSCRIBE"`) on the last 2-3s.
11. **Auto-upload** as configured (`upload.privacy: private` is the
    current default — flip to `public` via the website when ready).
    Long-form uses `upload.category_id: "22"` (People & Blogs) per
    the channel default.

Render time: ~25-40 min on a warm pipe (cloud TTS chunked + cloud
image gen + critic + compose). Slower than Shorts (~10 min) because
of the 60-90 panel image-gen budget. The 25-min kill-deadline rule
from `docs/render_deadline_policy.md` applies — do not interrupt
mid-render.

### 8. Self-learning hook

After the user runs `/critique-video` on the rendered long-form mp4:

1. Classify each finding:
   - **ONE-OFF** (this script's panel notes / typo / pronunciation
     in this slug) → fix in the relevant `<slug>.json`, append a
     1-line note to
     `.claude/skills/make-hindutava-long/learnings/_index.md`.
   - **CLASS-OF-BUG** (every future hindutava-long render will hit
     this) → fix at the right level:
     * New diffusion leak pattern → extend §6.11 prompt-safety scan
       AND `hindutavaanimated/learnings/diffusion_quoted_phrase_leak.md`.
     * Hindi-noun glossary miss (पर्वत-as-tree, उँगली-as-finger
       ambiguity) → extend `pipeline/glossary.py` (NEW — see
       cross-channel project doc) AND mirror to channel learning.
     * Character appearance drift across chapters → extend
       `pipeline/cast.py` appearance_lock propagation AND mirror
       to channel learning.
     * Chapter-pacing miss (one chapter felt rushed / dragged) →
       tighten §6.5 chapter-structure gate (min/max paragraph count
       per chapter) AND mirror to channel learning.
     * Caption-mode regression (sentence breaks at wrong points) →
       fix in `pipeline/captions.py` AND mirror to a new channel
       learning `long_form_caption_mode.md`.
   - Append the regression note to
     `.claude/skills/make-hindutava-long/learnings/<topic>.md` AND
     mirror to `hindutavaanimated/learnings/<topic>.md` per CLAUDE.md
     dual-save.
2. Update `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`
   index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in §6 that blocks emit on detection.

**FIRST-RUN watch-list** (most likely first-bug surfaces, derived
from the 2026-05-07 Shorts production debugging):

- New character not in cast file → face drift across 60+ panels.
  Always author cast.json before image gen for any 2+ named-character
  long-form.
- Centerpiece panels not earning their hold → audience tunes out at
  the iconic moment. Verify each centerpiece has ≥10 paragraph words
  AND `shot_size: extreme_close` or `shot_size: wide` (not medium).
- Chapter-opening title-card paragraph getting fused into the
  previous chapter's audio chunk → use explicit `\n\n` paragraph
  break in narration text + `chapter_break: true` flag in panels.
- Whisper-mlx-4bit bunching ~25 words at one timestamp on long Hindi
  audio (the 2026-05-07 krishna-govardhan-dharan render bug). The
  `pipeline/beats.py:save_beats` forward-progress monotonicity guard
  catches this; verify it's still active.

### 9. Report back

```
✓ wrote narration:    hindutavaanimated/narrations/<slug>.json
✓ wrote chapters:     hindutavaanimated/chapters/<slug>.json
✓ wrote cast:         hindutavaanimated/cast/<slug>.json   (only if needed)
  epic:               <epic>
  episode:            <episode_label>
  slug:               <slug>
  chapters:           <N> (8-12)
  panels:             <M> (60-90 typical)
  word count:         <N> Hindi words → ~<dur>s @ ~160 wpm
  centerpieces:       <K> panels (≥2 required)
  embedded asks:      [ASK1] at ~<m>min, [ASK2] in final chapter
  cast file:          <yes — N characters / no — single-protagonist default>
next:
  set -a && . .env && set +a && \
    .venv/bin/python -m pipeline.skill_dispatch render --cmd historyrecapped/scripts/render_long_form.py -- \
      --channel hindutavaanimated \
      --slug <slug> \
      --aspect 16:9

audio gate first (recommended):
  same command with --stop-after audio (if supported), then run
  /critique-audio on the resulting wav. Image gen + compose proceed
  only if pronunciation, danda terminators, and closer 3-paragraph
  separation all pass.
```

If any quality gate fails, list the failures and STOP. Do not
silently ship a 10-min long-form with rushed chapters, missing CTA,
or fabricated chapter content.

## Important rules

- **Pure Hindi narration, no Sanskrit-only verses.** One-line shloka
  quotation inside Hindi prose is OK.
- **NO Shorts hook formula.** Long-form opens with scene-setting
  ("व्रज-भूमि। सावन का महीना…"), not "जब X ने Y कर दिया".
- **Devanagari-only CTA** in the narration body. Latin "Like /
  Comment / Subscribe" causes IndicF5 to silently drop the trailing
  chunk — confirmed 2026-05-07 on krishna-govardhan-dharan.
- **Two `[ASK]` markers**, exactly. One at ~25-30%, one in the final
  chapter just before lesson.
- **8-12 chapters, 60-90 panels.** Outside the band → schema
  validation fails.
- **Each chapter has a Devanagari title + English title + 1-line
  Hindi summary.** Used by the renderer for chapter markers in
  the YouTube description.
- **Cast file required when** the long-form has 2+ named characters
  with distinct visual signatures. Long-form character drift across
  60+ panels is brutally visible; the seed-locked cast file is the
  fix.
- **Visual variety check** — no two adjacent panels share
  `scene_kind`. Vary shot size every 2-3 panels.
- **Phonetic respellings** are auto-skipped on the IndicF5 cloud
  path (`pipeline/tts/text_normalize.py:_HINDI_TATSAMA_RESPELLINGS`
  + `synthesize(skip_hindi_respellings=True)` for cloudrun_indicf5).
  No manual respelling required for IndicF5; informational only for
  Kokoro fallback.
- **Devanagari NEVER appears in image prompts** (single ॐ exception).
- **No quoted phrases in `notes:` fields.** Diffusion text-leak.
- **Render via `historyrecapped/scripts/render_long_form.py
  --channel hindutavaanimated --aspect 16:9`** — channel-parametric
  long-form renderer. Don't fork a per-channel renderer.
- **Cloud TTS = `cloudrun_indicf5`, fallback Kokoro `hf_alpha`.**
  Cloud image = `cloudrun_flux2_klein`, fallback local mflux. The
  `long_form_animated:` config block in `hindutavaanimated/config.yaml`
  is the source of truth — don't override.
- **Always use `.venv/bin/python`** for any helper commands.
- **Source `.env` in the same bash invocation** for cloud env vars
  (set -a && . .env && set +a). The website server auto-loads .env
  at startup but a fresh shell does NOT.
- **Never run TTS / image gen / ffmpeg in this skill.** That is the
  renderer's job. This skill produces JSON only.
- **Never call the Anthropic SDK directly.** LLM calls go through
  `pipeline/llm.py` (`claude -p`) per memory
  `feedback_llm_via_claude_cli.md`.
- **Never bypass the website-first workflow** for production
  renders — `/make-hindutava-long` produces JSON; the website
  triggers render. The CLI handoff above is for explicit-batch
  authoring sessions.

## Learnings from prior runs

<!-- empty on day 1; appended on every regression via §8 -->

## Why this skill is separate from /make-hindutava-short and /make-katha

**Vs. `/make-hindutava-short`:** different format, schema, and
renderer. Shorts is 50-60s with 13 staccato beats targeting the YouTube
Shorts feed (vertical 9:16, swipe-away mute-mode default). Shorts has a
hook formula ("जब X ने Y कर दिया") that's kill-on-sight here. Shorts
output is `narrations/<slug>.json:beats[]` consumed by
`scripts/make_shorts.py`. Long-form output is
`narrations/<slug>.json` + `chapters/<slug>.json:chapters[].panels[]`
consumed by `historyrecapped/scripts/render_long_form.py --aspect
16:9`. Forking the schema costs less than wedging two incompatible
length bands into one rewriter.

**Vs. `/make-katha`:** same channel, same Hindi narration register,
same long-form duration ballpark — but **footage strategy is the
incompatible primitive**. `/make-katha` is 50-70 min footage-only
(Wikimedia / archive.org / CC0 stock photography of temples, devotional
iconography, Ken-Burns on still icons), NO AI image gen. The kathaa
aesthetic is meditative-calming with a slow lecturing voice for sleep-
or-meditation viewing. `/make-hindutava-long` is 10-15 min animated
Amar Chitra Katha AI panels with a dramatic-storytelling voice for
active engagement. Different audience, different aesthetic, different
visual primitive — the fork is justified.

Both `/make-hindutava-short` and `/make-hindutava-long` share 90% of
the channel substrate (TTS routing, cast file convention, Devanagari
caption pipeline, banned-phrase list, image style prefix) — that's
inherited via `hindutavaanimated/config.yaml` + `hindutavaanimated/learnings/`,
not duplicated in this SKILL.md.

learnings_consulted:
  - hindutavaanimated/learnings/channel.md
  - hindutavaanimated/learnings/skill_make_hindutava_short.md
  - hindutavaanimated/learnings/long_form_kathaa.md
  - hindutavaanimated/learnings/hindi_tts_prosody.md
  - hindutavaanimated/learnings/devanagari_unicode_regex.md
  - hindutavaanimated/learnings/non_latin_text_via_compose.md
  - hindutavaanimated/learnings/diffusion_quoted_phrase_leak.md
  - hindutavaanimated/learnings/whisper_chunked_hindi_forced.md
  - hindutavaanimated/learnings/whisper_hindi_undercount.md
  - hindutavaanimated/learnings/sung_vs_prose_kand.md
  - hindutavaanimated/config.yaml
  - historyrecapped/learnings/long_form_support_asks.md (2-asks cadence)
  - historyrecapped/learnings/long_form_captions.md (sentence-level captions)
  - historyrecapped/learnings/long_form_visual_signature.md (Ken-Burns motion)
  - historyrecapped/learnings/long_form_trim_aspect_short_circuit.md
  - docs/voice_catalog.md (named-voice catalog)
  - docs/av_sync_invariants.md (pipeline owns A↔V sync)
  - docs/render_deadline_policy.md (25-min kill deadline)
  - docs/research/hindi_voice_refs_2026_05_07.md (voice ref selection)
  - memory/feedback_pipeline_owns_av_sync.md
  - memory/feedback_voice_catalog_default.md
  - memory/feedback_voice_ref_must_have_pitch_variance.md
  - memory/feedback_25min_render_deadline.md
  - memory/skill_make_hindutava_short.md (sibling Shorts skill)
  - memory/skill_make_katha.md (sibling kathaa skill)

---

## Cloud pre-render hook (mandatory)

Before handing off to `pipeline/render/<entrypoint>.py`, do the
**routing assertion** documented in
[`docs/cloud_prerender_hook.md`](/Users/rohit/ytFactory/docs/cloud_prerender_hook.md):
read the channel `config.yaml` (and variant YAML if applicable) and
assert `tts_provider` + `image_provider` start with `cloudrun_`
(except for documented local-only paths like
`mystoriesanimated/variants/tifu.yaml` and the Hindi `kokoro hf_alpha`
fallback).

**Pre-warm is now automatic** — the renderer entrypoints call
`pipeline.cloud.warm.warm_async(channel)` immediately after argparse,
so the 5-7 min cold-load happens in parallel with the renderer boot.
**Health is now in the admin tab** — `/app/cloud` (sidebar → Cloud)
shows green/yellow/red live; for CI use `/api/cloud/health`. The
`warm-cloud`, `cloud-health`, `cloud-cost`, and
`deploy-cloud-service` skills were retired on 2026-05-10; same code
lives in `pipeline/cloud/` + the admin tab.
