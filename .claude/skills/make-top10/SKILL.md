---
name: make-top10
description: Author a 28-32 minute long-form Top-10 list video — footage-only (stock + archival + news clips + document scans + website photos; NO AI image gen), 16:9, continuous narrative connecting #10 → #1, and TWO embedded LIKE+SUBSCRIBE asks (one ~3-4 min in, one in closer). Produces narrations/<slug>.json + shotlist/<slug>.json, then hands off to historyrecapped/scripts/render_footage_only.py with --aspect 16:9. Use when the user says "top 10 <topic>", "10 unsolved cases", "make a top 10 alien abduction list", "30-minute countdown video", or "list-format long-form". For 50-60s Top-5 Shorts use /make-ranking. For single-subject long-form deep-dives use /make-sports-doc. For "last-N rivalry" Shorts use /make-rivalry-recap. Channel is parametric — pass `channel: <slug>`; skill refuses if `<channel>/config.yaml` is missing.
---

# /make-top10 — long-form Top-10 footage-only countdown

This skill produces **30-minute Top-10 list** videos for any channel
that opts in via `render_style: footage_only` in its `config.yaml`. The
format counts down #10 → #1 with **real footage on every beat** (stock
clips, archival film, news cuttings, scanned documents, website
screenshot pans) — no AI image gen, no diffusion model, no GPU image
queue. Lowest-memory long-form path on the factory.

It is **NOT** an extension of `/make-ranking` (which is 50-60s Top-5
Shorts) and **NOT** an extension of `/make-sports-doc` (which is a
single-subject deep-dive). It does, however, ride on the same
**renderer** as `/make-sports-doc` and the same **TTS/caption infra**
as the historyrecapped long-form sleep videos. The diff is the
authoring contract: 10 distinct cases, continuous connective tissue,
2 embedded CTAs.

You wear three hats: **Researcher → Footage Producer → Writer**.

## How to run it

### 1. Confirm topic + channel + tone

If the user's prompt is partial ("make a top 10 alien abduction
video"), state your reading back in one sentence and let them
redirect. Lock these five:

- **Channel slug** — required. Skill checks
  `<channel>/config.yaml` exists and refuses otherwise. If the
  user's topic doesn't fit any existing channel
  (paranormal / true-crime / mystery / horror don't fit any of
  historyrecapped / hindutavaanimated / mystoriesanimated /
  sportsrecapped / rhymetimejunction / cosmosdecoded), tell them to
  run `/create-youtube-channel` first.
- **Topic** — the curatorial frame. e.g. "10 unsolved alien
  abduction cases", "10 most disturbing real-life horror
  incidents", "10 cold cases that still haunt investigators".
- **Tone** — calm-journalistic vs eerie-narrative vs documentary.
  Default: calm-journalistic (matches Sarah-soft long-form voice).
  Eerie-narrative requires a different voice (flag for the user
  to add to `<channel>/config.yaml` if not present).
- **N** — defaults to 10. Honour 7 / 12 / 15 if user insists, but
  the format is balanced for 10 (each case ~2:30-3:00).
- **Order** — countdown (most compelling = #1). Forward order
  (#1 → #10) is **not** supported; if user insists, route them to
  `/make-sports-doc` chapter format instead — that's the
  forward-order long-form option.

Output of stage 1: a 5-line spec.

```
channel: <slug>           # checked: <channel>/config.yaml exists ✓
topic:   "<topic phrase>"
tone:    calm-journalistic | eerie-narrative | documentary
N:       10
slug:    top10-<short-topic>-<YYYYMM>   # e.g. top10-alien-abductions-202605
```

### 2. Hat 1 — Researcher: pull 10 cases with primary sources

For each of the 10 cases you need:

- `title` — short, evocative, 4-8 words. ("The Travis Walton
  Abduction", "The Dyatlov Pass Incident", "The Disappearance of
  Madeleine McCann")
- `year` and `location` — when/where the event happened.
- `summary` — 2-3 sentences. The 10-second elevator version.
- `key_facts` — 4-7 bullet points. Each fact must be checkable
  against ≥2 primary sources.
- `unsolved_hook` — the one sentence that makes a viewer
  comment-bait ("Despite four investigations, no body was ever
  found"). This is the rank's hook line.
- `sources` — minimum **2** primary sources per case. Wikipedia
  is acceptable as ONE source but never the only one. Prefer:
  contemporaneous news reports (NYT, BBC, AP), government
  documents (FBI files, NTSB reports, court records), reputable
  long-form journalism (The New Yorker, Atavist, Snopes,
  BellingCat for OSINT).
- `footage_queries` — 3-5 search queries the footage producer
  (hat 2) will use. Each query must specify a **kind**:
  - `archival` — newsreel / period documentary clips.
  - `news_clip` — broadcast news report from the era.
  - `stock` — generic stock (e.g. "abandoned forest road night",
    "police tape evening lights").
  - `doc_cutting` — scan/pan of a newspaper, court document, FBI
    file. (Static image with slow zoom in renderer.)
  - `photo_pan` — Wikipedia / public-domain photo with a Ken
    Burns-style pan-and-zoom.

Rank by **viewer compulsion**, not chronology. #10 is the most
familiar / lightest case (re-rolling pull-in for low-attention
viewers); #1 is the most disturbing / unresolved / argument-bait.
Order matters — getting it wrong tanks watch-time at the climax.

If you can't find 2 primary sources for a case, **drop it** and
pick the next-best. Do NOT fabricate or infer details. A
fabricated case will burn the channel's credibility on a niche
where viewers fact-check.

### 3. Hat 2 — Footage Producer: source one+ clip per query

For each of the ~30-50 footage queries (3-5 per rank × 10 ranks),
find a real clip. The skill prefers (in order):

1. **archive.org public-domain** — zero ContentID risk. See
   `historyrecapped/scripts/download_long_form_sources.py` for
   the fetch pattern.
2. **Pexels / Pixabay stock** — CC0; safe for monetization.
   `scripts/_shared/find_footage.py` (extract from
   `sportstoriesanimated/scripts/find_b_roll.py` — see efficiency
   wins below) wraps the search.
3. **News broadcast clips** — fair-use under commentary doctrine
   for unsolved-case content; document the justification in the
   shotlist's `_license_note` field per clip. **Never** use a
   YouTube re-uploader's copy of a news clip — go to the
   broadcaster's official channel or archive.
4. **Document scans / photos** — Wikimedia Commons, the Internet
   Archive's Newspapers collection, government FOIA dumps. For
   pan-and-zoom on a still, the renderer does the motion;
   producer just supplies the still + crop region.

Write each clip's window into the shotlist (§4) with
`{in_s, out_s, kind, source_url, _license_note, _design_notes}`.
The renderer's `find_footage.py` helper can sample sources at
1fps to help you pick precise in/out timestamps.

**Hard rule: no clip from a YouTube re-uploader for archival
material.** ContentID will eat the upload (memory:
`long_form_sources.md`).

### 4. Hat 3 — Writer: continuous 28-32 min narration

Total target: **4,500-5,400 words at ~165 wpm** (Kokoro narrative)
or **~3,800-4,300 words at ~135 wpm** (F5-TTS sleep-cadence long-form).
Pick the WPM from the channel's `config.yaml` voice and budget
accordingly.

Strict structure:

```
<INTRO 90-120s, ~250 words>
  - hook (first 10s, ~25 words): contrarian / stakes / mystery
    framing. Names the topic + N. e.g. "Ten cases. Zero solved.
    These are the abductions that still haunt the people who
    were there."
  - promise: what the viewer gets (countdown #10 → #1, real
    footage, primary sources).
  - early-CTA tee-up: "Before we get to number ten, hit
    subscribe — these stories are why this channel exists."
    (This is the FIRST embedded ask, ~90s in.)

<RANK #10, ~2:30-3:00, ~450 words>
  - title card line: "Number ten — <case title>."
  - summary (5-8 sentences): year, location, what happened.
  - key_facts woven into prose, NOT bulleted.
  - unsolved_hook as climax: "And to this day, [unsolved beat]."
  - connective_tissue OUTRO: bridge to #9. e.g. "But if a missing
    plane unsettled you, what happened in case number nine has
    no plane at all — just a sound."

<RANK #9, #8, #7> ... same pattern ...

<MID-VIDEO BEAT — after #6, ~14-15 min in>
  No CTA here. Just one connective beat that re-establishes the
  premise so a viewer who joined late doesn't bounce. e.g.
  "We're halfway through. Five more cases, each more
  disorienting than the last."

<RANK #6, #5, #4, #3, #2>

<RANK #1 — the climax, ~3:00-3:30, ~550 words>
  - extra weight: the most footage, the most sources, the most
    quotable unsolved_hook.
  - deliberately ends on the unsolved beat. NO bridge after #1.
  - direct cut to closer.

<CLOSER 60-90s, ~200 words>
  - the late-CTA: "If these stories kept you watching, like the
    video and subscribe — I publish a new investigation every
    [cadence]."
  - sources call-out: "Every case in this video is sourced —
    primary documents linked in the description."
  - tease for next video (optional but boosts session time):
    "Next week — ten cases of [adjacent topic]."
```

**Continuous-narrative rules:**

- **Connective tissue between every rank.** No cold cut from one
  case to the next. Each rank's last sentence sets up the next
  rank's first sentence. The skill's pre-render quality gate
  (§7) scans for this and blocks emit if any rank-to-rank
  transition is a hard cut.
- **Conversational present tense.** "She walks into the woods.
  She never walks out." Not "She had walked into the woods, and
  she did not return."
- **No editorialising.** "Crazy story, right?" / "I personally
  think…" — kill on sight. The footage and the unsolved-hook
  carry the weight.
- **Phonetic-respell foreign / unusual names** in narration ONLY
  (memory: `feedback_pronunciation_pretts.md`). Preserve original
  spelling in the shotlist's `match_text` and on the
  rank-title-card overlay.
- **Two embedded CTAs only** — one ~3-4 min in (just before #10
  starts, after the intro), one in the closer. NOT five. NOT one
  every chapter. (Pattern from historyrecapped's
  `long_form_support_asks.md` REVISED 2026-05-04.) The
  pre-render quality gate (§7) counts CTA mentions and blocks
  emit on >2 or <2.
- **Ask phrasing — DO copy whatever the channel's
  `learnings/channel.md` literal closer is.** If none exists,
  default: "Hit subscribe, and like the video if you want more
  cases like this." Never "smash that subscribe button" / "vote
  in comments" / closer-anti-patterns from
  `mystoriesanimated/learnings/spoken_subscribe_ask.md`.

### 5. Output: two JSON files

#### `<channel>/narrations/<slug>.json`

```json
{
  "slug": "top10-alien-abductions-202605",
  "format": "top10",
  "topic": "10 unsolved alien abduction cases",
  "tone": "calm-journalistic",
  "duration_target_s": 1800,
  "wpm_target": 165,
  "hook": "Ten cases. Zero solved. These are the abductions that still haunt the people who were there.",
  "narration": "<full continuous text — intro through closer, NO chapter markers in the prose, those go in chapter_timestamps>",
  "ranks": [
    {
      "rank": 10,
      "title": "<case title>",
      "year": 1975,
      "location": "<city, country>",
      "title_card_text": "#10 — <case title>",
      "narration_anchor": "Number ten",
      "key_facts": ["<fact>", "<fact>", "..."],
      "unsolved_hook": "<one sentence>",
      "connective_tissue_out": "<bridge sentence to #9>",
      "sources": [
        {"url": "...", "type": "wikipedia"},
        {"url": "...", "type": "news_archive"}
      ]
    },
    { "rank": 9,  "...": "..." },
    { "rank": 8,  "...": "..." },
    { "rank": 7,  "...": "..." },
    { "rank": 6,  "...": "...", "_mid_video_beat_after": true },
    { "rank": 5,  "...": "..." },
    { "rank": 4,  "...": "..." },
    { "rank": 3,  "...": "..." },
    { "rank": 2,  "...": "..." },
    { "rank": 1,  "...": "...", "connective_tissue_out": null }
  ],
  "cta_inserts": [
    { "position": "after_intro_before_rank10", "text": "<early ask literal>" },
    { "position": "in_closer", "text": "<late ask literal>" }
  ],
  "chapter_timestamps": [
    {"ts_s": 0, "label": "Intro"},
    {"ts_s": 110, "label": "#10 — <title>"},
    {"ts_s": 280, "label": "#9 — <title>"},
    "..."
  ],
  "title_options": [
    "10 Unsolved Alien Abductions That Have No Explanation",
    "These 10 Alien Abduction Cases Were Never Solved",
    "10 Real Alien Abductions Investigators Still Can't Explain"
  ],
  "description_template": "<long-form description with ranked chapter timestamps + sources block>",
  "tags": ["unsolved mysteries", "alien abductions", "..."],
  "source_urls": ["<top-level topic primer>"],
  "source": "manual:make-top10"
}
```

#### `<channel>/shotlist/<slug>.json`

This is the input the renderer reads. **STRICT CONTRACT — single-source
wallpaper mode is BANNED for /make-top10 renders** (post-mortem
2026-05-05: see `learnings/wallpaper_mode_ban.md`). Every window
MUST have its own verified `source_url`. Top-level `source_url` is
forbidden — its presence blocks emit (gate G11).

```json
{
  "slug": "top10-alien-abductions-202605",
  "aspect": "16:9",
  "caption_mode": "long_form",
  "rank_cards": [
    {"rank": 10, "in_s": 0.0, "hold_s": 2.5,
     "title": "#10 — THE SCHIRMER ABDUCTION",
     "subtitle": "Ashland, Nebraska · 1967"},
    {"rank": 9,  "in_s": 0.0, "hold_s": 2.5,
     "title": "#9 — BUFF LEDGE CAMP ABDUCTION",
     "subtitle": "Lake Champlain, Vermont · 1968"}
  ],
  "thumbnail_spec": {
    "title": "10 ALIEN ABDUCTIONS",
    "tagline": "Cases investigators couldn't disprove",
    "hero_image": "<wikimedia URL of #1 case witness portrait OR iconic case visual>",
    "n_chip": "10"
  },
  "windows": [
    {
      "in_s": 0.0, "out_s": 8.0,
      "match_text": "Ten cases",
      "kind": "stock",
      "source_url": "https://www.pexels.com/video/<verified-id>",
      "_license_note": "CC0 — Pexels",
      "_verified_at": "2026-05-05T12:00:00Z",
      "_design_notes": "Slow forest road push-in. Sets eerie tone for hook."
    },
    {
      "in_s": 0.0, "out_s": 12.0,
      "match_text": "Number ten",
      "kind": "archival",
      "source_url": "https://archive.org/details/<verified-item-id>",
      "rank": 10,
      "_license_note": "Public domain — US Government 1968",
      "_verified_at": "2026-05-05T12:00:00Z",
      "_design_notes": "Newsreel cold-open of <event>; first 12s clean."
    },
    "<...one or more windows per rank, totaling ~30-50 windows>"
  ]
}
```

**Required fields per window**:
- `in_s`, `out_s` — clip-local timestamps
- `match_text` — narration anchor phrase
- `kind` — one of `archival`, `news_clip`, `stock`, `doc_cutting`, `photo_pan`
- `source_url` — REQUIRED, must be from approved domains (G12), must be downloadable (G13)
- `_license_note` — REQUIRED, ≥20 chars naming source + license/fair-use rationale
- `_verified_at` — REQUIRED, ISO-8601 timestamp of when URL was HEAD-checked / yt-dlp-simulated
- `rank` — REQUIRED for windows attached to a specific rank; intro/closer/mid windows can omit

**Approved source domains** (G12 enforces):
- `archive.org/details/...` and `archive.org/download/...` (PD newsreel, USAF Project Blue Book, gov.archives.arc.*)
- `commons.wikimedia.org/wiki/File:...` (PD / CC photos + scanned docs)
- `*.pexels.com`, `*.pixabay.com` (CC0 stock)
- `www.loc.gov`, `*.nasa.gov`, `*.nationalarchives.gov.uk`, `*.iwm.org.uk`, `*.smithsonianimages.si.edu` (institutional PD)
- Direct media files at the broadcaster's official channel (e.g. official BBC archive pages) WITH a fair-use commentary `_license_note`

**Banned source domains** (G12 blocks emit):
- ALL YouTube re-uploader / compilation / paranormal-content channels
  (e.g. Weird World, Strange But True, Unexplained, MrBallen reuploads,
  any channel that itself trims third-party documentary footage)
- The watermark scan (G16) catches what the domain check misses

The `kind` field drives renderer behaviour: `doc_cutting` + `photo_pan`
get a slow zoom/pan motion path; `archival` + `news_clip` get the
blurred-letterbox + warm-firelight grade; `stock` passes through
unmolested.

**Per-rank window minimums** (G14): ≥3 windows per rank (intro can have
≥2, closer ≥1, mid-bridge ≥1). A rank with 1 window will go static for
3+ minutes — the failure mode that destroyed the v1 alien-abductions
render. Hard-fail.

### 6. Cast / character handling

Footage-only long-form. No on-screen narrator, no characters.
Cast file is trivial:

`<channel>/cast/<slug>.json`:

```json
{
  "narrator": {
    "description": "calm investigative journalist",
    "default_emotion": "measured",
    "age_band": "middle-aged",
    "gender": "unspecified"
  },
  "supporting": []
}
```

### 7. Quality gates (run BEFORE renderer)

Mechanical, not advisory. Each blocks emit on hit.

1. **Banned-phrase scan** — grep `narration` against
   `<channel>/learnings/` for verbotens. For new mystery
   channels with no learnings yet, also scan against the
   factory-wide closer-anti-patterns ("smash subscribe", "vote
   in comments", verdict acronyms).
2. **Pronunciation pre-pass** — phonetic respelling for foreign
   place / abductee / witness names BEFORE TTS. Memory:
   `feedback_pronunciation_pretts.md`.
3. **Length budget** — total runtime must land in **28-32 min**.
   Compute from word count ÷ wpm_target. Hard-fail outside.
4. **CTA count** — exactly **2** embedded asks
   (`cta_inserts.length == 2`). >2 or <2 blocks emit. Mirrors
   `historyrecapped/learnings/long_form_support_asks.md` 2026-05-04.
5. **Continuous-narrative scan** — every rank from #10 down to
   #2 must have non-null `connective_tissue_out` AND that bridge
   sentence must literally appear at the end of that rank's
   prose in the master `narration` string. Cold cuts block emit.
6. **Source-fidelity check** — every rank has ≥2 distinct
   `sources` entries; ≥1 must be non-Wikipedia. Block on miss.
7. **/critique-audio gate** — run BEFORE handoff. TTS bugs
   invalidate the whole 30-min render. Memory:
   `feedback_critique_audio_before_image_gen.md`.
8. **ContentID scan** — for every shotlist window with
   `kind: archival` or `kind: news_clip`, require either
   `archive.org/details/...` source OR a `_license_note` that
   names the broadcaster + fair-use rationale. YouTube
   re-uploader URLs block emit (memory:
   `long_form_sources.md`).
9. **Aspect + voice match** — assert `shotlist.aspect == "16:9"`
   and `narration.wpm_target` matches the channel's
   `config.yaml` voice's known WPM. Mismatch blocks emit.
10. **Render-style assertion** — assert
    `<channel>/config.yaml` has `render_style: footage_only`. If
    not, instruct user to add it before render.
11. **Wallpaper-mode ban** — assert `shotlist["source_url"]` is
    ABSENT (single-source wallpaper mode is forbidden). All
    visual asset resolution MUST be per-window. Origin: the v1
    alien-abductions render shipped with one source URL covering
    every window and every viewer test failed —
    `learnings/wallpaper_mode_ban.md`.
12. **Approved-domain check** — for every window, parse
    `source_url`'s host. Block emit if:
    - host is `youtube.com` / `youtu.be` / `youtube-nocookie.com`
      and the URL is NOT the official broadcaster channel for the
      content. Compilation / reuploader channels are banned.
    - host is not in the approved list (archive.org,
      commons.wikimedia.org, *.pexels.com, *.pixabay.com,
      www.loc.gov, *.nasa.gov, *.nationalarchives.gov.uk,
      *.iwm.org.uk, *.smithsonianimages.si.edu, broadcaster-
      official channels with `_license_note`).
13. **URL liveness check** — for every window's `source_url`,
    require `_verified_at` ISO timestamp within the last 14 days
    AND HEAD-check passes 200 (for direct media) OR yt-dlp
    `--simulate` succeeds (for archive.org / yt-dlp-supported
    pages). Stale or 404 URLs block emit. The skill performs the
    HEAD/simulate at author time and writes `_verified_at`.
14. **Per-rank window minimum** — every rank from #10 → #1 has
    ≥3 windows in the shotlist. Intro ≥2, closer ≥1, mid-bridge
    ≥1. <3 per rank → static-frame failure mode — block.
15. **Rank cards mandatory** — `shotlist["rank_cards"]` MUST list
    one entry per rank, with `title` and `subtitle` non-empty,
    matching `narrations/<slug>.json`'s
    `ranks[].title_card_text`. The renderer burns these at each
    rank's `chapter_timestamps` boundary; mute viewers cannot
    follow a 30-min countdown without them.
16. **Source watermark + child-detection scan** — the skill
    invokes `scripts/_shared/safety_scan_source.py` for every
    distinct `source_url` in the shotlist. The scanner samples
    N=5 random frames via ffmpeg, runs OCR (`pytesseract`) +
    age-heuristic face detection (mediapipe), and blocks emit
    on:
    - watermark text matching
      `(?i)credit|episode|s\d+e\d+|copyright|©` overlapping the
      source's content area, OR
    - any frame contains a face the age model classifies as <18
      AND the rank context is adult (which it always is for this
      skill's typical topics).
    This gate exists to catch what G12 misses — third-party
    watermarks bleeding through the visual edit.
17. **Caption-mode + voice match** —
    - `shotlist["caption_mode"]` MUST be `"long_form"` (sentence-
      level yellow italic). `"none"` is forbidden for /make-top10
      renders (the v1 alien-abductions render had no captions and
      every mute viewer was lost).
    - Narration `wpm_target` MUST match the renderer being
      handed off to. If the user runs
      `render_footage_only.py` against a 16:9 long-form, the
      renderer must use the channel's `long_form.tts_provider`
      (F5/sarah at ~135 wpm) — NOT the channel's top-level
      `tts_provider` (Kokoro at ~165 wpm). Skill must declare the
      target voice path in narration JSON's `voice_target` field
      and verify the renderer respects it. Mismatch blocks emit.

### 8. Renderer handoff

```bash
.venv/bin/python historyrecapped/scripts/render_footage_only.py \
    --channel <channel-slug> \
    --slug <slug> \
    --aspect 16:9
```

(`--aspect 16:9` is the small renderer extension flagged in the
handoff report — currently the script hard-codes 9:16 letterbox.
Until that lands, the user runs the renderer with the patched
LETTERBOX_FILTER passed via env or temporary local edit. The
patch is one filter graph swap; the skill's stage-4 handoff
notes it explicitly.)

The renderer:

1. Synthesizes TTS via `<channel>/config.yaml`'s tts_provider/voice.
2. Runs whisper word timestamps + beat split.
3. Per-word caption PNGs (long-form caption alignment).
4. yt-dlp / direct-download every shotlist `source_url` into
   `<channel>/footage/sources/`.
5. ffmpeg trim each window with blurred-letterbox 16:9 →
   `scratch/clip_NN.mp4`.
6. ffmpeg concat → `scratch/video.mp4`.
7. ffmpeg mux narration + tpad → `scratch/video_with_audio.mp4`.
8. ffmpeg overlay word captions with cut-aware clamping →
   `<channel>/long_form/<slug>.mp4`.

Total render time: ~25-40 min on M2 Max for a 30-min video.
Wrap with `caffeinate -i` and run with `python -u` per memory:
`feedback_long_form_render_caffeinate.md`.

### 9. Self-learning hook

After the user runs `/critique-audio` or `/critique-video` on the
output of this skill:

1. If a regression is found, classify it:
   - **ONE-OFF** (typo in this script, this case's source was
     wrong) → fix in the `.json` output, append a 1-line note to
     `.claude/skills/make-top10/learnings/_index.md`.
   - **CLASS-OF-BUG** (every Top-10 will hit this) → fix in
     `pipeline/<module>.py` OR in this `SKILL.md`, then append a
     regression note to
     `.claude/skills/make-top10/learnings/<topic>.md` AND mirror
     to `<channel>/learnings/<topic>.md` per CLAUDE.md dual-save.
2. Update `~/.claude/projects/-Users-rohit-ytFactory/memory/MEMORY.md`
   index if a new file was created.
3. If the same class-of-bug fires twice, escalate: add a
   pre-render quality gate in §7 that blocks emit on detection.

### 10. Report back

```
✓ wrote narration: <channel>/narrations/<slug>.json
✓ wrote shotlist:  <channel>/shotlist/<slug>.json
✓ wrote cast:      <channel>/cast/<slug>.json
  topic: <topic>
  ranks (countdown #10 → #1):
    #10  <year>  <title>
    #9   <year>  <title>
    ...
    #1   <year>  <title>
  duration target: ~30:00 (computed: <words> ÷ <wpm> = <m:ss>)
  CTAs: 2 (after_intro_before_rank10 / in_closer) ✓
  footage windows: <N> sourced (<archive>/<stock>/<news>/<doc>/<photo>)
  ContentID risk: <low / fair-use justified / flagged>
next: render via http://127.0.0.1:8765 — pick <channel>, slug
<slug>, hit Generate. Or run:
  caffeinate -i .venv/bin/python -u \
    historyrecapped/scripts/render_footage_only.py \
    --channel <channel> --slug <slug> --aspect 16:9
```

If any quality gate fails, list the failures and STOP. Do not
silently ship a Top-10 with cold-cut transitions, missing
sources, or 1 / 3+ CTAs.

## Important rules

- **30-minute target band is 28-32 min hard.** Outside that, the
  YouTube long-form ad-break density model penalises watch-time.
- **Footage-only.** No AI image gen, no diffusion model loaded.
  If you can't source a clip for a beat, find a different beat —
  don't fall back to a generated still.
- **Countdown #10 → #1 only.** Forward order is `/make-sports-doc`'s
  format, not this one.
- **Two embedded CTAs, exactly two.** One ~3-4 min in, one in
  closer. The pre-render gate enforces.
- **Connective tissue between every rank.** No cold cuts. The
  pre-render gate enforces.
- **Every rank ≥ 2 primary sources.** Wikipedia counts as one
  but never the only one.
- **No YouTube re-uploaders for archival.** archive.org PD or
  paid stock or broadcaster's official channel only.
- **No single-source wallpaper mode.** EVERY window must have its
  own `source_url`. Top-level `shotlist["source_url"]` is BANNED
  and gate G11 enforces. Single-source renders fail every viewer
  test — see `learnings/wallpaper_mode_ban.md` (the v1 alien-
  abductions postmortem documents 100 specific audio/video
  mismatches all traceable to this one shortcut).
- **Mandatory rank cards.** Every rank gets a 2.5s overlay
  (`#10 — TITLE` + `LOCATION · YEAR`) at its
  `chapter_timestamps` boundary. Mute viewers cannot follow a
  30-min countdown without them. Gate G15 enforces.
- **Mandatory captions.** `caption_mode: "long_form"` in the
  shotlist. `"none"` is rejected by gate G17. Sentence-level
  yellow italic captions match the historyrecapped long-form
  signature.
- **Author-time URL verification.** The skill HEAD-checks /
  yt-dlp-simulates every `source_url` BEFORE writing the
  shotlist. Stamp `_verified_at` ISO timestamp per window. Gate
  G13 rejects stale (>14d) or 404 URLs.
- **Pre-render safety scan.** For every distinct `source_url`,
  the skill calls `scripts/_shared/safety_scan_source.py`
  (or stages a TODO if the helper is not yet present) to OCR
  for source-channel watermarks and detect minors. Gate G16.
- **Voice path declaration.** Narration JSON's `voice_target`
  field names which renderer + voice combo this is authored for
  (e.g. `"render_long_form.py:f5_tts/sarah@135wpm"`). Skill
  refuses to hand off to a renderer that uses a different voice
  than declared. Gate G17 enforces.
- **Phonetic-respell foreign / unusual names in narration only.**
  Preserve original spelling in `ranks[].title` and shotlist
  `match_text`.
- **The channel must already exist.** Skill refuses if
  `<channel>/config.yaml` doesn't exist. Tell the user to run
  `/create-youtube-channel` first.
- **The channel must have `render_style: footage_only`** in its
  `config.yaml`. Add it before running this skill.
- **Always use `.venv/bin/python`** for any helper commands.
- **Never run TTS / image gen / ffmpeg in this skill.** That is
  the renderer's job.
- **Never call the Anthropic SDK directly.** LLM calls go
  through `pipeline/llm.py` (`claude -p`).
- **Never bypass the website-first workflow** for production
  renders — `/make-top10` produces JSON, the website triggers
  the render.

## Learnings from prior runs

- **2026-05-05** — `top10-alien-abductions-202605` shipped in
  WALLPAPER MODE (single YouTube source, 12 windows, no rank
  cards, no captions). 100 specific audio/video mismatches
  documented at
  `data/critiques/top10-alien-abductions-202605-100-mismatches.md`.
  Root cause: shotlist used top-level `source_url` instead of
  per-window URLs. Fix shipped: gates G11–G17 added. See
  `learnings/wallpaper_mode_ban.md` for the bullet-point ban
  contract.

## Why this skill is separate from /make-ranking and /make-sports-doc

`/make-ranking` is a Top-5 countdown **Short** (50-60s); the
schema, runtime, and renderer assume Shorts. Stretching it to 30
minutes would require reworking every duration calculation, the
caption density, the closer pattern, and the rank-chip overlay —
at which point you have a different skill, not a variant.

`/make-sports-doc` is a 20-30 min long-form doc, but
single-subject (one player / team / match / rivalry) with a
chapter spine, not a 10-item countdown. Its schema has
`chapters[]`, not `ranks[]`; its writer hat is "investigative
biographer", not "list curator". The renderer is the same
underlying machine (`render_footage_only.py` covers both via the
shotlist schema), but the **authoring contract is incompatible**
— that's the dividing line.

This skill keeps the long-form footage-only renderer and the
historyrecapped TTS+caption infra **as a shared substrate**, and
adds the list-format authoring contract as a thin layer on top.

learnings_consulted:
  - historyrecapped/learnings/long_form_support_asks.md (2-CTA cadence)
  - historyrecapped/learnings/long_form_sources.md (archive.org PD only)
  - historyrecapped/learnings/long_form_captions.md (authored caption alignment)
  - historyrecapped/learnings/long_form_render_caffeinate.md
  - sportstoriesanimated/learnings/footage_align_to_commentary.md
  - feedback_pronunciation_pretts.md
  - feedback_critique_audio_before_image_gen.md
  - feedback_engineer_class_of_bug.md
  - feedback_dual_save_memory_and_docs.md

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
