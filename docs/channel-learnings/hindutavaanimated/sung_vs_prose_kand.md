---
name: HindutavaAnimated — sung-tradition vs prose-tradition kand selection
description: Class-of-bug discovered 2026-05-05 — /make-katha picked Sundarkand for a prose kathaa-vyaas, but Sundarkand is a SUNG text in the devotional tradition (Tulsi doha+chaupai meter, sung by Hari Om Sharan / Anuradha Paudwal / weekly sangha-paath). The audience searching "Sundarkand" expects sung recitation; prose narration is a different niche. Future kathaa selections must classify SUNG vs PROSE before authoring.
type: project
---

**Discovered 2026-05-05** — first `/make-katha` run picked
`ramayan-sundarkand-sarga-1-3` for a 70-min prose narration (Kokoro
hf_alpha hindi female reading kathaa-vyaas commentary). Render was
killed at ~30% TTS progress when the user pointed out the format
mismatch.

## The category

Hindu scripture has TWO distinct devotional-media traditions on
YouTube and in the home/temple. They look similar from outside but
are fundamentally different products:

### SUNG (paath / gaayan / kirtan)

The text is rendered in its native poetic meter, with melody, often
with light tabla/harmonium. The audience knows the words and chants
along.

- **Sundarkand** — Tulsi doha + chaupai, sung weekly on Tuesdays
  and Saturdays. YouTube examples: Hari Om Sharan (100M+ views),
  Anuradha Paudwal, Mukesh, Lallan Singh.
- **Hanuman Chalisa** — sung; 40 chaupais.
- **Aartis** — Om Jai Jagdish, Ganesh aarti, etc. — always sung.
- **Bhajans** — devotional songs, by definition sung.
- **Gita-paath in Sanskrit-only** — the verses are chanted in their
  Sanskrit meter, not narrated.
- **Devi Bhagavat / Sundar-paath sung** — same.
- **Ramcharitmanas full-paath / Akhand-paath** — multi-day sung
  community readings.

These need a **sung-text generator** (Suno, Udio, or pre-recorded
voiceover). Kokoro hf_alpha cannot sing — it produces spoken Hindi.
Authoring a "sung Sundarkand" with Kokoro produces a flat narration
of poetic verses, which sounds wrong to anyone in the tradition.

### PROSE (kathaa-vyaas pravachan)

The kathaa-vyaas tells the *story* of the scripture in his/her own
explanatory prose, weaving in shloka quotations + bhavarth + moral
interpretation. The audience listens to learn, not chant along.

- **Mahabharat parvas** — Bhishma-parva, Karna-parva, Stree-parva,
  Shanti-parva. Strong kathaa-vyaas tradition.
- **Ramayan Bal Kand prose-storytelling** — Ram-janma, Vishwamitra
  ki yagya, Ram-Sita vivah. Pravachan-friendly.
- **Ramayan Ayodhya Kand prose** — Kaikeyi-Manthara, Ram-vanvaas
  rationale. Pravachan-friendly.
- **Ramayan Aranya Kand prose** — Sita-haran story.
- **Ramayan Kishkindha Kand prose** — Sugriv-Bali.
- **Bhagavat Puraan Krishna-leela** — Vrindavan stories,
  Putana-vadh, Govardhan, Kaaliya-mardan. Strong pravachan
  tradition (Bhagvat-saptaah).
- **Shiv Mahapuraan stories** — Markandeya, Daksh-yajna,
  Shiv-Parvati vivah.
- **Vishnu Puraan / Devi Puraan stories.**
- **Bhagavad Gita pravachan** — explanatory commentary on a chapter
  (NOT the verses themselves chanted — the explanation around them).
  Ranbankura Maharaj / Govind Dev Giri Maharaj style.

These map cleanly to Kokoro hf_alpha narration over footage-only
visuals — the existing kathaa stack.

## How to apply

Stage 1 of `/make-katha` (text + section confirmation) MUST classify
the user's choice as SUNG or PROSE before proceeding:

| Choice                                          | Tradition | OK with Kokoro?         |
|-------------------------------------------------|-----------|-------------------------|
| Tulsi Sundarkand (any sarga)                    | SUNG      | NO — needs Suno         |
| Hanuman Chalisa                                 | SUNG      | NO — needs Suno         |
| Aartis / bhajans                                | SUNG      | NO — needs Suno         |
| Sanskrit-only shloka chanting                   | SUNG      | NO — needs Suno         |
| Mahabharat parva (any)                          | PROSE     | YES                     |
| Ramayan Bal/Ayodhya/Aranya/Kishkindha (prose)   | PROSE     | YES                     |
| Bhagavat Puraan Krishna-leela / Bhagvat-saptaah | PROSE     | YES                     |
| Shiv Mahapuraan stories                         | PROSE     | YES                     |
| Gita pravachan (explanatory, not chanted)       | PROSE     | YES                     |

If the user requests a SUNG text, the skill should either:

1. **Refuse + redirect** — explain the audience mismatch and
   suggest a prose-friendly alternative.
2. **Pivot the framing** — e.g. "Sundarkand kathaa-vyaas pravachan"
   (prose retelling of the events) is a real but smaller niche; if
   the user explicitly wants this, proceed but warn that the SEO /
   tags must NOT use the canonical Sundarkand title format
   (otherwise the video lands in front of the sung-paath audience
   and they bounce).
3. **(Future)** Wire a sung path — Suno-generated doha+chaupai
   tracks with Hindi voiceover translation interludes between
   verses. Substantial new work; not in current pipeline.

## Skill change

`/make-katha` SKILL.md needs an explicit SUNG vs PROSE classifier
in stage 1, blocking emit if the user picks SUNG without explicit
acknowledgement of the format mismatch. See:

`.claude/skills/make-katha/learnings/sung_vs_prose_traditions.md`

## Sundarkand-specific note

If the user *does* want Sundarkand for HindutavaAnimated, the right
format is:

- **Sung Tulsi doha+chaupai** via Suno prompts (similar pattern to
  `/make-rhyme` — generate the sung stems, then assemble)
- **Or** prose Sundarkand-pravachan with explicit re-titling
  (e.g. "Sundarkand ki kathaa — हनुमान की लंका यात्रा" — pravachan
  explainer, NOT "Sundarkand Path" / "Sundarkand Recital")

The narration produced 2026-05-05 (`ramayan-sundarkand-sarga-1-3-katha-202605.json`)
is **abandoned**. The render artefacts under
`hindutavaanimated/cache/ramayan-sundarkand-sarga-1-3-katha-202605/` may
be removed.
