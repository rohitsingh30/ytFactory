# Cloned-source mp4s are analysis-only — never use them as footage

## Rule

`data/format_clones/<slug>/source.mp4` exists for **format-DNA analysis only**.
It is a copyrighted mp4 belonging to the external creator we cloned the
format from. It must never appear as:

- a URL in any `<channel>/footage_plan/<slug>.json`
- a `--urls` argument to `find_match_clips.py`, `find_b_roll.py`, or any
  similar source-pool helper
- a `--sources-dir` override pointing at `data/format_clones/`

This applies to every channel and every skill chain `/clone-video-format`
hands into.

## Why

`sportsrecapped/scripts/find_match_clips.py` takes whatever URLs you
pass via `--urls` and slices windows out of them based on whisper
transcript matches. It has no way to know one of the URLs is the
cloned source itself. If you pass the source, every anchor will match
trivially (the source IS the analysis transcript) and the rendered
mp4 will be functionally a re-upload of the cloned video with new
narration over it. That's plagiarism, not cloning.

The helpers were built assuming `--urls` points at *rights-holder
content for the topic* (club official channels, league official
channels, archive.org PD). They do not validate this assumption.

## How this fired (2026-05-08)

First-ship of `/make-football-explainer` on
`hearts-glasgow-duopoly`. The `find_match_clips.py` invocation was
seeded with `https://youtu.be/BQkYsINy95k` (Mega Football's "How
Hearts Are DESTROYING the Entire Scottish System" — the very video
the format was cloned from) plus two unreachable placeholder URLs.
The two placeholders failed; the cloned source returned 14/14
matches and 8 b-roll windows. The render succeeded and produced a
356 MB mp4 entirely composed of cuts from Mega Football's broadcast
video + the new narration. Caught by the user. Deleted.

## What to do instead

For a sports explainer cloned from another sports channel, source
the footage from the **actual rights-holders for the topic**, not
from the cloned compilation:

| anchor topic | rights-holder source class |
|---|---|
| club / kit / stadium b-roll | club official YouTube channel (e.g. HeartsTV, BrightonHA) |
| league context / table graphics | league official channel (SPFL, Premier League) |
| match highlights | broadcaster official (e.g. Sky Sports highlights with attribution) |
| historical archival | archive.org PD entries / BBC archives / Pathe |
| owner / executive footage | club press kit / press-conference reuploads from official sources |

`/clone-video-format`'s output (the fingerprint + ANALYSIS.md) tells
the next skill *what* the format looks like. It is the next skill's
job to find the *content* from legitimate sources.

## Recommended guards

Three places to harden this so the next session can't fire it again:

1. **`find_match_clips.py` + `find_b_roll.py` (helper-side)** —
   compute the SHA-256 of every downloaded source mp4. Walk
   `data/format_clones/*/source.mp4` and compute their hashes. Refuse
   any helper input whose hash collides. Simple, deterministic,
   bullet-proof.
2. **Renderer preflight (`pipeline/render/sports_doc.py`)** — before
   stage 3 (footage download), walk `footage_plan` URLs and reject
   any URL that also appears in
   `data/format_clones/*/fingerprint.json:source_url`. Override:
   `CLONE_SOURCE_FOOTAGE_OVERRIDE=1` (dev only).
3. **`/clone-video-format` SKILL.md handoff** — add a literal one-liner
   telling the next agent: "The source at
   `data/format_clones/<slug>/source.mp4` is for analysis only. The
   generated /<trigger> skill must source footage from rights-holders,
   not from this file." This stops the trap at the prompt level.

## Memory mirror

[`feedback_clone_source_is_not_footage.md`](/Users/rohit/.claude/projects/-Users-rohit-ytFactory/memory/feedback_clone_source_is_not_footage.md)
