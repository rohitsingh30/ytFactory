---
name: make-reddit-thread
description: Author a 50-60s Reddit-thread Short for the ScrollPulse channel — discover a top thread on a round-robin subreddit (AITA, AskReddit, TIFU, relationship_advice, MaliciousCompliance, pettyrevenge, Showerthoughts, UnpopularOpinion, TrueOffMyChest, Confession), scrape it via Reddit's public JSON API, render the post + top comments as authentic Reddit-style cards (orange r/ pill, dark theme, comment thread styling), and stack a randomly-picked Subway-Surfers-style gameplay loop in the bottom 40% of the frame. Same caption + emoji + closer-button infra as make-tweet-reaction. Renders via scrollpulse/scripts/render_split_screen.py. Use when the user says "make me a reddit short", "make a reddit thread short", "AITA short of <topic>", "react to <r/sub>", "scrollpulse short", "brain rot reddit", or names a specific reddit thread URL. For X/Twitter reaction Shorts use /make-tweet-reaction; for animated AITA stories with character art (different format) use /make-mystories-short.
---

# /make-reddit-thread

> **State I/O + schema contract** (auto-loaded via CLAUDE.md): all
> channel JSON state ops in this skill go through `pipeline.state_client`
> against `gs://ytfactory-prod-v2-state` — never local file Writes.
> Narration payloads (when this skill produces or reads them) conform
> to `NicheVideo` ([`pipeline/niche_schema.py`](/Users/rohit/ytFactory/pipeline/niche_schema.py)).
> Conventions + per-niche defaults:
> [`docs/skill_state_io_conventions.md`](/Users/rohit/ytFactory/docs/skill_state_io_conventions.md).
> Channel `learnings/` + `config.yaml` stay on laptop (still read with `Read`).

Author a Reddit-thread Short for **ScrollPulse** — the "brain rot" format
where the top half shows a Reddit post + top comments as authentic
Reddit-styled cards while the bottom half loops Subway-Surfers-style
gameplay. The narrator reads the post + comments wave-by-wave, captions burn
in over the gameplay region (so they never cover Reddit body text), and the
closer is the same LIKE+SUBSCRIBE pill card as the sportsrecapped tweet
reactions.

First exemplar: `aita-hibachi` (2026-05-08, AITA top of day, ~7K upvotes).

---

## What good looks like

- **10 beats**, 50-60 s total
- Beat 0 = HERO: the post card (subreddit pill + title + first body paragraph)
- Beats 1-4 = SETUP/TWIST: selftext continuation cards (paginated)
- Beats 5-7 = TOP COMMENTS: the funniest / spiciest / most-upvoted reactions
- Beat 8 = VERDICT: AITA-style giant verdict (NTA/YTA) on full-frame card
- Beat 9 = CTA: COMMENT-your-verdict + SUBSCRIBE pill closer

Each comment beat's narration **labels the take's flavor** before reading it
("then someone went straight for the throat", "the receipts came out") —
same wave-by-wave pattern as `/make-tweet-reaction`. Don't read flatly.

---

## Pipeline (what this skill drives)

```
Stage 1 — DISCOVER       pipeline/reddit_scrape.py
Stage 2 — RENDER CARDS   pipeline/reddit_card.py (post + body chunks + comments + verdict)
Stage 3 — AUTHOR         scrollpulse/narrations/<slug>.json
Stage 4 — RENDER         scrollpulse/scripts/render_split_screen.py
Stage 5 — UPLOAD         pipeline.upload.youtube_upload (only if user OKs)
```

Channel rules: [`scrollpulse/learnings/channel.md`](/Users/rohit/ytFactory/scrollpulse/learnings/channel.md).
Caption + emoji + alignment lessons: [`memory/feedback_tweet_reaction_pitfalls.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_tweet_reaction_pitfalls.md)
(applies to both formats).

---

## Stage 0 — Scope (AskUserQuestion, prefilled)

Per the `make-skill` heuristic: never free-text ask. 2-4 prefilled options
grounded in the format reality.

**Q1. Which thread / subreddit?**
- (Recommended) "Top of day from r/AmItheAsshole" — verdict-bait, evergreen
- "Top of day from r/<auto-pick from round-robin>" — rotates through the 10
- "Specific thread URL I'll paste" → ask for URL
- "Other (free-text subreddit)"

**Q2. Privacy on upload?**
- Public (recommended)
- Unlisted (preview before share)
- Don't upload — just render

That's it. Beat-level decisions (which 5 comments, the bridges, the verdict)
are the model's job. Don't ask the user to do that work.

### Stage 0 preflight gates (before scraping anything)

1. **OAuth token** — if upload != "Don't upload", check
   `~/.config/ytfactory/youtube_token_scrollpulse.json` exists. If
   missing, surface to the user immediately:
   *"ScrollPulse YouTube channel hasn't been created yet. Run
   `/create-youtube-channel` on your target Google account, then
   re-run me."* Don't waste 90 s rendering an mp4 that can't ship.
   The `account="scrollpulse"` reference in `config.yaml` is a
   placeholder until then.

   **Gameplay mp4 required.** Check `scrollpulse/footage/gameplay/`
   has ≥1 `.mp4` file. If empty, surface immediately: *"Drop ≥1
   Subway-Surfers / Minecraft / GTA gameplay mp4 (60 s+) in
   `scrollpulse/footage/gameplay/` and re-run me — without gameplay
   the bottom 40% of every frame is dead space and the format
   premise is gone."* The renderer's placeholder-gradient fallback
   is only for layout smoke-tests. See
   [`scrollpulse/learnings/gameplay_required_hard_fail.md`](/Users/rohit/ytFactory/scrollpulse/learnings/gameplay_required_hard_fail.md).

2. **Link-post-only sub guard** — if the user's pick lands on
   `todayilearned`, `news`, `worldnews`, `politics`, `science`,
   `gaming`, `gifs`, `pics`, `videos`, `aww`,
   `Damnthatsinteresting`, `interestingasfuck`, or `Showerthoughts`,
   the scraper will exit 1 (every top post fails `is_self`). Pivot
   silently to `AskReddit` — joke-comment king and a clean self-post
   match for "TIL-flavored / engaged comments / more jokes" asks.
   Confirm the pivot to the user. See
   [`docs/reddit_scraping.md` § 1](/Users/rohit/ytFactory/docs/reddit_scraping.md).

3. **Verdict label** — if the picked sub is non-AITA, choose a
   1-3-word verdict label fitting the sub's payoff (PEAK., WINNER,
   OOF., KARMA., RUN., MIND. BLOWN., etc.) and pass `--verdict` +
   `--verdict-caption` to `pipeline.reddit_card`. The CLI no longer
   hardcodes NTA. See
   [`scrollpulse/learnings/non_aita_verdict_override.md`](/Users/rohit/ytFactory/scrollpulse/learnings/non_aita_verdict_override.md).

---

## Stage 1 — Discover

```bash
.venv/bin/python -m pipeline.reddit_scrape \
    --subreddit AmItheAsshole              \
    --top-n-comments 12                    \
    --out scrollpulse/raw/<slug>.json
```

Returns a JSON with `post` + `top_comments`. Quality gates baked in:
- post score ≥ 5,000 upvotes
- num_comments ≥ 200
- not stickied / NSFW / locked
- self-post only (text, not link/image)

If gates fail, the script exits 1 and the skill aborts. Try a different sub
or relax the gates with `--min-score`.

---

## Stage 2 — Render Reddit cards

```bash
# Always wipe the per-slug card cache first — pipeline.reddit_card output
# is cached and stale cards survive code fixes (see split_screen_polish.md
# § 5). Cheap to re-render, expensive to debug a stale card.
rm -rf scrollpulse/cache/<slug>

.venv/bin/python -m pipeline.reddit_card \
    --bundle scrollpulse/raw/<slug>.json \
    --out-dir scrollpulse/cache/<slug> \
    --verdict 'NTA' \
    --verdict-caption 'The verdict is in'
```

For non-AITA subs override `--verdict` / `--verdict-caption` (e.g.
AskReddit → `--verdict 'PEAK.' --verdict-caption "reddit's verdict on
humanity"`). See
[`scrollpulse/learnings/non_aita_verdict_override.md`](/Users/rohit/ytFactory/scrollpulse/learnings/non_aita_verdict_override.md)
for the per-sub label table.

Generates:
- `00_post.png` — hero card (subreddit pill, title, first body paragraph)
- `01_body_<N>.png` — selftext continuation pages
- `02_comment_<N>.png` — one card per top comment
- `03_verdict.png` — AITA-style verdict card (you set the verdict in Stage 3)

All cards are 1080×1152 (top region of the 1080×1920 Short).

---

## Stage 3 — Author narration JSON

`scrollpulse/narrations/<slug>.json` shape:

```json
{
  "slug": "<slug>",
  "format": "reddit_thread",
  "channel": "scrollpulse",
  "tts_speed_override": 1.06,
  "hook": "<one-line headline>",
  "title_options": ["<3 YouTube titles>"],
  "source_url": "<reddit thread URL>",
  "subreddit": "<sub>",
  "scraped_at": "<ISO>",
  "beats": [
    /* Hero post card */
    {"kind": "card_split", "png": "scrollpulse/cache/<slug>/00_post.png",
     "narration": "From r-slash <sub> — this person asks: <title>"},

    /* Selftext continuation, 1-4 cards depending on length */
    {"kind": "card_split", "png": "...01_body_1.png",
     "narration": "<paraphrase the chunk in narrator-voice>"},

    /* Top comment beats — wave-labelled */
    {"kind": "card_split", "png": "...02_comment_1.png",
     "narration": "<wave label>: <paraphrase or read the comment>"},

    /* Verdict — full-frame card */
    {"kind": "card_full", "png": "...03_verdict.png",
     "narration": "The verdict — is in. <NTA/YTA>."},

    /* Closer */
    {"kind": "card_closer_buttons", "screen_text": "COMMENT your\nverdict",
     "narration": "Comment your verdict. Subscribe for more Reddit chaos."}
  ]
}
```

**Authoring rules:**
- 50-60 s total → ~125-140 narration words.
- Read the title literally on beat 0 ("AITA for ruining a hibachi dinner").
- Paraphrase long selftext into narrator voice — don't read 4 paragraphs flat.
- Each comment beat opens with a **wave label** ("then someone went for the
  throat", "the receipts came out", "and someone called it"). Don't read
  flatly.
- Strip any banned phrases (per `scrollpulse/config.yaml::gates.banned_phrases`)
  — "shit" / "fuck" etc. are visible monetisation risks even when in quotes.
- Verdict beat goes BIG — narrator speaks slow, with a pause. The full-frame
  card carries the visual weight.

---

## Stage 4 — Render

```bash
set -a && source .env && set +a && \
.venv/bin/python -m pipeline.skill_dispatch render --cmd scrollpulse/scripts/render_split_screen.py -- \
    --narration scrollpulse/narrations/<slug>.json   \
    --out scrollpulse/shorts/<slug>.mp4
```

Renderer behaviour (v6, 2026-05-08 architecture — see
[`scrollpulse/learnings/split_screen_polish.md`](/Users/rohit/ytFactory/scrollpulse/learnings/split_screen_polish.md) §§ 6–15):

- **Picks one gameplay mp4** from `scrollpulse/footage/gameplay/*.mp4`
  (round-robin via `data/cron/scrollpulse/last_gameplay.json`); refuses
  to render when the dir is empty unless `--allow-no-gameplay` is set.
- **Random start offset** — `random.uniform(0, gp_dur - 60)` is passed
  via `-ss` before `-stream_loop -1` so each render begins at a
  different point in the gameplay file (~133 distinct openings on a
  3:13 source).
- **Card-on-gameplay layout** — gameplay scales to fill 1080×1920 via
  blurred-letterbox (force_original_aspect_ratio=decrease for fg,
  =increase + crop + gblur for bg). The card PNG is RGBA-transparent
  except for the card band at `CARD_TOP_OFFSET=220 px` from the top.
  Card auto-shrinks to content height (~450 px without upvote/score
  footers).
- **Center captions** — `Alignment 5` + `MarginV=0` puts caption
  baseline at frame center `y=960`, sitting in the gameplay region
  below the card. Word-by-word style for now (still uses Whisper
  alignment — sometimes hallucinates proper nouns; see
  `split_screen_polish.md` § 11 for the open authored-text fix).
- **Voice override** — narration JSON may set `tts_voice_override` and
  `tts_ref_text_override` to swap voices without editing
  `config.yaml`. Used for A/B voice tests.

**Parallel-render warning** — never run two
`render_split_screen.py` processes against the same `--out` mp4.
ffmpeg leaves a 1.5 MB stub with no `moov` atom. Always use distinct
output paths for A/B variants.

Total render time: ~90–120 s for a 9-beat Short on M2 Max.

---

## Stage 5 — Upload (only if user OKs)

If the user said "ship it" / "upload" in Stage 0:

```python
from pipeline.upload import youtube_upload
result = youtube_upload(
    mp4_path=mp4, title=narration["title_options"][0],
    description="<built from subreddit + thread URL + tags>",
    tags=["shorts", "reddit", subreddit.lower(), "aita", "drama", ...],
    privacy="public",  # or "unlisted"
    category_id="24",  # Entertainment
    account="scrollpulse",
    thumbnail_path=<hero frame .jpg>,
)
```

The `account="scrollpulse"` token may not exist yet — the actual YouTube
channel hasn't been created. If `youtube_upload` raises a token error, tell
the user to first run `/create-youtube-channel` on their preferred Google
email.

---

## Quality gates (run before declaring done)

1. **Total length 45-62 s** — outside this window, trim narration or split
   a body card into two beats. (Same window as the cron driver.)
2. **Visual inspection** — sample frames at hero, mid (split-screen
   working?), verdict, closer. Check for layout drift (gameplay scaling),
   caption overlap with comment text, tofu glyphs.
3. **Audio coherence** — listen end-to-end. Captions sync to syllables.
4. **No banned phrases** — `pipeline/text/banned.py` style check on every
   visible card text + every narration string.
5. **Subreddit pill rendered correctly** — orange circle + r/<sub> visible
   on hero card.

---

## After upload

Run the post-upload analysis (CLAUDE.md): walk the conversation, classify
ONE-OFF / CLASS-OF-BUG / PIPELINE-BUG / WORKFLOW-IMPROVEMENT / PRONUNCIATION,
write to:
- ONE-OFF → 1 line in `scrollpulse/learnings/_index.md`
- CLASS-OF-BUG → new file in `scrollpulse/learnings/` + memory mirror
- PIPELINE-BUG → fix the code
- WORKFLOW-IMPROVEMENT → update CLAUDE.md or this skill

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
