---
name: make-katha
description: Author a 50-70 minute Hindu scripture kathaa for the HindutavaAnimated channel — soothing, calming, premier-quality Hindi narration of Mahabharat / Ramayan / Bhagavad Gita / Puraan chapters, with footage-only visuals (stock photography, Wikimedia / public-domain temple imagery, archive.org devotional footage, slow Ken Burns on still icons) and NO AI image gen. You become the kathaa-vyaas — investigative scripture-writer + devotional curator + audio-first showrunner — and produce narrations/<slug>.json + shotlist/<slug>.json, then hand off to historyrecapped/scripts/render_footage_only.py with --aspect 16:9. Use when the user says "make me a kathaa", "1 hour Hindu scripture video", "Gita adhyay 2 narration", "Ramayan Sundarkand", "Mahabharat Bhishma parva", or any "long-form devotional" / "calming Hindu narration" ask. For 50-60s mythology Shorts use /make-script. For Top-10 list long-form use /make-top10. For sports docs use /make-sports-doc.
---

# /make-katha — long-form Hindu scripture kathaa for HindutavaAnimated

This skill authors a **50-70 minute** devotional / meditative narration
of Hindu scripture (a parva of Mahabharat, a kaand of Ramayan, an
adhyay of the Bhagavad Gita, or a leela from a Puraan) for the
**HindutavaAnimated** channel. The format prioritises **narration
quality** above all else: pure flowing Hindi, traditional
kathaa-vyaas register, soft Kokoro `hf_alpha` voice over **footage-only
visuals** sourced from stock libraries / Wikimedia / archive.org /
broadcaster channels. **No AI image gen** — production time stays low
because there is no diffusion queue.

You wear three hats: **Scripture Researcher → Footage Curator →
Kathaa-Vyaas (writer/narrator)**.

The output is two JSON files — `narrations/<slug>.json` and
`shotlist/<slug>.json` — and a cast file. Stages 4-7 (TTS, ffmpeg,
mux) live in the renderer, not in this skill.

## How to run it

### 1. Confirm scripture + chapter + tone

If the user's prompt is partial ("make me a Gita kathaa"), state your
reading back in one sentence and let them redirect. Lock the
**5-line spec** before writing anything.

**FIRST — classify the chosen text+section as SUNG vs PROSE.** This
is a kill-on-sight blocker discovered 2026-05-05 when the first
`/make-katha` run picked Sundarkand and rendered ~30% of a 70-min
prose narration before the user caught the format mismatch. See
`hindutavaanimated/learnings/sung_vs_prose_kand.md` for the full
rationale; the short version:

- **SUNG-tradition** texts are CHANTED in their native poetic meter
  by named devotional artists (Hari Om Sharan / Anuradha Paudwal /
  Mukesh / Lallan Singh). The audience searching them on YouTube
  expects **sung paath**, not prose explanation. Kokoro hf_alpha
  cannot sing — narrating these as prose produces a flat reading
  of poetry that bounces in <60s. Block emit unless the user
  explicitly opts in to a prose-pravachan reframing AND agrees the
  SEO/title must NOT collide with the sung-paath audience.

  - **Tulsi Sundarkand** (any sarga) — sung doha+chaupai
  - **Hanuman Chalisa** — sung 40 chaupai
  - **Aartis** (Om Jai Jagdish, Ganesh aarti, etc.)
  - **Bhajans** — sung by definition
  - **Sanskrit-only shloka chanting** (Gita verses chanted in
    Sanskrit meter, not narrated)
  - **Akhand-paath / Ramcharitmanas full-paath / Bhagavat-paath**
    when framed as SUNG community recitation
  - **Devi Stuti / Vishnu Sahasranaam sung**

- **PROSE-tradition** texts are TOLD by a kathaa-vyaas in
  explanatory Hindi prose with shloka-quotation interludes. The
  audience listens to learn. Maps cleanly to the existing kathaa
  stack (Kokoro hf_alpha + footage-only render). PROCEED.

  - **Mahabharat parvas** (Bhishma / Shanti / Karna / Stree / etc.)
  - **Ramayan kand prose** (Bal-kand / Ayodhya / Aranya /
    Kishkindha — but NOT Sundarkand; that's the sung one)
  - **Bhagavat Puraan Krishna-leela** (Putana / Govardhan /
    Kaaliya-mardan / Maharaas — Bhagvat-saptaah territory)
  - **Shiv Mahapuraan stories** (Shiv-Parvati vivah, Markandeya,
    Daksh-yajna)
  - **Vishnu Puraan / Devi Bhagavat stories**
  - **Gita pravachan** — explanatory commentary on a chapter
    (Govind Dev Giri / Ranbankura Maharaj style; NOT verse
    chanting)

Output of stage 1 must include `tradition: prose` (or `tradition:
sung-pravachan-pivot` if user explicitly opts the sung text into
prose). Quality gate §6.13 (NEW) blocks emit when `tradition: sung`
slips through without override.

Lock the **5-line spec** before writing anything:

- **Text** — `mahabharat` | `ramayan` | `bhagavad-gita` |
  `bhagavat-puraan` | `shiv-puraan` | `vishnu-puraan` (others
  per user request, but the 6 above are the curatorial defaults).
- **Section** — the parva / kaand / adhyay / leela. e.g.
  `bhishma-parva-shloka-1-50`, `sundarkand-sarga-1-3`,
  `gita-adhyay-2`, `krishna-balleela-vrindavan`. The renderer is
  agnostic; the slug is the contract.
- **Tone** — `kathaa-classical` (default — measured pandit voice,
  doha-style cadence) | `meditation-soft` (slower, longer pauses,
  for sleep / driving) | `dramatic-katha` (closer to Shorts
  energy but stretched). Default to `kathaa-classical` unless the
  user names another. Sleep-mode = `meditation-soft`.
- **Sanskrit policy** — `hindi-only` (DEFAULT — matches the
  channel's `learnings/channel.md` rule that pure-Sanskrit was
  rejected after the karmanye-vadhikaraste mock) | `shloka-then-translation`
  (opt-in: read Sanskrit shloka in Devanagari, then Hindi
  bhavarth — only for Gita/Upanishad text where the verse itself
  is the point). Confirm explicitly before enabling
  shloka-then-translation; the channel's locked-format rule is
  Hindi-only by default.
- **Duration target** — 50, 60, or 70 minutes. Default 60.

Output of stage 1 — the 5-line spec:

```
text:           bhagavad-gita
section:        adhyay-2-sankhya-yog
tone:           kathaa-classical
sanskrit:       shloka-then-translation
duration:       60 min
slug:           gita-adhyay-2-sankhya-yog-katha-202605
```

### 2. Hat 1 — Scripture Researcher: build the chapter dossier

Read the canonical text. Acceptable primary sources (in order of
preference):

1. **Gita Press editions** (Geeta, Ramayan, Mahabharat) — the
   reference Hindi text for devotional channels. Cite by edition
   + page when possible.
2. **BORI Critical Edition of Mahabharat** (Sanskrit + scholarly
   apparatus). Translate into Hindi yourself or quote a
   well-known Hindi rendering (e.g. Pandit Ramnarayan Datt
   Shastri's Geeta Press translation).
3. **Valmiki Ramayan** (Sanskrit) + Tulsi Ramcharitmanas (Awadhi-
   Hindi devotional) — both acceptable for Ramayan kathaa.
4. **Shrimad Bhagavad Gita** (Gita Press / Swami Sivananda
   commentary) — for Gita adhyay.
5. **Puraan** — Bhagavat Mahapuraan (Gita Press) for Krishna
   leela; Shiv Mahapuraan / Vishnu Puraan for those traditions.

Wikipedia is acceptable as a secondary outline source — never the
only source.

For the chosen section, build a chapter dossier with these fields
(this becomes `chapters[]` in the output JSON):

```
- chapter_label:    "Adhyay 2 — Sankhya-Yog" / "Sundarkand Sarga 1"
- duration_target:  ~6-10 min per chapter (so 6-10 chapters fill 60 min)
- mool_shloka:      [{shloka_n: 11, devanagari: "...", anuvad: "..."}]
                    (only when sanskrit policy = shloka-then-translation;
                     else empty list)
- prose_summary:    3-5 sentences in plain Hindi — what happens / what is taught
- bhavarth:         the kathaa-vyaas's commentary — the point; what the
                    devotee should take away. 2-4 sentences in pure Hindi.
- characters:       proper nouns appearing this chapter (Krishna, Arjun,
                    Ram, Sita, Hanuman, etc.) — for pronunciation pre-pass
- locations:        Kurukshetra, Lanka, Vrindavan — same reason
- sources:          [{edition, page_or_shloka_range, url_if_online}]
                    minimum 2 distinct sources per chapter
- footage_queries:  3-5 queries the curator hat will use (§3)
- connective_tissue_out: bridge sentence to the next chapter (always
                        non-empty except on the final chapter)
```

**Refuse to fabricate.** If the user names a section you cannot
ground in canonical text, say so and ask them to pick a different
section. Devotional viewers fact-check.

### 3. Hat 2 — Footage Curator: source one+ visual per query

For each of ~25-50 footage queries (3-5 per chapter × 6-10
chapters), find a real visual. Prefer in this order:

1. **Wikimedia Commons** — public-domain temple / mural / iconography
   photographs and scanned manuscripts. Zero ContentID risk.
2. **archive.org** public-domain — old devotional reels, scanned
   Gita Press editions, public-domain Indian art. Use the existing
   helper at `historyrecapped/scripts/download_long_form_sources.py`.
3. **Pexels / Pixabay** — CC0 stock for temple ambience, river
   ghats, oil-lamp diyas, marigold garlands, sunrise over hills.
   Search via `sportstoriesanimated/scripts/find_b_roll.py` (the
   helper is channel-agnostic; reusing it is the right call —
   memory: heuristic #51).
4. **Met Museum / Indian Museum / British Museum open access** —
   CC0 high-resolution Indian miniature paintings, Pahari /
   Mughal / Rajput schools. Excellent for chapter title cards.
5. **Broadcaster / publisher official channels** — Gita Press
   / BAPS / ISKCON official YouTube uploads of devotional
   imagery. Fair-use under devotional-commentary doctrine; document
   the rationale in `_license_note` per clip.

**Hard rules** (mirror `historyrecapped/learnings/long_form_sources.md`):

- **NO YouTube re-uploaders** for any archival / public-domain
  source. ContentID will eat the upload. Go to the
  rights-holder's official channel or to archive.org.
- **NO AI image gen.** No Z-Image-Turbo, no diffusion. The whole
  premise of this skill is footage-only fast-production. If you
  cannot source a visual for a beat, reframe the beat to one you
  can source (a static manuscript scan with a slow Ken Burns is
  always available).
- **NO faces from the diffusion-quoted-phrase-leak class** —
  Z-Image-Turbo issues don't apply because we are not generating,
  but the principle survives in **caption / overlay text**: never
  quote descriptive phrases over devotional imagery (channel
  learning `diffusion_quoted_phrase_leak.md`).

For each shotlist window write:
`{in_s, out_s, match_text, kind, source_url, _license_note,
chapter, _design_notes}`. Hold each visual **long** (30-90s slow
Ken Burns) — jarring cuts wake a viewer who is using this for
sleep / meditation. Mirror
`historyrecapped/learnings/long_form_channel.md` continuous-window
rule.

`kind` enum: `temple_photo` | `manuscript_scan` | `painting_pan` |
`stock_ambience` | `archival_devotional` | `iconography_still`.

### 4. Hat 3 — Kathaa-Vyaas: write the continuous narration

Total target by duration:

- 50 min @ ~110 wpm Kokoro hf_alpha measured cadence → ~5,500 Hindi words
- 60 min → ~6,600 words
- 70 min → ~7,700 words

(Hindi WPM is lower than English; Kokoro hf_alpha at speed 1.0 with
the channel's `_HINDI_TATSAMA_RESPELLINGS` pacing lands around
105-115 wpm. Validate against the first chunk — if WPM drifts the
quality gate §6 catches it.)

**Always count words explicitly before kicking off render** — the
historyrecapped first 2-hour authoring attempt produced ~5,000
words and rendered ~50 min instead of ~120 min. Memory
`feedback_long_form_image_panels_capped.md` and
`long_form_channel.md` both document this. Don't trust intuition
on length scaling.

Strict structure:

```
<MANGALAACHARAN — opening invocation, 90-120s, ~180-220 words>
  - One-line invocation: "ॐ नमो भगवते वासुदेवाय" or section-appropriate
  - Kathaa-vyaas frame: "आज हम सुनेंगे <text> की <section> की कथा।
    जिसमें <one-line premise>।"
  - Set the listener's posture: "आप शांति से बैठिए, आँखें बंद कर
    सकते हैं, और इस कथा को अपने भीतर उतरने दीजिए।" (or equivalent
    for the chosen tone)
  - Early-ask tee-up at ~3-4 min: "इस यात्रा को आगे बढ़ाने से पहले,
    अगर आपको यह कथा प्रिय लगे, तो SUBSCRIBE करें — और यह घंटी
    दबा दीजिए।" (FIRST embedded ask, not the closer)

<CHAPTER 1, ~6-10 min, ~700-1000 words>
  - chapter_label spoken aloud as a soft title: "अब सुनिए, अध्याय एक…"
  - mool_shloka (if sanskrit-then-translation policy):
    Devanagari shloka read slow + clear, then "इसका भाव यह है कि…"
  - prose_summary woven into flowing kathaa register — never
    bulleted, never journalistic
  - bhavarth as the chapter's emotional climax — slow down, lower
    register, let the line breathe
  - connective_tissue_out: bridge to chapter 2 in one sentence
    ("और जब अर्जुन यह सुनता है, तो उसके मन में एक नया प्रश्न
    जागता है — वही प्रश्न जो हम अगले अध्याय में सुनेंगे।")

<CHAPTERS 2 … N> same pattern. N = 6-10.

<UPSANHAR — closing benediction, 90-120s, ~180-220 words>
  - the late-ask: "अगर आज की कथा से आपको शांति मिली हो, तो LIKE
    करें और SUBSCRIBE करें — हर हफ्ते एक नई कथा।" (SECOND
    embedded ask — paired with closer per
    historyrecapped/learnings/long_form_support_asks.md REVISED
    2026-05-04: TWO asks total, NOT five.)
  - blessing line: "जय श्री कृष्णा" / "जय श्री राम" /
    "हर हर महादेव" — pick by text:
      mahabharat / gita / bhagavat-puraan → "जय श्री कृष्णा"
      ramayan                              → "जय श्री राम"
      shiv-puraan                          → "हर हर महादेव"
      vishnu-puraan                        → "जय श्री हरि"
  - silence beat at the very end (1.5-2s, no narration) — the
    renderer's `closer_hold_s` handles this
```

**Kathaa-register rules** (the writer's craft layer):

- **Pure Hindi.** No English words mid-narration. "Subscribe" /
  "Like" / "Comment" appear ONLY in the two embedded asks, never
  in the kathaa body. Banned-phrase scan §6 enforces.
- **Kathaa-vyaas voice.** Second-person occasionally
  ("कल्पना कीजिए…"), but the dominant register is
  third-person omniscient pandit. Avoid first-person "मैं"
  except in dialogue voiced from a character (Krishna, Arjun,
  Ram).
- **Sentence-length variation.** Short punch lines ("और तब
  युद्ध रुक गया।") interleaved with long sensory sentences. A
  monotonous Hindi long-form bores within 8 minutes.
- **Phonetic-respell tatsama and proper nouns.** The
  channel's `pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS` table
  handles common conjuncts (`अभि-मन्यु` etc.); for chapter-specific
  proper nouns (e.g. `अश्वत्थामा` → `अश्व-त्थामा`,
  `द्रोणाचार्य` → `द्रोणा-चार्य`) add per-story overrides to
  `narrations/<slug>.json:pronunciation_dict`. Memory:
  `feedback_pronunciation_pretts.md`.
- **No verdict acronyms.** Not relevant here, but the
  cross-channel banned list (no AITA/YTA/NTA, no
  "smash subscribe", no "vote in comments") still applies.
- **Two embedded asks, exactly two.** One ~3-4 min in (end of
  Mangalaacharan), one in Upsanhar. Pre-render gate §6 counts
  CTAs and blocks emit on >2 or <2.
- **No "हाय दोस्तों" / "नमस्कार दोस्तों" YouTube-style opener.**
  The kathaa starts with the invocation, not with a host
  greeting. Channel's locked format is devotional, not vlogger.

### 5. Output: three JSON files

#### `hindutavaanimated/narrations/<slug>.json`

```json
{
  "slug": "gita-adhyay-2-sankhya-yog-katha-202605",
  "format": "katha-long-form",
  "text": "bhagavad-gita",
  "section": "adhyay-2-sankhya-yog",
  "tone": "kathaa-classical",
  "sanskrit_policy": "shloka-then-translation",
  "duration_target_s": 3600,
  "wpm_target": 110,
  "narration": "<full continuous Hindi text — Mangalaacharan through Upsanhar, danda-terminated, NO chapter markers in the prose, those go in chapter_timestamps>",
  "chapters": [
    {
      "chapter_n": 1,
      "chapter_label": "अध्याय 2 श्लोक 1-10 — अर्जुन का विषाद",
      "narration_anchor": "अब सुनिए, अध्याय दो की पहली कथा",
      "mool_shloka": [
        {"shloka_n": 11, "devanagari": "अशोच्यानन्वशोचस्त्वं प्रज्ञावादांश्च भाषसे...", "anuvad": "जिनके लिए शोक नहीं करना चाहिए..."}
      ],
      "prose_summary": "<3-5 sentences flowing Hindi>",
      "bhavarth": "<2-4 sentences — the takeaway>",
      "characters": ["कृष्ण", "अर्जुन"],
      "locations": ["कुरुक्षेत्र"],
      "sources": [
        {"edition": "Gita Press, Geeta", "page_or_shloka_range": "Adhyay 2 Shloka 1-10", "url": "..."},
        {"edition": "Sivananda commentary", "page_or_shloka_range": "Adhyay 2", "url": "..."}
      ],
      "connective_tissue_out": "<one sentence bridge to chapter 2>"
    },
    "<...one entry per chapter, 6-10 chapters total>",
    {"chapter_n": "<last>", "...": "...", "connective_tissue_out": null}
  ],
  "cta_inserts": [
    {"position": "in_mangalaacharan", "text": "इस यात्रा को आगे बढ़ाने से पहले, अगर आपको यह कथा प्रिय लगे, तो SUBSCRIBE करें — और यह घंटी दबा दीजिए।"},
    {"position": "in_upsanhar",       "text": "अगर आज की कथा से आपको शांति मिली हो, तो LIKE करें और SUBSCRIBE करें — हर हफ्ते एक नई कथा।"}
  ],
  "blessing_close": "जय श्री कृष्णा",
  "pronunciation_dict": {
    "द्रोणाचार्य": "द्रोणा-चार्य",
    "अश्वत्थामा": "अश्व-त्थामा"
  },
  "chapter_timestamps": [
    {"ts_s": 0, "label": "मंगलाचरण"},
    {"ts_s": 110, "label": "अध्याय 1 — <chapter_label>"},
    "..."
  ],
  "title_options": [
    "श्रीमद्भगवद्गीता अध्याय 2 — सांख्य योग की पूरी कथा | Gita Adhyay 2 Hindi Katha",
    "गीता अध्याय 2 की संपूर्ण कथा | Krishna-Arjun Samvad",
    "Bhagavad Gita Chapter 2 in Hindi — Sankhya Yog Full Katha"
  ],
  "description_template": "<long-form description with chapter_timestamps + sources block + om-namo invocation>",
  "tags": [
    "bhagavad gita in hindi",
    "gita adhyay 2",
    "sankhya yog",
    "krishna arjun samvad",
    "hindu scripture",
    "spiritual hindi",
    "meditation hindi",
    "kathaa",
    "devotional hindi"
  ],
  "source_urls": ["<canonical text source URL>"],
  "source": "manual:make-katha"
}
```

#### `hindutavaanimated/shotlist/<slug>.json`

Reuses the existing footage-only shotlist schema (matches
`historyrecapped/scripts/render_footage_only.py`):

```json
{
  "slug": "gita-adhyay-2-sankhya-yog-katha-202605",
  "windows": [
    {
      "in_s": 0.0, "out_s": 90.0,
      "match_text": "ॐ नमो भगवते वासुदेवाय",
      "kind": "iconography_still",
      "source_url": "https://commons.wikimedia.org/wiki/File:Krishna_Holding_Flute.jpg",
      "_license_note": "Wikimedia Commons — Public Domain",
      "chapter": "mangalaacharan",
      "_design_notes": "Slow 90s Ken Burns push-in on Krishna iconography. Audio-led."
    },
    {
      "in_s": 5.0, "out_s": 60.0,
      "match_text": "अब सुनिए, अध्याय दो",
      "kind": "manuscript_scan",
      "source_url": "https://archive.org/details/gita-press-geeta",
      "_license_note": "archive.org — Public Domain",
      "chapter": 1,
      "_design_notes": "Scanned Gita Press page, slow vertical pan over the Devanagari shloka."
    },
    "<...one or more windows per chapter, totaling ~25-50 windows>"
  ],
  "aspect": "16:9"
}
```

#### `hindutavaanimated/cast/<slug>.json`

Footage-only kathaa — no on-screen characters. Cast file is
trivial but kept for consistency with other channels:

```json
{
  "narrator": {
    "description": "kathaa-vyaas — calm Hindi pandit register",
    "default_emotion": "measured-reverent",
    "voice_preset": "kokoro_hf_alpha",
    "speed": 1.0
  },
  "supporting": []
}
```

### 6. Quality gates (run BEFORE renderer — mechanical)

Each gate blocks emit on hit. Run in order.

1. **Banned-phrase scan** — grep `narration` against
   `hindutavaanimated/learnings/` for verbotens AND against the
   factory-wide closer-anti-patterns ("smash subscribe",
   "vote in comments", verdict acronyms,
   "namaskaar dosto" YouTube-vlogger opener). Block on hit.
2. **Hindi-only assertion** — narration must contain ≤ 2% ASCII
   word characters (only allowed: SUBSCRIBE / LIKE / COMMENT
   inside `cta_inserts`, and proper-noun transliterations in
   `description_template`). >2% blocks emit. Use the
   `\w` UNICODE regex pattern per channel learning
   `devanagari_unicode_regex.md`.
3. **Pronunciation pre-pass** — every proper noun in
   `chapters[].characters` ∪ `locations` must either match an
   existing entry in `pipeline/audio.py:_HINDI_TATSAMA_RESPELLINGS`
   or appear in `narrations/<slug>.json:pronunciation_dict` with
   a hyphenated respelling. Memory:
   `hindi_tts_prosody.md` + `feedback_pronunciation_pretts.md`.
4. **Length budget** — Hindi word count must satisfy
   `duration_target_s × wpm_target / 60` within ±5%. Outside
   that, hard-fail. (Computes against measured Kokoro hf_alpha
   ~110 wpm — calibrate if a future channel learning lands a
   different number.)
5. **CTA count** — exactly **2** embedded asks
   (`cta_inserts.length == 2`, positions
   `in_mangalaacharan` + `in_upsanhar`). >2 or <2 blocks emit.
   Mirrors `historyrecapped/learnings/long_form_support_asks.md`
   REVISED 2026-05-04.
6. **Connective-tissue scan** — every chapter from #1 to #(N-1)
   must have non-null `connective_tissue_out`, AND that bridge
   sentence must literally appear at the end of that chapter's
   slice in the master `narration` string. Cold cuts block emit.
7. **Source-fidelity check** — every chapter has ≥2 distinct
   `sources` entries; ≥1 must be a primary edition (Gita Press
   / BORI / Sivananda / Tulsi / Valmiki). Block on miss.
8. **/critique-audio gate** — run BEFORE the ffmpeg / footage
   download stage. TTS bugs invalidate the whole 60-min render.
   Memory: `feedback_critique_audio_before_image_gen.md`. Run
   the gate on a single representative chapter chunk first
   (chapter 1's first 60s synth) to catch tatsama / numeral
   / danda regressions early.
9. **ContentID scan** — for every shotlist window, source must
   be archive.org / Wikimedia / Pexels / Pixabay / Met / a
   museum open-access endpoint / a broadcaster's OFFICIAL
   YouTube channel. YouTube re-uploader URLs block emit. Memory:
   `historyrecapped/learnings/long_form_sources.md`.
10. **Aspect + voice match** — assert
    `shotlist.aspect == "16:9"` and
    `narration.wpm_target ∈ [105, 115]` (Kokoro hf_alpha measured
    band) AND `cast.narrator.voice_preset == "kokoro_hf_alpha"`.
    Mismatch blocks emit.
11. **Render-style assertion** — assert
    `hindutavaanimated/config.yaml` has a `kathaa:` block with
    `aspect: 16:9` + `duration_band: [50, 70]` + a
    `caption_mode` (default `none` — long-form devotional
    listeners often have eyes closed, mirroring sleep-mode
    `historyrecapped/learnings/long_form_channel.md`). If the
    block is missing, instruct the user to add it (the stage-4
    handoff prints the YAML diff). Do NOT proceed.
12. **Sanskrit policy gate** — if `sanskrit_policy` ==
    `shloka-then-translation`, every `mool_shloka[].devanagari`
    must be followed in the master `narration` by an
    `इसका भाव यह है` / `अर्थात्` / `इसका अर्थ है` connector and
    a Hindi anuvad. Bare-Sanskrit-without-translation blocks
    emit (rejected pattern from channel learnings).

### 7. Renderer handoff

The hand-off is to `render_footage_only.py` because (a) it reads
the channel's `tts_provider` (Kokoro hf_alpha works), (b) it is
the lowest-memory long-form path (no diffusion queue), (c) the
historyrecapped `render_long_form.py` REJECTS any
`tts_provider != f5_tts` (memory:
`feedback_long_form_strict_f5.md`) and Kokoro is the only Hindi
voice in the repo.

```bash
caffeinate -i .venv/bin/python -u \
  historyrecapped/scripts/render_footage_only.py \
  --channel hindutavaanimated --slug <slug> --aspect 16:9
```

`caffeinate -i` + `python -u` per memory
`feedback_long_form_render_caffeinate.md` — sleep kills the GPU/
ffmpeg context silently and buffered stdout hides the crash
point.

The renderer will:

1. Synthesize TTS via Kokoro hf_alpha, chunked at danda /
   sentence boundaries → resumable per-chunk cache under
   `hindutavaanimated/cache/<slug>/tts_chunks/`.
2. Whisper word timestamps for the FULL narration (only relevant
   if `caption_mode != none`). Apply 1.5s word-clamp + chunked
   forced-Hindi if that learning is wired — see
   `whisper_chunked_hindi_forced.md` and
   `whisper_hindi_undercount.md`.
3. yt-dlp / direct-download every shotlist `source_url` →
   `hindutavaanimated/footage/sources/`.
4. ffmpeg trim each window with blurred-letterbox 16:9 →
   `scratch/clip_NN.mp4`.
5. ffmpeg concat → `scratch/video.mp4`.
6. ffmpeg mux narration + ambient bed at -28 dB (if
   `hindutavaanimated/music/<bed>.wav` exists) → no captions if
   `caption_mode == none`.
7. Final mux to `hindutavaanimated/long_form/<slug>.mp4`.

Render time: ~45-75 min on M2 Max for a 60-min kathaa
(majority spent in Kokoro chunked synth; ffmpeg footage trim is
fast because there is no per-beat image gen).

**Renderer extension required (handoff flag):** the existing
`render_footage_only.py` is hard-coded `[bg]scale=1080:1920`
(9:16 only). The `--aspect 16:9` flag is the same one-filter
swap that `/make-top10` already requested. **Single unified
patch to land BEFORE this skill's first run** — surfaced in the
handoff so the user can sequence it.

### 8. Self-learning hook

After the user runs `/critique-audio` on the chapter-1 chunk OR
`/critique-video` on the final 60-min mp4:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo in this kathaa, this chapter's source was
     wrong) → fix in `narrations/<slug>.json`, append a 1-line
     note to `.claude/skills/make-katha/learnings/_index.md`.
   - **CLASS-OF-BUG** (every kathaa will hit this) → fix in
     `pipeline/audio.py` / `pipeline/captions.py` / this
     `SKILL.md` prompt, then append a regression note to
     `.claude/skills/make-katha/learnings/<topic>.md` AND mirror
     to `hindutavaanimated/learnings/<topic>.md` per CLAUDE.md
     dual-save.
2. Update `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`
   index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a
   pre-render quality gate in §6 that blocks emit on detection.

Specific watch-list for the FIRST run (these are the most
likely first-bug surfaces, derived from channel learnings):

- Kokoro tatsama collapse on a proper noun not in the respelling
  table → add to `_HINDI_TATSAMA_RESPELLINGS` AND to this skill's
  `learnings/proper_noun_respellings.md`.
- Sanskrit shloka mispronounced (Kokoro reading Sanskrit as
  Hindi mangles diphthongs) → revisit `sanskrit_policy` default
  or add a separate Sanskrit pre-pass.
- Whisper undercount on long-form Hindi (memory
  `whisper_hindi_undercount.md` documents 50% loss on dense
  Hindi) → if captions are enabled, force chunked-Hindi Whisper
  (`whisper_chunked_hindi_forced.md`).
- 16:9 aspect handoff still pending in `render_footage_only.py`
  → block until the patch lands; do NOT silently render at 9:16.

### 9. Report back

```
✓ wrote narration: hindutavaanimated/narrations/<slug>.json
✓ wrote shotlist:  hindutavaanimated/shotlist/<slug>.json
✓ wrote cast:      hindutavaanimated/cast/<slug>.json
  text:           <text> / <section>
  tone:           <tone>
  sanskrit:       <policy>
  chapters (N=<N>):
    1.  <chapter_label>     ~<min>:<sec>
    2.  <chapter_label>     ~<min>:<sec>
    ...
    N.  <chapter_label>     ~<min>:<sec>
  duration target: ~<MM>:00 (computed: <words> Hindi words ÷ <wpm> wpm = <m:ss>)
  CTAs: 2 (in_mangalaacharan / in_upsanhar) ✓
  blessing close: <blessing_close>
  footage windows: <N> sourced (<wikimedia>/<archive>/<stock>/<museum>/<broadcaster>)
  ContentID risk: <low / fair-use justified / flagged>
next:
  prerequisites — confirm both before render:
    [ ] hindutavaanimated/config.yaml has `kathaa:` block (handoff prints YAML diff if missing)
    [ ] historyrecapped/scripts/render_footage_only.py supports --aspect 16:9
  then:
    caffeinate -i .venv/bin/python -u \
      historyrecapped/scripts/render_footage_only.py \
      --channel hindutavaanimated --slug <slug> --aspect 16:9
```

If any quality gate fails, list the failures and STOP. Do not
silently ship a 60-min kathaa with English-mid-narration leaks,
1/3+ CTAs, ungrounded shloka claims, or YouTube-reuploader
footage.

## Important rules

- **Pure Hindi narration, no English mid-kathaa.** SUBSCRIBE /
  LIKE only inside the two embedded asks. Banned-phrase scan §6
  enforces.
- **Two embedded asks, exactly two.** One in Mangalaacharan
  (~3-4 min), one in Upsanhar (closer). Mirrors
  `historyrecapped/learnings/long_form_support_asks.md` REVISED
  2026-05-04.
- **Closer is the BLESSING line, not a CTA.** "जय श्री कृष्णा" /
  "जय श्री राम" / "हर हर महादेव" / "जय श्री हरि" by text. The
  CTA is *embedded inside* the Upsanhar; the final line is
  always the blessing.
- **Footage-only.** No AI image gen, no diffusion model loaded.
  If a beat can't be sourced, reframe the beat — never fall back
  to a generated still. (Channel's
  `diffusion_quoted_phrase_leak.md` plus the heuristic-#51
  efficiency win — diffusion is the slow path; we don't need it
  here.)
- **Connective tissue between every chapter.** No cold cuts.
  Quality gate §6 enforces.
- **Every chapter ≥ 2 primary sources.** Wikipedia counts as
  one but never the only one. ≥1 must be a primary edition
  (Gita Press / BORI / Sivananda / Tulsi / Valmiki).
- **No YouTube re-uploaders for archival** — archive.org PD,
  Wikimedia, Pexels, Pixabay, museum open-access, or a
  broadcaster's official channel only.
- **Phonetic-respell every proper noun and every tatsama.**
  Use the channel's `_HINDI_TATSAMA_RESPELLINGS` first; add per-
  story overrides via `pronunciation_dict`.
- **Channel must have a `kathaa:` block** in
  `hindutavaanimated/config.yaml`. Skill stage-4 handoff prints
  the YAML diff if missing. Refuses to ship until added.
- **Render via `render_footage_only.py`, not
  `render_long_form.py`** — the latter is F5-TTS-MLX only and
  refuses Kokoro.
- **Always use `.venv/bin/python`** for any helper commands the
  user runs.
- **Never run TTS / footage download / ffmpeg in this skill.**
  That is the renderer's job. This skill produces JSON only.
- **Never call the Anthropic SDK directly.** LLM calls go
  through `pipeline/llm.py` (`claude -p`) per memory
  `feedback_llm_via_claude_cli.md`.
- **Never bypass the website-first workflow** for production
  renders — `/make-katha` produces JSON; the website triggers
  render.

## Learnings from prior runs

<!-- empty on day 1; appended on every regression via §8 -->

## Why this skill is separate from /make-top10 and /make-sports-doc

`/make-top10` is a 28-32 min countdown LIST format with `ranks[]`
and connective-tissue between unrelated cases. Kathaa is **not a
list** — it is a continuous scriptural narrative with `chapters[]`
that share characters, locations, and a single moral arc.
Stretching `/make-top10` to 60 minutes AND replacing the rank
contract with a chapter contract would invalidate every
curatorial gate the skill ships with — at which point you have a
different skill, not a variant.

`/make-sports-doc` is the closest neighbour structurally — both
are single-subject long-form with chapters. But the **voice,
language, source policy, and curatorial register are
incompatible**: sports-doc rides on broadcast-clip alignment, an
investigative-biographer writer hat, Sarah English, and 16:9
documentary visuals. Kathaa rides on Kokoro hf_alpha Hindi,
kathaa-vyaas register, devotional / iconographic visuals, and a
sanskrit-policy switch the sports format has no concept of.
Forking the schema costs less than a generic-long-form skill
that branches on channel.

This skill keeps the **footage-only renderer** and the
**historyrecapped 2-ask CTA cadence** as a shared substrate,
and adds the kathaa-vyaas authoring contract as a thin layer on
top.

learnings_consulted:
  - hindutavaanimated/learnings/channel.md (Hindi-only, no Sanskrit-only, locked closer, Kokoro hf_alpha)
  - hindutavaanimated/learnings/hindi_tts_prosody.md (tatsama hyphen-respellings, danda terminator)
  - hindutavaanimated/learnings/devanagari_unicode_regex.md (\w UNICODE for any narration regex)
  - hindutavaanimated/learnings/whisper_chunked_hindi_forced.md (caption Whisper path)
  - hindutavaanimated/learnings/whisper_hindi_undercount.md (clamp + script-proportional fallback)
  - historyrecapped/learnings/long_form_channel.md (footage-only renderer pattern)
  - historyrecapped/learnings/long_form_support_asks.md (TWO asks per video, 2026-05-04 revised)
  - historyrecapped/learnings/long_form_sources.md (no YouTube re-uploaders, archive.org PD)
  - historyrecapped/learnings/long_form_render_caffeinate.md (caffeinate -i + python -u)
  - feedback_long_form_strict_f5.md (render_long_form.py rejects non-F5 — must use render_footage_only.py)
  - feedback_pronunciation_pretts.md (phonetic respelling pre-TTS)
  - feedback_critique_audio_before_image_gen.md (audio gate first)
  - feedback_engineer_class_of_bug.md (one-off vs class-of-bug classification)
  - feedback_dual_save_memory_and_docs.md (project doc + memory entry)
  - feedback_closer_caption_style.md (closer woven into last beat, not separate panel)

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
