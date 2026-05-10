---
name: make-tweet-reaction
description: Author a 50-60s tweet-reaction Short for SportsRecapped — discover viral sports tweets via the X scraper, screenshot the funniest replies (with their videos/GIFs auto-downloaded), then narrate the discourse wave-by-wave (confusion → wit → receipts → callback → real take → absurd → cherry) with a wry-commentator voice. The opener is a bold red headline + a breaking-news tweet underneath; the closer is YouTube-style LIKE+SUBSCRIBE icon buttons. Live videos play inside the tweet card at the captured bbox; word-level yellow captions sync to whisper-aligned audio with sticky+lead polish; emoji decorations render via PIL+ffmpeg overlay (libass can't render Apple Color Emoji on macOS). Renders via sportsrecapped/scripts/render_tweet_reaction.py and ships through pipeline/upload.py. Use when the user says "make me a tweet-reaction short", "react to <viral story>", "twitter is roasting <X>", "make a Short on what twitter is saying about <Y>", or names a current sports controversy and asks for "a reaction Short". For sports head-to-head countdowns use /make-last5; for narrated documentary Shorts use /make-history-short pattern; for long-form sports docs use /make-sports-doc.
---

# /make-tweet-reaction

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

Author a tweet-reaction Short for SportsRecapped that's literally about *what
Twitter is saying* — a 50-60s vertical Short composed of real x.com tweet
screenshots (with their attached images/videos playing live), narrated by a
wry sports commentator who labels each reaction-wave as it lands.

This skill exists because **breaking sports moments don't always have
broadcast footage** — they're rumors, leaks, dressing-room incidents — but
they generate massive Twitter discourse with high-engagement viral takes that
make natural Short content. Format-wise this is SportsRecapped's 5th lane,
distinct from the Tifo-style narrated documentary.

First exemplar shipped 2026-05-08: `madrid-locker-room-fight` (Tchouaméni vs
Valverde dressing-room fight, 4 live-video reaction tweets + 4 image tweets,
56s, video id `3qC6tU_YHrw`).

---

## What good looks like (the wave-by-wave arc)

Beat 0 = HERO: bold red headline + the highest-engagement breaking-news tweet.
Beat 1 = OFFICIAL SOURCE: Fabrizio Romano / Sky Sports / club statement.
Beats 2-8 = THE WAVES, each labeled by the narrator:
- Wave 1 (confusion) — "First — the obvious question nobody dared ask"
- Wave 2 (wit) — "Then someone got philosophical"
- Wave 3 (receipts) — "Then someone surfaced the receipts"
- Wave 4 (callback) — "And the callback nobody saw coming"
- Wave 5 (hot take) — "Then came the realest take of the night"
- Wave 6 (absurd) — "Soon things got fully Hollywood"
- Wave 7 (cherry) — "But this one — said it best"
Beat 9 = LIKE + SUBSCRIBE button card.

Each bridge MUST characterise the next tweet's flavour, not just announce it.
That's the difference between "story" and "list of tweets."

---

## Pipeline (what this skill drives)

```
Stage 1 — DISCOVER          pipeline/x_scrape.py --query/--tweet-url
Stage 2 — PICK              you (the model) read 30-50 tweets, pick 7-8 funny ones
Stage 3 — SCREENSHOT+VIDEO  pipeline/x_screenshot.py
Stage 4 — AUTHOR            write narrations/<slug>.json with the 10-beat arc
Stage 5 — RENDER            sportsrecapped/scripts/render_tweet_reaction.py
Stage 6 — UPLOAD            pipeline.upload.youtube_upload (only if user OKs)
```

All channel rules + visual specs live in
[`sportsrecapped/learnings/tweet_reaction_format.md`](/Users/rohit/ytFactory/sportsrecapped/learnings/tweet_reaction_format.md).
Read that first if you haven't seen this format before.

All scraping rules + bug-museum live in
[`docs/x_scraping.md`](/Users/rohit/ytFactory/docs/x_scraping.md).

Bug catalog (CLASS-OF-BUG findings to never re-hit):
[`memory/feedback_tweet_reaction_pitfalls.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_tweet_reaction_pitfalls.md).

---

## Stage 0 — Scope (AskUserQuestion, prefilled defaults only)

Per the skill-prefilled-options heuristic in `make-skill`. NEVER free-text
ask. Always provide 2-4 prefilled options grounded in this format's reality.

**Q1. What's the breaking story?**
Options:
- (Recommended if a current viral topic exists) "<that topic>"
- "Pick the top trending sports topic on x.com right now"
- "Use a tweet URL I'll paste" (then ask for URL)
- "Other (free text)"

**Q2. Channel?** (Default: SportsRecapped)
- SportsRecapped (only one configured for this format today)

**Q3. Privacy on upload?**
- Public (Recommended)
- Unlisted (preview before share)
- Don't upload yet (just render)

That's it. Beat-level decisions (which 7 tweets, the bridges, the closer
caption) are the model's job — don't ask the user to do that work. (See
`memory/feedback_skill_question_budget.md`.)

---

## Stage 1 — Discover

```bash
.venv/bin/python -m pipeline.x_scrape \
    --query "<topic>"                              \
    --out data/x_scrape/<slug>.json                \
    --chrome-profile "Profile 3"                    \
    --headless --max 25 --scroll-passes 4
```

This returns the top 25 tweets for the query. Read them. Identify the
**anchor tweet** = the highest-engagement breaking-news tweet (usually
Fabrizio Romano, Madrid Zone, theAthletic, or the official club account).
The anchor's URL feeds the next call.

Then scrape **the replies** under the anchor (or another high-engagement
viral source):

```bash
.venv/bin/python -m pipeline.x_scrape \
    --tweet-url "<anchor_tweet_url>"               \
    --out data/x_scrape/<slug>-replies.json        \
    --chrome-profile "Profile 3"                    \
    --headless --max 60 --scroll-passes 8
```

The replies are where the FUNNY content lives. Most search-result tweets are
news-style; replies are the comedy.

---

## Stage 2 — Pick

Read the replies JSON. Score each candidate on:
- **Connection to story** (does it actually reference Tchouaméni / Valverde
  / Madrid? generic content fails)
- **Funny ≠ random** — wit, callback, receipts, hot take, absurd-meme.
  See the wave list above.
- **Has media?** Tweets with attached images / GIFs / short videos render
  better than text-only.
- **Engagement** as a tiebreaker — top 5% likes/retweets is a quality signal.
- **Profanity check** — skip tweets with "shit" / "fuck" / verdict-acronym
  YouTube filters. The screenshot text is visible to the moderation system.

Pick **8 tweets total**:
1 hero tweet (high-engagement breaking-news with Valverde+Tchouaméni-style
imagery — players visible, not just text)
1 source tweet (Fabrizio Romano-tier)
6 reaction tweets covering the waves above

Write the chosen URLs into `data/x_scrape/<slug>-picks.json` with this shape:
```json
{"tweets": [{"handle": "@...", "url": "https://x.com/.../status/..."}, ...]}
```

---

## Stage 3 — Screenshot + auto-download videos

```bash
.venv/bin/python -m pipeline.x_screenshot \
    --urls-file data/x_scrape/<slug>-picks.json   \
    --out-dir data/x_scrape/shots                  \
    --manifest data/x_scrape/shots/manifest.json   \
    --chrome-profile "Profile 3"
```

The screenshot tool:
- Hides X's sticky nav before screenshotting (no backdrop-blur leak)
- Matches the article by tweet ID (no parent-tweet-grabbed-instead bug)
- Detects media kind (`video` / `image` / `none`) + bounding box (as fractions)
- Downloads tweet videos via yt-dlp when present

Verify each PNG visually — if any tweet shows the wrong author, the tweet ID
match failed (rare, but worth a glance). If a video tweet's PNG shows the
poster's first frame, that's correct (the live video plays at render time).

---

## Stage 4 — Author narration JSON

Write `sportsrecapped/narrations/<slug>.json` with this 10-beat shape:

```json
{
  "slug": "<slug>",
  "format": "tweet_reaction",
  "channel": "sportsrecapped",
  "tts_speed_override": 1.06,
  "hook": "<one-line headline>",
  "title_options": ["<3 YouTube titles>"],
  "source_query": "<query>",
  "source_anchor_tweet": "<anchor URL>",
  "scraped_at": "<ISO>",
  "beats": [
    {
      "kind": "hero_heading_tweet",
      "heading": "<2 LINES IN CAPS>",
      "url": "<hero tweet URL>",
      "png": "data/x_scrape/shots/<...>.png",
      "narration": "<5-8s tight setup>"
    },
    {
      "kind": "tweet",
      "url": "<source tweet URL>",
      "png": "<...>",
      "narration": "<full context with names/numbers>"
    },
    /* 6 more reaction tweets, each with a wave-labelling bridge */
    {
      "kind": "card_closer_buttons",
      "screen_text": "<short caption above buttons>",
      "narration": "<like/subscribe CTA>"
    }
  ]
}
```

Authoring rules (channel-doc + post-upload-analysis classify these as
quality gates, not nice-to-haves):
- Total narration ~125-140 words → 50-60s at chatterbox speed 1.06.
- Each bridge characterises the next tweet's *flavour* (the wave label).
  Don't read the tweet flatly.
- Use full proper-noun spellings ("Tchouaméni" not "Tchouameni" — the TTS
  pronounces accents correctly).
- No verdict acronyms in audio (per cross-channel rule).

---

## Stage 5 — Render

```bash
set -a && source .env && set +a && \
.venv/bin/python -m pipeline.skill_dispatch render --cmd sportsrecapped/scripts/render_tweet_reaction.py -- \
    --narration sportsrecapped/narrations/<slug>.json   \
    --manifest data/x_scrape/shots/manifest.json        \
    --out sportsrecapped/shorts/<slug>.mp4
```

`.env` must be sourced for `CLOUDRUN_TTS_CHATTERBOX_URL`. The renderer
handles:
- Per-beat TTS via cloudrun_chatterbox (sarah.wav)
- Whisper word-boundary forced alignment + SequenceMatcher mapping
- Sticky+lead caption sync (LEAD=0.05s, captions extend to next-word-start)
- Emoji PNG overlays via PIL+Apple Color Emoji
- Live-video composition for tweets with downloaded mp4s
- Concat re-encode (no glitch frames)

Total render time: ~90-120s for a 10-beat Short on this M2 Max.

---

## Stage 6 — Upload (only if user OKs)

If the user said "ship it" / "upload" in Stage 0:

```python
from pipeline.upload import youtube_upload
result = youtube_upload(
    mp4_path=mp4, title=narration["title_options"][0],
    description=<built from tweet credits + hashtags>,
    tags=<topic-derived>,
    privacy="public",  # or "unlisted" if user picked that
    category_id="17",  # Sports
    account="sportsrecapped",
    thumbnail_path=<hero frame .jpg>,
)
```

Save the upload record to `sportsrecapped/uploads/<slug>.json` for the
post-upload analysis loop (per CLAUDE.md).

Generate thumbnail by sampling a hero-beat frame: `ffmpeg -ss 2 -i <mp4>
-frames:v 1 -q:v 2 <slug>.thumb.jpg`.

---

## Quality gates (run before declaring done)

1. **Total length 50-60s** — outside this window, trim narration or add a
   beat. YouTube Shorts cap at 60s.
2. **Visual inspection** — sample frames at hero (~3s), each live-video
   beat, the closer. Check for tofu glyphs, broken layout, caption
   overlapping critical content.
3. **Audio coherence** — listen end-to-end. Watch for chunked-TTS hiccups
   between beats; each beat's narration should flow into the next.
4. **Hero tweet has player imagery** — the breaking-news graphic must show
   the players (or a related visual anchor). Pure text loses retention.
5. **No profanity in caption text** — visible monetisation risk.
6. **Captions in sync** — sticky+lead should land each word with the
   syllable. If a beat feels off, sample frames at the word's expected
   timestamp.

---

## After upload

Run the post-upload analysis (per CLAUDE.md): walk back through every
surprise/fix/pivot in the conversation, classify ONE-OFF / CLASS-OF-BUG /
PIPELINE-BUG / WORKFLOW-IMPROVEMENT / PRONUNCIATION, and write to the right
file:

- ONE-OFF → 1 line in `learnings/_index.md`
- CLASS-OF-BUG → new file in `sportsrecapped/learnings/` and mirror to memory
- PIPELINE-BUG → fix the code, document in `docs/`
- WORKFLOW-IMPROVEMENT → update CLAUDE.md or this skill

Default to **write, not skip**. Compounds over renders.
