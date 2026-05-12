# Long-form character consistency (CLASS-OF-BUG, 2026-05-12)

> **TL;DR** — Long-form has no cast stage; the rewriter authors panel
> `scene` prompts independently with no character re-use directive.
> Result: a single protagonist gets rendered as a young child in
> some panels and an elderly man in others because each panel's
> scene says "a person…" with whatever the LLM imagined for THAT
> panel. Strengthen the `rewrite_long_form` prompt to require ONE
> character spec defined upfront, repeated verbatim in every panel
> that features the protagonist; defer to the channel YAML's
> `character_description` ("Recurring character: …") when present.

## What surfaced this

Render job [8413e79d…](https://console.cloud.google.com/run/jobs/details/asia-southeast1/ytfactory-render-worker-v2/executions/ytfactory-render-worker-v2-9j8nr)
— a 30-min mystoriesanimated long-form. Across 24 panels of the same
story-protagonist, the rendered character age varied randomly: child
in panel 2, middle-aged in panel 7, elderly in panel 14. User flagged
"images don't have consistency of character, also you are not fixing
the age? randomly child / randomly old".

## Root cause

`pipeline/render/long_form.py` timeline shows `cast: skipped — long-form has no cast stage`.

The Shorts pipeline has a dedicated `cast` LLM stage that produces a
per-story `character_description` JSON, prepended to every per-beat
image prompt to lock the character spec across all renders of one
story. **Long-form has no equivalent.** `pipeline/llm/rewrite_long_form.py`
authors the `scene` strings inside the same LLM call that writes the
narration — and the prompt didn't tell the LLM to lock the character
spec.

The channel YAML has a fallback `character_description` (e.g.
mystoriesanimated's "round-headed character with shoulder-length
brown hair, simple dot eyes, …"). The rewriter's
`_channel_context()` mentions it as flavour text ("Recurring
character: …"). But the prompt's panel-authoring instructions
treated each panel scene as independent prose:

> Panels: each panel scene must be CONCRETE and STAGED — name the
> subject, what they're doing, what's in their hands, the
> environment, the lighting.

No instruction to RE-USE the character spec across panels. The LLM
filled in "name the subject" with whatever fit each scene's emotional
beat — a young child for a memory beat, an adult for a confrontation,
an elder for a reflective close. The image generator (FLUX.2 klein)
has no inter-panel memory; each panel renders the literal spec it
was given.

## Fix (shipped commit `bfbbec1`, 2026-05-12)

`pipeline/llm/rewrite_long_form.py::_PROMPT_TEMPLATE` — added a
NON-NEGOTIABLE craft rule:

> **CHARACTER CONSISTENCY** (this is the #1 long-form quality bug —
> fix it in your output, not in render):
>
>  * Decide ONE character spec for the protagonist (and named
>    supporting cast) BEFORE writing any panel. A character spec is
>    a 1-2 sentence physical description: age range (e.g. "30s"),
>    gender presentation, hair (length + colour), skin tone, build,
>    clothing palette, and any signature prop. Channel context above
>    may already define a "Recurring character" — when present, USE
>    THAT EXACTLY as the protagonist spec; do not invent a new one.
>
>  * In every panel scene that features that character, repeat the
>    WHOLE character spec verbatim at the start of the scene
>    description. Yes, repeat the same words across all 24 panels.
>    The image generator has no memory between panels — if you
>    describe the protagonist as "a young child" in panel 2 and
>    "an elderly man" in panel 5, you WILL get a young child and
>    an elderly man, not the same person at two ages. This is
>    non-negotiable.
>
>  * If the story spans years, keep the character spec the same
>    anyway — viewers tolerate a flat-art protagonist who looks the
>    same age throughout much better than a protagonist who morphs
>    between random ages every panel.
>
>  * Landscape / inanimate panels (no people) don't need the
>    character spec.

## Why this works without a cast stage

Two reasons:

1. The channel YAML's `character_description` is already the seed for
   "what should our protagonist look like across this channel". The
   prompt now instructs the LLM to USE THAT EXACTLY rather than
   invent. Channel-level consistency comes free.

2. Panel-to-panel consistency within ONE long-form is enforced
   client-side in the LLM: it writes one canonical character spec at
   the top of its sectioned reasoning, then literally copy-pastes
   that spec into every panel scene. Human review of the panel
   strings would show the same opening phrase repeated 18-24 times
   per script — that's the point.

A future cast stage for long-form (mirroring Shorts) would be cleaner
architecturally — the spec would be in `<channel>/cast/<slug>.json`
instead of duplicated 24× in the script. But the prompt-side fix
shipped today is a one-edit zero-rollout-cost change; the cast stage
can be incremental work later if drift returns.

## Channels affected

Every long-form-producing channel:

| channel             | long-form variants                            |
|---------------------|------------------------------------------------|
| mystoriesanimated   | `unresolved_mysteries`, `aita_long_form`       |
| historyrecapped     | `sleep_history`                                |
| sportsrecapped      | `football_explainer`, `sports_doc`             |
| hindutavaanimated   | `katha`, `hindutava_long_form`                 |
| cosmosdecoded       | `how_we_knew` long-form                        |

ALL of them currently route through
`pipeline/llm/rewrite_long_form.py::rewrite_long_form` and read the
same `_PROMPT_TEMPLATE`. The fix lands for every channel
simultaneously — no per-channel patches needed.

## Verification

Pin: `tests/test_rewrite_long_form_prompt.py::CharacterConsistencyPromptTest`
asserts the prompt contains:

- `"CHARACTER CONSISTENCY"`
- `"repeat the WHOLE character spec verbatim"`
- `"USE THAT EXACTLY"` (deferral to channel YAML)
- `"Landscape / inanimate panels (no people)"` (landscape carve-out)

End-to-end verification: open a fresh long-form script JSON and grep
for the protagonist spec — it should appear identically across every
panel that features the protagonist:

```bash
.venv/bin/python -c "
import json
with open('script.json') as f: d = json.load(f)
panels = d['panels']
person_panels = [p for p in panels if 'person' in p['scene'].lower() or 'man' in p['scene'].lower() or 'woman' in p['scene'].lower() or 'child' in p['scene'].lower()]
specs = [p['scene'][:120] for p in person_panels]
# Eyeball: are they all leading with the same character description?
for s in specs: print(s)
"
```

If panels with people open with DIFFERENT character descriptions,
the rule didn't take — file a regression.

## Cross-references

- Prompt source: `pipeline/llm/rewrite_long_form.py::_PROMPT_TEMPLATE`
- Channel context surfacing: `pipeline/llm/rewrite_long_form.py::_channel_context`
- Sister rule (Shorts): `mystoriesanimated/learnings/per_story_character.md`
- Sister rule (Sports): `sportsrecapped/learnings/character_consistency.md`
- Memory: `feedback_long_form_character_consistency.md`
- Render that surfaced this: job `8413e79dfbc244c3b27a46271d95f46d`
