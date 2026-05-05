# Source prep is automated — `pipeline/cosmos_footage_prep.py`

**Class-of-bug fix, 2026-05-05.** First `/make-cosmos-decoder` run on `eddington-1919-eclipse` emitted four JSONs and stopped at the prep gap, leaving 49 long-form clips + 7 Short windows for manual fetch. User feedback was sharp: "source should run in the skill by default". This file is the project-doc half of the dual-save (memory entry: `feedback_skill_runs_source_prep.md`).

## What the prep tool does

`pipeline/cosmos_footage_prep.py` reads a channel/slug shotlist, fetches every `source_url`, and runs ffmpeg zoompan on every `still_ken_burns` entry to produce a proper-aspect mp4. It is idempotent (re-runs skip files already on disk) and structured-error-reporting (so you can see exactly which entries fell through to manual fallback).

```bash
.venv/bin/python -u -m pipeline.cosmos_footage_prep \
    --channel cosmosdecoded --slug eddington-1919-eclipse
.venv/bin/python -u -m pipeline.cosmos_footage_prep \
    --channel cosmosdecoded --slug eddington-1919-eclipse-short
```

## Resolvers wired in

| Host pattern | How it's resolved | Output |
|---|---|---|
| `commons.wikimedia.org/wiki/File:Foo.jpg` | Wikimedia API `imageinfo` with `iiurlwidth=2400` | 2400px-wide JPG thumb (much faster than full original) |
| `upload.wikimedia.org/wikipedia/commons/...` | direct download | full asset |
| `images.nasa.gov/details/<id>` | NASA `images-api.nasa.gov/asset/<id>`, picks rank `orig > large > medium > small > thumb` | best mp4 / jpg |
| `archive.org/details/<id>` | `archive.org/metadata/<id>`, prefers h.264/MPEG4 mp4 | direct `archive.org/download/...` URL |
| direct `.mp4`/`.jpg`/`.png`/`.webp`/`.mov`/`.webm` | urllib download | as-is |
| `royalsocietypublishing.org`, `nature.com`, `wiley.com`, `arxiv.org`, `ligo.caltech.edu`, `ligo.org`, `pexels.com`, `pixabay.com`, `storyblocks.com`, `thetimes.co.uk`, `timesmachine.nytimes.com`, `articles.adsabs.harvard.edu` | **manual fallback** with curator instruction printed | nothing — user saves asset by hand |

For `still_ken_burns` entries the tool then runs:

```bash
ffmpeg -y -loop 1 -i <still> -t <duration> \
  -vf "scale=<2x output>:..., crop=<2x output>:..., \
       zoompan=z='min(zoom+<step>,1.18)':d=<frames>:s=<aspect dims>:fps=30, \
       format=yuv420p" \
  -r 30 -c:v libx264 -preset veryfast -crf 20 -movflags +faststart -an <out.mp4>
```

`<aspect dims>` is `1920x1080` for long-form (16:9) and `1080x1920` for Shorts (9:16). The zoom step is computed so total zoom reaches 1.18 over the clip's frame count.

## How the skill calls it

`/make-cosmos-decoder` Section 7 of the SKILL.md now invokes the prep tool by default after authoring. The flow is:

1. Author the four JSONs (narrations + shotlists).
2. Run the eight quality gates.
3. **Run the prep tool for both shotlists** (this file).
4. Surface manual-fallback list to the user (paywalled hosts).
5. User downloads the manual fallbacks (typically 2–4 entries).
6. Invoke the renderer.

The skill is responsible for steps 3 + 4. Skipping them leaves the renderer hard-failing at `FileNotFoundError: shotlist clip 0 source missing`.

## Engineering-efficiency wins surfaced

This prep tool is **channel-agnostic**. It works on any shotlist that follows the cosmosdecoded schema (clips/windows with `source_url`, `source_type`, `source`, `in_s`, `out_s`, optional `aspect`). When `/make-top10`, `/make-katha`, and `/make-sleep-history` next get touched, they should adopt this same prep step — file `pipeline/cosmos_footage_prep.py` is misnamed; promote it to `pipeline/footage_prep.py` and import from each footage-only skill. (Heuristic #46 — engineering-efficiency scout.)

## Manual-fallback class — what to expect

The first eddington-1919-eclipse run will have a non-zero manual-fallback list because:
- `royalsocietypublishing.org/doi/10.1098/rsta.1920.0009` — paper page screenshot, save by hand.
- `nature.com/articles/nature01997` — Bertotti 2003 title page, save by hand.
- `pexels.com/video/...` — Pexels needs API key.
- A handful of Wikimedia URLs were authored by pattern-guess (e.g. `File:Cambridge_Observatory_-_geograph.org.uk_-_1497339.jpg`); some won't resolve and need a real File:-page lookup.

The prep tool prints the URL + destination filename for each manual case. Walk the list, save each asset, re-run the prep tool to confirm zero remaining errors before invoking the renderer.

## See also

- `pipeline/cosmos_footage_prep.py` (the implementation)
- `.claude/skills/make-cosmos-decoder/SKILL.md` Section 7 (the skill flow)
- `.claude/skills/make-cosmos-decoder/learnings/auto_source_prep.md` (skill-side mirror)
- Memory: `feedback_skill_runs_source_prep.md`
