---
name: make-hindutava-short
description: Author a 50-60s Hindi mythology Short for the HindutavaAnimated channel — Mahabharat / Ramayan / Puraan / lesser-known katha. Validated 12-beat arc (hook → name reveal → setup → rising action → twist → centerpiece → lesson → CTA), pure-Hindi narration (no Sanskrit-only — rejected after the karmanye-vadhikaraste mock), Amar Chitra Katha visuals via Cloud Run FLUX.2 klein, IndicF5 cloud TTS storyteller voice ref, Devanagari captions overlaid by pipeline.captions (never baked into diffusion). Produces narrations/<slug>.json + optional cast/<slug>.json, then hands off to scripts/make_shorts.py --channel hindutavaanimated/config.yaml --script <path>. Use when the user says "make me a hindutava short", "Mahabharat short on <X>", "Ramayan short on <Y>", "Krishna leela short", "next mythology short", "abhimanyu / karna / hanuman / eklavya next". For 50-70 min long-form kathaa use /make-katha. For MyStoriesAnimated Shorts (AITA / TIFU / wiki / TIH) use /make-mystories-short.
---

# /make-hindutava-short — 50-60s Hindi mythology Short for HindutavaAnimated

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

This skill authors **one dramatic Hindu mythology episode** as a 50-60s
YouTube Short for **HindutavaAnimated**. The format is locked from the
4 shipped exemplars (`abhimanyu-chakravyuh`, `hanuman-sanjivani-parvat`,
`eklavya-guru-dakshina`, `karna-kavach-kundal`): a 12-beat arc with a
hook ("जब X ने Y कर दिया" formula), name reveal, setup, rising action,
twist, **iconic centerpiece**, lesson, and Hindi-CTA-plus-blessing
closer. Pure Hindi prose narration; no Sanskrit-only; no
"नमस्कार दोस्तों" YouTube-vlogger opener.

You wear three hats: **Mythology Researcher → Kathaa-Vyaas (writer) →
Visual Director (per-beat scene notes for image gen)**.

The output is one or two JSON files —
`hindutavaanimated/narrations/<slug>.json` (always) and
`hindutavaanimated/cast/<slug>.json` (only if the story has named
characters that need a per-story face lock). Stages 4-7 (TTS, image
gen, compose) live in `pipeline.render.shorts` via
`scripts/make_shorts.py`, not in this skill.

## How to run it

> **CLOUD-TTS RULE (2026-05-07):** This skill's render path reads
> `tts_provider:` from the channel YAML. That field MUST be a
> `cloudrun_*` engine (chatterbox / f5 / higgs / cosyvoice /
> indicparler / indicf5). Local providers (`kokoro` / `f5_tts` /
> local `chatterbox`) are automatic-fallback paths only, NEVER the
> default. The renderer hard-fails at preflight on any local
> provider unless `LOCAL_TTS_OVERRIDE=1`. If a render comes back
> with audibly degraded voice, FIRST check `<channel>/config.yaml`
> for a misconfigured `tts_provider:` block (Shorts top-level vs
> long-form sub-block) and fix it before re-rendering. Source of
> truth: [`docs/cloudrun_tts.md`](/Users/rohit/ytFactory/docs/cloudrun_tts.md)
> + [`docs/tts_stack.md`](/Users/rohit/ytFactory/docs/tts_stack.md).
> Voice refs deployed at `pipeline/voice_refs/`: `sarah.wav` (female,
> default English), `michael.wav` (male, sports-doc default).

> **SCOPING RULE (2026-05-07):** Stage-1 / scope-confirmation MUST use
> `AskUserQuestion` with **2-4 prefilled options** grounded in
> channel exemplars (`hindutavaanimated/narrations/*.json` —
> abhimanyu / hanuman / eklavya / karna shipped) + canonical
> not-yet-covered Mahabharat / Ramayan / Puraan episodes
> (Krishna-Govardhan, Drona's death, Bhishma's bed of arrows,
> Lakshman-Surpanakha, Hanuman lifting Sanjeevani, Parashuram axe,
> etc.). Never ask the user to type the episode name — propose 3-4.
> Source of truth: [`docs/skill_prefilled_options.md`](/Users/rohit/ytFactory/docs/skill_prefilled_options.md).

### 1. Confirm episode + epic

Lock the **4-line spec** before writing anything. Ask one consolidated
clarification only if the spec is ambiguous; otherwise state your
reading back in one sentence and let the user redirect.

- **Epic** — `mahabharat` | `ramayan` | `bhagavat-puraan` |
  `shiv-puraan` | `vishnu-puraan` | `krishna-leela` (BhagavatPuraan
  Vrindavan/Mathura subset). Default to `mahabharat` if unspecified —
  the channel's deepest catalog is Mahabharat episodes.
- **Episode** — the specific dramatic moment. e.g.
  `bheeshma-iccha-mrityu`, `draupadi-cheer-haran`,
  `lakshman-rekha`, `ravan-das-mukh`, `kaaliya-naag-mardan`,
  `govardhan-parvat`, `shabri-ke-ber`. The spec must name a SCENE,
  not a character ("Krishna" alone is not an episode — pick the leela).
- **Slug** — `<character>-<episode>` lowercase-hyphen. Match the
  shipped pattern: `karna-kavach-kundal`, `eklavya-guru-dakshina`,
  `abhimanyu-chakravyuh`, `hanuman-sanjivani-parvat`. NO dates in
  Shorts slugs (long-form kathaa uses dated slugs; Shorts don't).
- **Lesson hindi** — the one-line takeaway in pure Hindi that lands
  in beat 11. e.g. "सच्चा दान वो होता है — जो खुद को कीमत देकर भी
  दिया जाए।" Write this FIRST — the whole arc bends toward it.

Output of stage 1:

```
epic:    mahabharat
episode: bheeshma-iccha-mrityu
slug:    bheeshma-iccha-mrityu
lesson:  "मरना नहीं — मौत को चुनना। यही असली योद्धा है।"
```

**Refuse** if the user names a SUNG-tradition text framed as paath
(Hanuman Chalisa, Sundarkand sung doha-chaupai, aartis, bhajans). For
Shorts, the audience expects a dramatic *summary*, not a chanted paath
— so you can still treat e.g. "Hanuman lifting the parvat" as a
Sundarkand-derived dramatic Short (see shipped `hanuman-sanjivani-parvat`).
But the Short cannot be pitched as "the Hanuman Chalisa". Mention this
distinction in one line if the trigger is ambiguous.

### 2. Hat 1 — Mythology Researcher: build a tiny dossier

For the chosen episode, gather:

- **Canonical source** — Gita Press editions (Geeta, Ramayan,
  Mahabharat) preferred; Wikipedia acceptable as a *secondary outline
  source*, never the only source. Cite the parva / kaand / adhyay
  in `metadata.epic_section` (e.g. `"Mahabharat, Vana Parva — Karna
  gives away his immortality"`).
- **Characters on screen** — proper-noun list (3-7 names typical).
  Goes into `metadata.characters_on_screen` AND drives the cast file
  if any character needs a face lock.
- **Visual centerpiece** — the ONE iconic frame the whole Short
  pivots on (severed thumb on a leaf, kavach being cut off the
  chest, mountain held overhead, conch raised on the chariot).
  This goes into beat 9 with `scene_kind: "centerpiece"` and a
  detailed `notes` field.

**Refuse to fabricate.** If the user names an episode you can't
ground in canonical text in 60s of research, say so and offer
alternatives. Devotional viewers fact-check.

### 3. Hat 2 — Kathaa-Vyaas: write the 12-beat narration

**Word budget:** 130-160 Hindi words total for 50-60s at IndicF5's
measured cadence (~155-165 wpm at speed 1.0 — slightly faster than
Kokoro hf_alpha which historically banded 105-115 wpm). Don't trust
intuition on length scaling — the budget gate in §6 is mechanical.

**Validated 12-beat template** (from `hindutavaanimated/learnings/channel.md`):

| Beat | Role | Duration | Word target |
|---|---|---|---|
| 0 | Hook — "जब X ने Y कर दिया" formula | 3-4s | 8-15 |
| 1 | Name reveal — short, confident ("एकलव्य।" / "हनुमान।") | 2-3s | 3-8 |
| 2 | Setup — who/where | 4-6s | 10-15 |
| 3 | Setup — what's at stake | 4-6s | 10-15 |
| 4 | Rising action — escalation | 5-7s | 12-18 |
| 5 | Rising action — escalation | 4-6s | 10-15 |
| 6 | Rising action — turn toward the moment | 5-7s | 12-18 |
| 7 | Twist — the antagonist's calculation | 4-6s | 10-15 |
| 8 | Pre-centerpiece beat — recognition / smile | 4-5s | 8-13 |
| 9 | **CENTERPIECE** — THE iconic visual; ≥4s hold; ≥10 words narration | 4-7s | 12-22 |
| 10 | Stoic aftermath — the cost paid | 3-5s | 6-12 |
| 11 | Lesson — short Hindi punchline (the `lesson_hindi` from §1) | 3-5s | 8-15 |
| 12 | CTA + blessing — "अगर ये कथा पसंद आई हो — लाइक करें, कमेंट में लिखें, सब्सक्राइब करें। जय श्री कृष्णा।" | 5-7s | 14-20 |

**Hook formula** — proven across 4 shipped episodes:

> "जब [unexpected subject] ने [extreme action]"

Sets the "you won't believe this" tension. Always reveal the
protagonist's NAME in beat 1, never in the hook. The hook is a
provocative claim; beat 1 lands the name with weight.

**Centerpiece rule** — explicitly mark beat 9 as `scene_kind:
"centerpiece"` AND mark its `notes:` field with `"THE ICONIC FRAME"`
and a 1-sentence visual brief. Write ≥10 words of beat-9 narration so
the script-proportional time allocation gives it ≥4s hold. The visual
must read on mute (severed thumb on leaf, kavach being cut, parvat
overhead, etc.).

**Closer rule** (literal from `hindi_tts_prosody.md` L18 — three
discrete paragraphs, NOT em-dash-fused):

```
<beat 11 — lesson, alone>

<beat 12 — CTA + blessing, comma-period-joined NOT em-dash>
```

The CTA itself is the LITERAL string the channel ships:

> "अगर ये कथा पसंद आई हो — लाइक करें, कमेंट में लिखें, सब्सक्राइब करें। जय श्री कृष्णा।"

Or `जय श्री राम।` for Ramayan, `हर हर महादेव।` for Shiv-Puraan,
`जय श्री हरि।` for Vishnu-Puraan, `जय श्री कृष्णा।` for everything
else (the Mahabharat / Bhagavat-Puraan / Krishna-leela default).

**Banned phrasings** (kill-on-sight):

- Sanskrit-only narration (channel rejected this 2026-05-03 after the
  karmanye-vadhikaraste mock — `hindutavaanimated/learnings/channel.md`
  "locked format" section). One-line shloka quotation inside Hindi
  prose is OK; pure-Sanskrit verse-after-verse is not.
- "नमस्कार दोस्तों" / "हैलो दोस्तों" / "हाय दोस्तों" — YouTube-
  vlogger opener. The Short opens with the HOOK, not a host greeting.
- "smash that subscribe button" / "vote in comments" / verdict
  acronyms (AITA/YTA/NTA — not relevant here, but still in the
  cross-channel banned list).
- Quoted descriptive phrases that will leak into image prompts —
  see §4 below and `diffusion_quoted_phrase_leak.md`.

**Prosody table** — every shipped episode has a `narration_prosody`
array that drives per-sentence speed modulation and pause budget.
Match the shipped pattern (see `karna-kavach-kundal.json:5-29`):

```json
"narration_prosody": [
  {"text": "<sentence>", "speed": <0.72-0.92>, "post_pause_s": <0.0-0.6>, "tag": "<short-tag>", "paragraph_end": <bool>}
]
```

Pause budget rules (literal from
`hindutavaanimated/learnings/hindi_tts_prosody.md`):

- Total pause budget ≤5% of audio duration.
- Concentrate at paragraph boundaries (`paragraph_end: true` →
  `post_pause_s: 0.4-0.6`); non-boundary `post_pause_s` ≤0.15s
  (use 0.20-0.30s only for intra-paragraph dramatic micro-beats).
- The source `narration` string MUST have matching `\n\n`
  paragraph breaks at every `paragraph_end: true` point. The
  pipeline's per-paragraph stitcher and `audio_critic.py` L3 lens
  both check this.
- Speed-down on weight words (0.72-0.78) for the hook, name reveal,
  smile-beat, and the centerpiece line. Speed up the CTA only
  (~0.92), never above 1.0.

**Phonetic respellings** — every Mahabharat / Ramayan / Puraan
proper noun or tatsama word in the narration MUST either:

1. Already exist in `pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS`
   (check before adding), or
2. Be added there as a **hyphen-forced** form (NOT vowel-elongation,
   per `hindi_tts_prosody.md` validated 2026-05-03), e.g.
   `अभि-मन्यु`, `महा-रथियों`, `सब-स्क्राइब`. Final consonants need
   explicit halant (`बाण → बाण्`); anusvara/chandrabindu (ं/ँ) get
   dropped silently — respell explicitly (`साँस → सान्स`).
3. Otherwise appear in `narrations/<slug>.json:pronunciation_dict`
   as a per-story override.

**No regex-on-narration without `\w` UNICODE.** If you write any
helper that touches the narration text, default to `\w` with
`re.UNICODE` and include `।॥` alongside `.!?` in any sentence
terminator set. Three separate places in `pipeline/` had this bug
in 2026-05-03 (`devanagari_unicode_regex.md`).

### 4. Hat 3 — Visual Director: per-beat scene notes

Each beat with a meaningful image gets a `notes:` field describing
the **visual content of the frame** — what the diffusion model
should render. Channel uses `image_provider: cloudrun_flux2_klein`
(FLUX.2 klein 4B on Cloud Run, with `image_style_prefix` from
`config.yaml` auto-prepended) and falls back to local mflux. Per-
character face lock comes from `cast/<slug>.json` if present.

**Diffusion safety rules** (literal from
`hindutavaanimated/learnings/diffusion_quoted_phrase_leak.md` and
`non_latin_text_via_compose.md`):

1. **Never quote phrases inside `notes:`**, even when describing
   composition style. Replace `Composition like a 'this is where
   you must go' map-panel.` with `Iconographic map-style
   composition.` Diffusion treats quoted phrases as text-to-render
   targets, regardless of "NO TEXT" guardrails.
2. **Never reference caption / title / text overlay locations.**
   Replace `lower-third negative space for the caption` with
   descriptive visual language only — `lower-third dominated by
   warm gold and saffron tones`. The diffusion model reads "where
   text goes" and fills it with hallucinated Devanagari glyphs.
3. **No Devanagari in image prompts.** Multi-character Devanagari
   collapses (कृ→कु conjunct collapse, श्री omitted entirely).
   The single ॐ glyph is the documented exception (icon_03_om_lotus
   trained as a graphic). All on-image text is overlaid by
   `pipeline.captions` post-diffusion.
4. **Reduce ensembles.** Diffusion can't render N≥4 distinct named
   figures cleanly (channel learning). Reframe to "1-2 named
   characters + silhouette wall" — see how the kaurava_seven cast
   in `abhimanyu-chakravyuh` and the kaurava-silhouette wall in
   `hanuman-sanjivani-parvat` beat 02 handle this.
5. **No "broken/torn/shattered" applied to clothing/objects.** Use
   "wreckage scattered around" instead. Channel learning, validated
   on the karna kavach-cut beat which used "Karna cutting away
   golden armor … blood visible but iconographic restraint."

**Centerpiece beat (9) gets the longest notes** — describe the iconic
frame in 1-2 sentences. This frame ships at `≥4s hold` so it has the
budget for detail.

**Last beat (12) — the CTA panel** — set `"notes": "Devotional
namaste closer. NO TEXT in image."` The closer panel (3-row LIKE /
COMMENT / SUBSCRIBE) is composed by `pipeline.captions.render_closer_panel`
post-diffusion — driven by `closer_format` in `config.yaml`
(`"जय श्री कृष्णा 🙏  SUBSCRIBE"`).

### 5. Cast handling

Most shipped episodes did NOT need a per-story cast file — the
character description in `config.yaml:character_description`
("Mahabharat-era Indian warrior, fair-skinned, dark wavy hair…")
was enough for single-protagonist Mahabharat episodes.

You DO need a `cast/<slug>.json` when:

- The episode has 2+ named characters with distinct visual
  signatures (Karna + Indra-disguised-as-brahmin, Eklavya + Drona,
  Hanuman + Sushena + Lakshman, Krishna + Putana). Lock per-
  character `description`, `palette`, `default_emotion`, and
  optional `seed` to prevent face drift across beats.
- Female characters (Sita, Draupadi, Yashoda, Putana) — config.yaml
  default is male warrior; female faces drift without an override.
- Non-human forms (Hanuman as monkey-warrior, Kaaliya as serpent-
  king, Shesha-naag, Garuda) — config.yaml default doesn't apply.

Schema (matches the shipped long-form cast files at
`hindutavaanimated/cast/krishna-balleela-vrindavan-katha-202605.json`):

```json
{
  "<character_slug>": {
    "description": "<2-4 sentence visual description; bake the channel's Amar Chitra Katha style cues — bold black ink outlines, sindoor red / peacock blue / marigold yellow / antique gold>",
    "palette": ["sindoor red", "peacock blue", "antique gold"],
    "default_emotion": "<measured-reverent | warrior-resolute | ...>",
    "seed": <int, optional — for character-lock across beats>
  }
}
```

The cast tokens get inlined into image prompts via the channel's
existing `cast_locked_tokens` infra (used by sportsrecapped;
the same pipeline.images path picks up cast files automatically when
they exist at `<channel>/cast/<slug>.json`).

### 6. Quality gates (run BEFORE renderer — mechanical)

Each gate blocks emit on hit. Run in order.

1. **Banned-phrase scan** — grep `narration` against the channel's
   banned list: Sanskrit-only verses, "नमस्कार दोस्तों" / "हैलो
   दोस्तों" / "हाय दोस्तों" YouTube-vlogger openers, "smash
   subscribe", "vote in comments", verdict acronyms. Block on hit.
2. **Hindi-only assertion** — narration must contain ≤2% ASCII
   word characters (only allowed: SUBSCRIBE / Like / Comment / a
   single optional emoji per `shorts_caption_emoji_density.md`).
   Use `\w` UNICODE per `devanagari_unicode_regex.md`. >2% blocks.
3. **Pronunciation pre-pass** — every proper noun in
   `metadata.characters_on_screen` ∪ tatsama words in narration
   must either match an existing entry in
   `pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS` or appear in
   `narrations/<slug>.json:pronunciation_dict`. Memory:
   `feedback_pronunciation_pretts.md` + channel learning
   `hindi_tts_prosody.md`.
4. **Length budget** — Hindi word count must satisfy
   `duration_target_s × wpm_target / 60` within ±10%. Target
   wpm = 160 for IndicF5 storyteller voice; 110 for Kokoro
   fallback. duration_target_s = 50-60s band per `config.yaml`.
   Outside that, hard-fail.
5. **12-beat structure** — exactly 12-13 beats; beat 0 is hook;
   beat 1 is name-reveal (≤8 words); beat 9 has
   `scene_kind: "centerpiece"` AND ≥10 words of narration AND
   `notes` containing "ICONIC" or "centerpiece"; final beat is
   CTA-with-blessing. Outside that, hard-fail.
6. **Closer 3-paragraph rule** (literal from `hindi_tts_prosody.md`
   L18) — `narration` MUST have `\n\n` between (lesson, blessing,
   CTA). Em-dash-fused closers fuse on the TTS phoneme pass into
   one breathless run and block emit.
7. **CTA literal match** — beat-12 narration must contain "लाइक
   करें" + "कमेंट में लिखें" + "सब्सक्राइब करें" + the epic-
   appropriate blessing line. **Devanagari only — no Latin tokens
   in the CTA**: IndicF5 production render 2026-05-07 silently
   dropped the entire closer chunk (lesson + CTA + blessing) when
   it hit Latin "Like / Comment / Subscribe" mid-Hindi-paragraph,
   confirmed via /critique-audio score=3. Variants block emit (the
   channel ships exactly this pattern).
8. **Centerpiece-hold sanity** — `beats[9].duration_s ≥ 4.0` AND
   `beats[9].narration` word count ≥10. Channel learning: hold
   <4s on the centerpiece tanks comprehension on mute.
9. **Diffusion-prompt safety scan** — for every `beats[i].notes`
   field, reject if it contains: any `'…'` quoted phrase; the
   substrings "caption", "title", "text", "writing", "where text",
   "negative space" (per `diffusion_quoted_phrase_leak.md`); any
   Devanagari character (per `non_latin_text_via_compose.md` —
   except the single ॐ glyph). Block on hit.
10. **/critique-audio gate** — run on a synth of beat-0 + beat-1
    + beat-12 (hook + name + closer) BEFORE green-lighting the
    full image-gen run. TTS bugs (tatsama collapse, danda-skip,
    closer fusion) invalidate ALL downstream image work. Memory:
    `feedback_critique_audio_before_image_gen.md`. Run via:

    ```bash
    .venv/bin/python -m pipeline.skill_dispatch render \
      --channel hindutavaanimated/config.yaml --script <path> \
      --stop-after audio
    ```

    (If `--stop-after audio` doesn't exist yet, surface that as a
    pipeline follow-up — synth one short clip standalone via
    `pipeline.audio` and critique it manually.)
11. **Aspect + voice + image-provider match** — assert
    `config.yaml:output_resolution == [1080, 1920]`,
    `tts_provider == "cloudrun_indicf5"` (with Kokoro fallback OK),
    `image_provider == "cloudrun_flux2_klein"`,
    `script_check_strict: false` (Hindi narration doesn't match
    `pipeline/llm/script_check.py`'s AITA-tuned English CTA / hook
    regexes; without this flag, missing_cta + weak_hook fire as
    ERRORS and abort the render — first hit 2026-05-07 on
    krishna-govardhan-dharan). Drift blocks emit.
12. **Source-fidelity check** — `metadata.epic_section` names a
    canonical parva / kaand / adhyay / leela. Bare "Mahabharat" or
    "Hindu mythology" without a section is too vague — block.

### 7. Renderer handoff

The CLI needs the cloud-TTS / cloud-image URLs from `.env`, which
the website server auto-loads at startup but a fresh shell does
NOT (no `python-dotenv`). Source `.env` in the same bash invocation
or `cloudrun_indicf5` raises `RuntimeError: ... requires
CLOUDRUN_TTS_INDICF5_URL` (first hit 2026-05-07 on
krishna-govardhan-dharan):

```bash
set -a && . .env && set +a && \
  .venv/bin/python -m pipeline.skill_dispatch render \
    --channel hindutavaanimated/config.yaml \
    --script hindutavaanimated/narrations/<slug>.json
```

`source_adapter: manual` in `hindutavaanimated/config.yaml`
short-circuits stages 1-3 (no pull / no LLM rewrite / no auto-source) —
the renderer consumes the pre-authored `narrations/<slug>.json`
directly.

**Voice — named via the catalog:** the channel uses
`tts_voice: hindi-female-storyteller` (resolved by
`pipeline/voice_catalog.py` against `pipeline/voice_refs/catalog.yaml`)
with `tts_provider: cloudrun_indicf5`. The cloud chunker applies
**voice-clone anchoring by default**: chunk 0 synthesises with the
catalog ref WAV; chunks 1..N use chunk 0's audio as the ref, so all
paragraphs clone the speaker established in paragraph 0 → consistent
narrator across the full 60-70s render. See
[`docs/voice_catalog.md`](/Users/rohit/ytFactory/docs/voice_catalog.md)
for adding new voices.

The renderer will:

1. Synthesize TTS via Cloud Run IndicF5 (storyteller voice ref auto-
   loaded from `tts_voice` path in `config.yaml`); falls back to
   local Kokoro `hf_alpha` on cloud failure (the only on-laptop
   Hindi voice).
2. Whisper-transcribe the audio with chunked + forced `language='hi'`
   per `whisper_chunked_hindi_forced.md` (recovers 2-3× more words
   than plain Whisper on dense Hindi, renders clean Devanagari
   without Telugu-glyph drift).
3. Word-clamp ≤1.5s + script-proportional beat allocation per
   `whisper_hindi_undercount.md` (Whisper-mlx-4bit transcribes ~50%
   of dense Hindi; the clamp + fallback survives both undercount and
   normal cases).
4. Generate images via Cloud Run FLUX.2 klein (Amar Chitra Katha
   style prefix auto-prepended); falls back to local mflux on cloud
   failure. Per-render circuit breaker: first cloud failure trips
   module-global flag → all subsequent calls skip cloud.
5. Compose word-level Devanagari captions via `pipeline.captions`
   (Devanagari Sangam MN auto-picked by the script-aware font probe).
6. Mix optional BGM at 12% under narration with 1.5s fade-out (if
   a Suno-generated mellow Hindu instrumental sits at
   `hindutavaanimated/music/<slug>.wav`).
7. Overlay the closer panel (`closer_format: "जय श्री कृष्णा 🙏
   SUBSCRIBE"` from `config.yaml`) on the last 4s.
8. Auto-upload as configured (`upload.privacy: private` is the
   current default — flip to `public` via the website when ready).

Render time: ~6-12 min on a warm pipe (cloud TTS + cloud image both
auto-scale; FLUX.2 klein at `--min-instances=1` is always-warm).

### 8. Self-learning hook

After the user runs `/critique-audio` on the audio sample OR
`/critique-video` on the final mp4:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo in this script, this beat's notes were
     wrong) → fix in `narrations/<slug>.json` and re-render the
     affected stage; append a 1-line note to
     `.claude/skills/make-hindutava-short/learnings/_index.md`.
   - **CLASS-OF-BUG** (every future Hindi mythology Short will hit
     this) → fix in the right place:
     * Pronunciation collapse on a tatsama not in the table → add
       to `pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS`.
     * New diffusion leak pattern → extend the §6.9 prompt-safety
       scan AND `hindutavaanimated/learnings/diffusion_quoted_phrase_leak.md`.
     * Whisper drift on a new audio profile → extend
       `pipeline.beats.transcribe_words` per
       `whisper_chunked_hindi_forced.md` Production TODO.
     * Beat-structure regression → tighten §6.5 in this SKILL.md
       AND mirror to `hindutavaanimated/learnings/<topic>.md`.
   - Then append a regression note to
     `.claude/skills/make-hindutava-short/learnings/<topic>.md` AND
     mirror to `hindutavaanimated/learnings/<topic>.md` per
     CLAUDE.md dual-save.
2. Update `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`
   index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a pre-render
   quality gate in §6 that blocks emit on detection.

**FIRST-RUN watch-list** (the most likely first-bug surfaces, derived
from the 4 shipped episodes' regression history):

- New proper noun not in `_HINDI_TATSAMA_RESPELLINGS` → tatsama
  conjunct collapse on Kokoro fallback (e.g. `बहुधा`, `विद्वान`,
  `अश्वत्थामा`). Add hyphen-forced respelling.
- New ensemble scene with N≥4 named figures → reframe to "1-2 named
  + silhouette wall" before image gen, not after.
- Non-male / non-warrior protagonist → write a `cast/<slug>.json`
  before the first image-gen pass; female faces drift on the
  config.yaml male-warrior default.
- Whisper undercount on the IndicF5 sample (new voice, untested) →
  if `transcribe_words().count() < 0.7 × script.word_count`,
  switch the channel default to chunked-Whisper-forced-Hindi (the
  fix is already implemented per `whisper_chunked_hindi_forced.md`,
  may just need to be wired into `pipeline.beats` from the
  /tmp-deleted harness).

### 9. Report back

```
✓ wrote narration: hindutavaanimated/narrations/<slug>.json
✓ wrote cast:      hindutavaanimated/cast/<slug>.json   (only if needed)
  epic:           <epic>
  episode:        <episode_label>
  slug:           <slug>
  beats (N=12-13): hook → name → setup → rising → twist → centerpiece → lesson → CTA
  centerpiece:    beat 9, "<one-line iconic frame brief>"
  word count:     <N> Hindi words → ~<dur>s @ <wpm> wpm
  CTA + blessing: "<literal CTA + epic-appropriate blessing>"
  cast file:      <yes — N characters / no — single-protagonist default>
next:
  .venv/bin/python -m pipeline.skill_dispatch render \
    --channel hindutavaanimated/config.yaml \
    --script hindutavaanimated/narrations/<slug>.json

audio gate first (recommended):
  same command with --stop-after audio (if supported), then run
  /critique-audio on the resulting wav. Image gen + compose proceed
  only if pronunciation, danda terminators, and closer 3-paragraph
  separation all pass.
```

If any quality gate fails, list the failures and STOP. Do not
silently ship a Mahabharat Short with Sanskrit-only narration,
pure-vlogger opener, fused closer, or diffusion-leaked English/
Devanagari text in the image.

## Important rules

- **Pure Hindi narration, no Sanskrit-only verses.** One-line shloka
  quotation inside Hindi prose is OK; verse-after-verse is not. Channel
  rejected this 2026-05-03.
- **Hook formula is "जब X ने Y कर दिया".** Validated across 4 shipped
  episodes. Don't drift to "एक बार की बात है" or "क्या आप जानते हैं".
- **Name reveal goes in beat 1, not the hook.** The hook teases; beat
  1 lands the name.
- **Closer is THREE discrete paragraphs:** lesson, blessing, CTA. NOT
  em-dash-fused — TTS fuses em-dash into one breathless run. Channel
  learning `hindi_tts_prosody.md` L18.
- **CTA is the literal channel string** + the epic-appropriate
  blessing (`जय श्री कृष्णा।` for Mahabharat / Bhagavat / Krishna,
  `जय श्री राम।` for Ramayan, `हर हर महादेव।` for Shiv-Puraan,
  `जय श्री हरि।` for Vishnu-Puraan). Don't paraphrase.
- **Centerpiece beat (9) is the iconic visual.** ≥10 words of
  narration, ≥4s hold, `scene_kind: "centerpiece"`, `notes:`
  containing "ICONIC" or "centerpiece". Hold <4s tanks
  comprehension on mute.
- **Phonetic-respell every tatsama and proper noun.** Hyphen-forced
  syllables in `_HINDI_TATSAMA_RESPELLINGS`, never vowel-elongation.
  `अभि-मन्यु`, NOT `अभीमन्यू`.
- **Pause budget ≤5%, concentrated at paragraph boundaries.**
  Non-boundary `post_pause_s` ≤0.15s; `paragraph_end: true` →
  0.4-0.6s.
- **Every regex on narration uses `\w` UNICODE** AND treats `।॥` as
  sentence terminators alongside `.!?`. Three separate places hit
  this bug in 2026-05-03; assume the fourth is one prompt away.
- **Devanagari NEVER appears in image prompts.** Even single-line
  proper nouns. Diffusion can't render Indic conjuncts (कृ→कु
  collapse, श्री omission). Single ॐ glyph is the documented
  exception. All on-image text overlaid by `pipeline.captions`.
- **No quoted phrases in `notes:` fields.** Diffusion treats them as
  text-to-render targets regardless of "NO TEXT" guardrails.
  `diffusion_quoted_phrase_leak.md`.
- **No caption / title / text-overlay region references in `notes:`.**
  Triggers hallucinated-Devanagari fill.
- **Reduce ensembles to 1-2 named + silhouette wall.** Diffusion
  can't render N≥4 distinct figures cleanly. Channel-validated.
- **Cast file required when** the episode has 2+ named characters
  with distinct visual signatures, OR a female protagonist, OR a
  non-human form (Hanuman / Kaaliya / Garuda / Shesha).
- **Render via `scripts/make_shorts.py --channel hindutavaanimated/config.yaml
  --script <path>`** — `source_adapter: manual` short-circuits
  stages 1-3. Don't reach for the deleted `/tmp/compose_episode.py`
  harness; it was removed.
- **Cloud TTS = `cloudrun_indicf5`, fallback Kokoro `hf_alpha`.**
  Cloud image = `cloudrun_flux2_klein`, fallback local mflux. The
  config.yaml routing is the source of truth — don't override.
- **Always use `.venv/bin/python`** for any helper commands.
- **Never run TTS / image gen / ffmpeg in this skill.** That is the
  renderer's job. This skill produces JSON only.
- **Never call the Anthropic SDK directly.** LLM calls go through
  `pipeline/llm.py` (`claude -p`) per memory
  `feedback_llm_via_claude_cli.md`.
- **Never bypass the website-first workflow** for production
  renders — `/make-hindutava-short` produces JSON; the website
  triggers render. The CLI handoff above is for explicit-batch
  authoring sessions.

## Learnings from prior runs

<!-- empty on day 1; appended on every regression via §8 -->

## Why this skill is separate from /make-script and /make-katha

`/make-script` mines English-language sources (Reddit / Wikipedia /
TIH / YouTube long-form) and runs an LLM-rewrite via
`pull_stories.py` to produce a hook + 50-80 word English narration.
Hindi mythology has none of those entry points: there is no
Mahabharat subreddit to mine, no LLM-rewrite contract that respects
kathaa register, no `narration_prosody` table in the
`/make-script` schema, no per-tatsama hyphen-respelling pre-pass,
and no diffusion-leak guard for Devanagari. Forking the schema
costs less than wedging four incompatible niches into one rewriter.

`/make-katha` is the closest neighbour by language and channel —
both produce Hindi narration for HindutavaAnimated. But the
**format, duration, schema, and renderer are incompatible**:
kathaa is 50-70 min footage-only with `chapters[]`, two embedded
asks, and `render_footage_only.py --aspect 16:9` at 1920×1080.
`/make-hindutava-short` is 50-60s **AI-image-generated** with
`beats[]`, `narration_prosody[]`, a single CTA-and-blessing closer,
and `make_shorts.py` at 1080×1920. Different schema → different
quality gates → different skill. Same channel substrate (Indic
TTS / Devanagari overlay / Amar Chitra Katha visuals) is reused via
`pipeline/` and `config.yaml`, not re-implemented.

learnings_consulted:
  - hindutavaanimated/learnings/channel.md (locked 12-beat format, hook formula, closer panel, centerpiece rule, banned Sanskrit-only)
  - hindutavaanimated/learnings/hindi_tts_prosody.md (hyphen-respellings, ≤5% pause budget, closer 3-paragraph rule)
  - hindutavaanimated/learnings/devanagari_unicode_regex.md (\w UNICODE on every narration regex, `।॥` terminators)
  - hindutavaanimated/learnings/non_latin_text_via_compose.md (Devanagari overlay via pipeline.captions, never baked in diffusion)
  - hindutavaanimated/learnings/diffusion_quoted_phrase_leak.md (no quoted phrases / no "where text goes" in image notes)
  - hindutavaanimated/learnings/whisper_chunked_hindi_forced.md (chunked + force language='hi' for caption coverage)
  - hindutavaanimated/learnings/whisper_hindi_undercount.md (1.5s word-clamp + script-proportional fallback)
  - hindutavaanimated/learnings/sung_vs_prose_kand.md (SUNG-tradition framing — Shorts are dramatized summaries, not paath)
  - hindutavaanimated/config.yaml (cloudrun_indicf5 + cloudrun_flux2_klein routing, image_style_prefix, closer_format, output_resolution)
  - feedback_pronunciation_pretts.md (phonetic respelling pre-TTS)
  - feedback_critique_audio_before_image_gen.md (audio gate first)
  - feedback_engineer_class_of_bug.md (one-off vs class-of-bug classification)
  - feedback_dual_save_memory_and_docs.md (project doc + memory entry)
  - feedback_image_cache_content_hash.md (content-hashed image cache)
  - feedback_skills_kick_render_directly.md (Bash-execute make_shorts.py, don't hand off to website)
  - feedback_skill_description_1024_cap.md (frontmatter description ≤1024 chars)
  - shorts_caption_emoji_density.md (one emoji per 3-5 spoken words; cross-channel)

## Section 11 — Post-upload analysis (cross-channel rule, 2026-05-08)

After this skill's render uploads successfully (or `/critique-video`
surfaces new issues post-upload), run the conversational debrief
defined in [`docs/post_upload_analysis.md`](/Users/rohit/ytFactory/docs/post_upload_analysis.md).
Walk back through the iterations that produced this video and
classify each surprise / fix / pivot as ONE-OFF, CLASS-OF-BUG,
PIPELINE-BUG, WORKFLOW-IMPROVEMENT, or PRONUNCIATION. Save learnings
per CLAUDE.md dual-save rule (channel project doc + skill-side mirror
+ memory entry).

**Most likely topics to surface for this skill** (extend as new
learnings land):
- TTS pronunciation gaps (extend `pipeline/tts/text_normalize.py::_ACRONYM_PHRASES`)
- Photo / footage aspect inconsistency
- Image-narration sync drift
- Cache-invalidation gaps between iterations
- Closer beat / button overlay alignment
- Numerical caption transform misses
- Wikimedia File: URL miss-rate

Default to write, not skip. Small learnings compound; missing them
causes the next render to re-discover the same bug.
