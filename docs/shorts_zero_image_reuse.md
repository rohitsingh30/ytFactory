# Zero image reuse rule — every window in a Short gets a unique source

**Established 2026-05-08** during the cosmosdecoded 5-Shorts iteration.
After v3 had 26-28 windows but ~7-9 visual repeats per Short, user
feedback: "you are reusing images, plan how many images and when to
use which to cover the reel without repeating".

## The rule

**Every window in a single Short must reference a UNIQUE
`source_url`.** Zero image reuse within one Short.

A Short with 20 windows needs 20 distinct images.

A Short with 26 windows needs 26 distinct images.

## Why

- **Reuse signals low-effort.** Viewers (and the YouTube algorithm)
  see an image flash by twice and immediately downgrade their attention.
- **Each shot is a chance to add information.** Reusing means a beat
  that could have shown a *different* relevant photo got nothing new.
- **Shorts that scroll in 9-second-attention-span apps need maximum
  visual variety per second.** Repetition is a tax on retention.

## What counts as "the same image"

- **Same `source_url` →** definitely reuse. Reject.
- **Same Wikimedia file at different ken-burns crops →** still reuse;
  viewer recognises the underlying image. Reject.
- **Two AI-genned variants of the same subject (e.g. galaxy_recede_1
  vs galaxy_recede_2) →** acceptable IF the visual content is
  distinctly different (different angle, different stage of
  motion, different subject within the same theme).

## Authoring discipline

When the model authors a shotlist, the author script must:

1. Enumerate windows and check `len(set(source_urls)) == len(windows)`
2. If duplicates exist, generate or fetch additional unique sources
   (more AI images, more Wikimedia variants) until uniqueness holds.
3. Reject the shotlist as draft if duplicates remain.

The validator in `cosmosdecoded/scripts/bulk_author_100.py` enforces
this; deviation should be raised as a blocker.

## How to source enough variety

For a 20-window Cosmos Decoded Short:

- **5-7 verified Wikimedia photos** (scientist portraits, instrument
  photos, observatory exteriors, NASA imagery)
- **13-15 AI-genned photoreal images** following the cosmos-AI-gen
  rule (NASA-press-photo style for cosmic phenomena; period etching
  for pre-photographic events; never AI-gen real-people portraits).

For batches, generate a per-subject AI image pool of ~25-30 distinct
prompts upfront so 18-21 windows fits without reuse.

## Auditing existing renders

```bash
.venv/bin/python -c "
import json, sys
for path in sys.argv[1:]:
    d = json.load(open(path))
    urls = [w['source_url'] for w in d['windows']]
    dups = len(urls) - len(set(urls))
    if dups: print(f'{path}: {dups} duplicate URLs')
" cosmosdecoded/shotlist/*.json | grep -v ".ai_prompts"
```

## Companion rules

- [Shorts cadence calibration](shorts_cadence_calibration.md)
- [Image relevance gate](../cosmosdecoded/learnings/image_relevance_gate.md)
- [AI-gen fallback rule](../cosmosdecoded/learnings/ai_gen_fallback_rule.md)

## Memory pointer

`feedback_shorts_zero_image_reuse.md`.
