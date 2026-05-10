# Skill scope-confirmation: prefilled `AskUserQuestion` options only

**Established 2026-05-07.** Every ytFactory authoring skill (`/make-*`)
MUST present its Stage-0 scope-confirmation step as
`AskUserQuestion` menus with 2-4 prefilled, intelligent options.
Free-text "type your subject" / "type the thesis" / "how long?"
asks are banned.

> Quote from the user: "I just want options to select from, no input."

---

## The rule, in one paragraph

When a `/make-<skill>` is invoked and the user has not pre-specified
the scope in their initial prompt, the skill MUST surface its
scope-confirmation questions through `AskUserQuestion` with prefilled
options that are intelligent defaults grounded in:

1. The channel's recent / shipped slugs (`<channel>/narrations/*.json`).
2. Canonical / popular subjects in the niche the user hasn't covered.
3. Trending or topical subjects from MCP helpers where fast.

The user picks; "Other" is always available as the escape hatch for
the rare custom case.

---

## What this looks like in practice

### Bad (banned — free-text ask)

```
Before I start researching, I need three things:

1. Subject — who/what exactly? (e.g., "Argentina 2022", "Mbappé career")
2. Spice — what's the contested angle?
3. Length — 20, 25, or 30 minutes?
```

### Good (mandated — prefilled options)

```
[AskUserQuestion: 2 questions]

Q1 — header: "Subject" — "Which sports doc subject?"
options:
  - "Mbappé 2022 World Cup Final" — 25 min, hat-trick + thesis "won the
    final alone and still lost"
  - "Liverpool Istanbul 2005" — 30 min, contested CL final comeback
  - "Maradona 1986 World Cup" — 25 min, "Hand of God + greatest tournament"
  - "Zidane headbutt 2006 final" — 20 min, "career-end controversy"

Q2 — header: "Length" — "Target run-time?"
options:
  - "20 min" — tight, single-arc
  - "25 min" — channel default
  - "30 min" — full 4-act

[Then a follow-up AskUserQuestion once subject is locked, with 2-3
thesis options tailored to that subject.]
```

---

## When this rule applies

- **All authoring skills:** `/make-sports-doc`, `/make-ranking`,
  `/make-history-short`, `/make-cosmos-short`, `/make-cosmos-long`,
  `/make-hindutava-short`, `/make-katha`, `/make-rhyme`,
  `/make-rivalry-recap`, `/make-sleep-history`, `/make-top10`,
  `/make-mystories-short`.

- **Every parameter that has a closed enum** in the skill's parameter
  table → `AskUserQuestion`. Examples:
  - cold-open kind (commentator clip / dramatic moment / shocking
    stat / provocative question / youtuber take / cold silence)
  - narrator tone (academic / podcast / playful / serious)
  - arc (rise-fall-redemption / parallel-lives / chronological /
    theme-driven / mystery-reveal / oral-history)
  - duration (Shorts: 50/60s; long-form: 20/25/30 min; sleep:
    60/90/120 min; kathaa: 50/60/70 min)
  - thumbnail mood / palette
  - music bed mood

- **Subject and thesis are NOT enums** — but the skill must propose
  3-4 intelligent candidates per channel. Never start with a blank
  text box.

## When this rule does NOT apply

- The user pre-specified the scope in their initial prompt
  (e.g., "make me a doc on Mbappé 2022, 25 min, focus on the
  hat-trick that should have won it"). Skip the question.

- Critique skills (`/critique-audio`, `/critique-video`) and
  one-off utility skills (`/update-config`, `/loop`, etc.) — they
  take a file path or a command, not a scope.

- Multi-stage menus where > 4 options are needed (e.g.,
  `/make-mystories-short` has 9 variants) — use a 2-stage menu:
  first stage narrows source (AITA / TIFU / Wiki / TIH), second
  stage narrows variant. NEVER collapse to a free-text ask.

## Order of asks

**Subject → Length → Thesis** (so thesis options can be tailored to
the chosen subject + length).

- Run subject + length in a single multi-question `AskUserQuestion`
  call.
- Then a follow-up `AskUserQuestion` for the 2-3 thesis options once
  the subject is locked.
- Then proceed into Stage-1 research.

## How to surface "intelligent defaults"

For each authoring skill, populate the subject options from:

| Source | When | How |
|---|---|---|
| `<channel>/narrations/*.json` slugs | always | List recent slugs, propose 1 "next obvious" subject from the same era/niche |
| Canonical not-yet-covered subjects | always | Maintain a per-channel "subject palette" in `<channel>/learnings/subject_palette.md` |
| `mcp__vidlens__discoverNicheTrends` | optional, when fast | Trending in the niche this week |
| Channel exemplars from competitors | optional | E.g., Sleepy Time History recent uploads for `/make-sleep-history` |

For thesis options, after subject is locked, use the channel's tone
register to phrase 2-3 contested-take options. The "obvious" thesis
is option 1; the "contrarian" thesis is option 2; the "process-led"
thesis (how something was made / discovered) is option 3.

## How skills enforce this

Each `/make-*` skill's `SKILL.md` MUST include this banner near the
top of the Stage-0 / "How to run it" section:

```markdown
> **SCOPING RULE (2026-05-07):** Every Stage-0 / scope-confirmation
> question MUST use `AskUserQuestion` with 2-4 prefilled options
> grounded in channel exemplars. Free-text scope asks are banned.
> Source of truth: [`docs/skill_prefilled_options.md`](../../docs/skill_prefilled_options.md).
```

## Question budget per invocation

Established 2026-05-07 during a sports-doc run. After 4
prefilled-option prompts (Subject + Length + Thesis + Arc/Cold-open/
Tone batched), the user pushed back on a 5th about chapter-spine
framing: "you may decide all this — should question you are asking."

**Rule of thumb:** total prefilled-option prompts per `/make-*`
invocation ≤ 4 single-question batches (or ≤ 2 multi-question
batches). Beyond that, the user is being asked to do the model's
job.

**Always ask (scope-level):**
- Subject + Length + Thesis (Stage-0)
- Arc + Cold-open + Narrator-tone (Stage-1, closed enums)
- Variant / sub-variant for multi-variant skills

**Never ask (authoring-level — model decides):**
- Chapter spine framings / chapter titles
- Per-chapter beats
- Footage in_s/out_s windows
- Talking-head selection
- Music bed mood per section
- Engagement-ask placement / line wording
- Pronunciation respellings
- Thumbnail concept (propose ONE in JSON; user swaps post-hoc)

**Exception:** load-bearing creative calls that would force a full
re-render to undo (16:9 vs 9:16 aspect, single-character vs ensemble)
— surface that ONE call. Otherwise: decide and ship.

## Linked from

Every channel `learnings/channel.md` should add a one-line pointer
to this doc so it surfaces in any channel's context.
