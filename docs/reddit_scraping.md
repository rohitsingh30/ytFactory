# Reddit thread scraping for ytFactory

Last updated: 2026-05-08 during the ScrollPulse channel scaffold.

Parallel to [`docs/x_scraping.md`](./x_scraping.md). Reddit is the easier
side of the story — the public JSON API is unauthenticated and stable.

## Why scraping (not PRAW)

PRAW requires a registered Reddit app + OAuth tokens per script. For our
use case (read-only, public threads, low-volume), Reddit's appended-`.json`
URLs return the same data without any auth setup.

```
https://www.reddit.com/r/<sub>/top.json?t=day&limit=25     ← subreddit listing
https://www.reddit.com/r/<sub>/comments/<id>.json?limit=200  ← post + comments
```

`pipeline/reddit_scrape.py` wraps both. No PRAW, no client ID, no token
file. Just a sane `User-Agent` header.

## Two modes

```bash
# Mode A — auto-pick top thread of day from a subreddit
.venv/bin/python -m pipeline.reddit_scrape \
    --subreddit AmItheAsshole \
    --top-n-comments 12       \
    --out scrollpulse/raw/<slug>.json

# Mode B — fetch a specific thread URL
.venv/bin/python -m pipeline.reddit_scrape \
    --thread-url "https://reddit.com/r/.../comments/<id>/<slug>" \
    --out scrollpulse/raw/<slug>.json
```

## Quality gates baked in

Mode A only returns posts that pass:
- `score ≥ min_score` (default 5,000)
- `num_comments ≥ min_comments` (default 200)
- not stickied
- not NSFW (`over_18 == False`)
- not locked
- self-post (text body) — link / image posts can't be narrated as a card

If no post passes, exit 1. The skill aborts; try a different sub or relax
the gates with `--min-score`.

## Output schema

```json
{
  "subreddit": "AmItheAsshole",
  "scraped_at": "2026-05-08T...Z",
  "post": {
    "id", "url", "subreddit", "title", "selftext", "author",
    "score", "num_comments", "upvote_ratio", "created_utc",
    "is_nsfw", "is_locked"
  },
  "top_comments": [
    {"id", "author", "body", "score", "depth", "controversiality", "permalink"},
    ...
  ]
}
```

`top_comments` are sorted by score desc; only top-level (`depth == 0`) for
now. Stickied + `[deleted]` + `[removed]` bodies are filtered.

## Pitfalls

### 1. Don't try to fetch image / video / link posts

Reddit's API returns them just fine — but they have no `selftext`, so the
narrated card-stack format has nothing to read. The scraper's `is_self`
filter prevents these from leaking through Mode A. If you pass a non-self
thread URL via Mode B, you'll get an empty body card and a hollow Short.

**Link-post-only subs to never offer the user** (every top post fails
`is_self` so Mode A always exits 1 — confirmed against r/todayilearned
2026-05-08):

- `todayilearned` — TIL, Wikipedia link required by sub rules
- `news`, `worldnews`, `politics` — link-only by rule
- `science` — link-only by rule
- `gaming`, `gifs`, `pics`, `videos`, `aww` — image/video posts
- `Damnthatsinteresting`, `interestingasfuck` — image/video posts
- `Showerthoughts` — title-only posts (also no selftext; format incompatible)

**Joke-engaged self-post fallbacks** when the user asks for "something
TIL-flavored / where people joke more / more engaged comments":
`AskReddit` (joke-comment king, evergreen) → `TIFU` (confessional comedy)
→ `MaliciousCompliance` / `pettyrevenge` (payoff stories). All three are
self-post + joke-rich top comments.

`/make-reddit-thread` Stage 0 should validate the user's pick against
this list and pivot before scraping.

### 2. Cap scrape rate to avoid 429s

Reddit will rate-limit aggressive scraping (~60 reqs/min from one IP). The
scraper makes **one** call per scrape (Mode B = 1 call, Mode A = 2 calls
since it lists then fetches). The cron driver respects a 4h interval, so
this is well under the limit. If we ever bulk-mine, batch with sleep.

### 3. Don't dedupe by URL — dedupe by post ID

Reddit assigns immutable IDs (`t3_<6char>` or just `<6char>`). The shipped-
posts log under `data/cron/scrollpulse/shipped_ids.json` should track IDs,
not URLs (URLs change when posts are crossposted or moved).

### 4. Body text uses `\n\n` paragraph breaks, NOT real newlines

Reddit selftext serializes paragraphs as double-newlines. `paginate_text()`
in `pipeline/reddit_card.py` collapses them and re-splits at sentence
boundaries to chunk into ~380-char card pages.

### 5. Subreddit pill needs the OFFICIAL casing

Reddit's "subreddit" field returns the canonical-casing name (e.g.
`AmItheAsshole`, not `amitheasshole`). Pass it straight through to the
card renderer — don't lowercase it for display, or the pill reads as
`r/amitheasshole` instead of `r/AmItheAsshole`.

## Quota notes

- Public JSON API has no documented quota for unauthenticated reads.
- We've tested ~30 sequential scrapes from one machine without throttling.
- If we ever do, swap to PRAW with a registered app — the scraper is small
  enough that the swap is mechanical.

## Rendering Reddit cards

See `pipeline/reddit_card.py`. Card kinds:
- `post_card`     — hero (subreddit pill + post title + body excerpt)
- `body_card`     — selftext continuation (paginated)
- `comment_card`  — single top comment with author + body + score
- `verdict_card`  — AITA-style giant verdict (NTA / YTA / etc.)

All cards are 1080×1152 (top region of the 1080×1920 ScrollPulse Short).
Visual signature: `#1A1A1B` bg, `#FF4500` orange r/ mark + accents,
white text, `#272729` inset comment boxes with 4px orange left bar.
