# Cast `appearance_lock` propagation — every animated long-form

Cross-channel rule established 2026-05-08 from the
krishna-govardhan-leela-long iteration. The fix that resolved a
serious devotional offense (Krishna rendered as female on ~20 of
65 panels of an animated Hindu mythology long-form).

## The bug

For long-form animated renders with 60+ panels, FLUX.2 klein
(and other diffusion models) interprets character names loosely
panel-by-panel without an explicit appearance description in
each prompt. Mythology characters named "Krishna" / "Indra" /
"Hanuman" have multiple competing priors in the diffusion training
data (different ages, genders, eras, art styles).

Without an explicit per-panel `appearance_lock`, the model picks
inconsistently across panels — Krishna appearing as a child in one
panel and an adult in the next, or as MALE in one and FEMALE in
another. On krishna-govardhan-leela-long this surfaced as a
serious devotional offense the user flagged immediately:

> "you are not sticking to similar drawing to god, and sometime
> showing in offensive way like kanha is male but you are showing
> as female at a lot of places, this is serious offence"

The fix: `pipeline/render/long_form.py:_flatten_chapters_to_panels`
auto-detects mentioned characters via `_detect_mentioned_chars` +
`_CAST_NAME_ALIASES_CANNED` and prepends each character's locked
description to every panel prompt that mentions them.

## How it works

1. **Cast file load** (in long_form main):

   ```python
   cast_path = paths.channel_root / "cast" / f"{args.slug}.json"
   if cast_path.exists():
       cast_doc = json.loads(cast_path.read_text())
   ```

2. **Pass cast_doc to flatten**:

   ```python
   panels = _flatten_chapters_to_panels(
       chapters_doc=chapters_doc, ..., cast_doc=cast_doc,
   )
   ```

3. **Per-panel character detection** (in flatten):

   For each panel's `notes` field (the visual prompt), run
   `_detect_mentioned_chars(scene, char_locks)` — a case-
   insensitive substring match against each character's aliases
   list (`_CAST_NAME_ALIASES_CANNED` maps canonical names to
   alternate forms).

4. **Prepend locked descriptions**:

   ```python
   if mentioned:
       lock_strs = [
           f"[{name.upper()} APPEARANCE LOCK] {char_locks[name]['description']}"
           for name in mentioned
       ]
       scene = " ".join(lock_strs) + " " + scene
   ```

   Order matters — locks come FIRST so identity anchors before
   the diffusion model parses scene composition.

5. **Single-character seed override**:

   When exactly ONE character is mentioned in a panel, override
   `seed_offset = char_lock.seed % 1000` so single-character
   panels stay visually consistent across the whole video.

## Mythology aliases shipped (2026-05-08)

`_CAST_NAME_ALIASES_CANNED` maps canonical character names to
alternate forms used in different epics / registers:

| Canonical | Aliases |
|---|---|
| krishna | krishna, kanha, kanhaiya, krsna, child krishna |
| indra | indra, deva-king, devraj |
| nanda | nanda, nanda maharaj, nandbaba |
| yashoda | yashoda, maa yashoda |
| balrama | balrama, balaram |
| radha | radha |
| arjun | arjun, arjuna |
| karna | karna |
| abhimanyu | abhimanyu |
| draupadi | draupadi |
| bhishma | bhishma, bhishma pitamah |
| drona | drona, dronacharya |
| hanuman | hanuman, anjaneya, bajrangbali |
| ram | ram, rama, shri ram |
| sita | sita |
| lakshman | lakshman, laxman |
| ravan | ravan, ravana |
| shiv | shiv, shiva, mahadev |
| ganesh | ganesh, ganesha |

Add new mythology aliases here (one place), not per-skill.

## Cast file schema (per-panel description format)

`<channel>/cast/<slug>.json`:

```json
{
  "krishna": {
    "description": "<2-4 sentence visual lock — bold black ink outlines, signature colors, age, gender, attire, signature props (peacock feather, blue skin, yellow dhoti)>",
    "palette": ["deep cobalt blue (skin)", "marigold yellow (dhoti)", ...],
    "default_emotion": "serene-knowing-smile",
    "seed": 108
  },
  "indra": { ... },
  ...
}
```

## When this applies

Every long-form animated render across every channel that uses
`pipeline/render/long_form.py:_flatten_chapters_to_panels`. Required
for any mythology / character-driven content where the same named
character appears in 5+ panels.

For Shorts (`pipeline/render/shorts.py`) the existing
`character_description` channel-config + per-slug cast file path
already handles this — but for long-form (60+ panels) the explicit
prepend-on-every-panel is mandatory.

## Memory

[`memory/feedback_cast_appearance_lock.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_cast_appearance_lock.md)
