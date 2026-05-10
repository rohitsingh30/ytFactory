# X (Twitter) scraping for ytFactory

Last hardened: 2026-05-08 during the `madrid-locker-room-fight` ship.

## Why scraping at all

Free-tier X API does **not** include `/2/tweets/search/recent`. We have OAuth
1.0a write credentials per channel (in `~/.config/ytfactory/x_credentials_<account>.json`)
but those are write-only. Search/replies require Basic ($200/mo) or above —
not worth the spend yet. We scrape via Playwright instead.

## The auth path that works

**One-time:**

1. Sign into x.com in a real Chrome profile (any one, doesn't have to be the
   one tied to the channel's OAuth credentials).
2. Run `security find-generic-password -wga Chrome` once and click
   "Always Allow" on the keychain prompt — `browser_cookie3` needs this to
   decrypt Chrome's encrypted cookie store on macOS.

**Every run:**

```
.venv/bin/python -m pipeline.x_scrape \
    --query "Real Madrid"            \
    --out data/x_scrape/<slug>.json  \
    --chrome-profile "Profile 3"     \
    --headless --max 25
```

`pipeline/x_scrape.py` reads `~/Library/Application Support/Google/Chrome/<Profile>/Cookies`
(SQLite, encrypted), pulls the `auth_token` / `ct0` / `twid` cookies via
`browser_cookie3`, injects them into a Playwright context, navigates to
x.com — logged-in. The first run also saves the Playwright session at
`~/.config/ytfactory/x_scrape/auth_state.json` so subsequent runs work even
if the source Chrome cookies expire.

## Modes

- `--query "Real Madrid"` — scrapes the search-results page (top-relevance)
- `--tweet-url "https://x.com/.../status/..."` — scrapes the **replies thread**
  under the given tweet (where the funny reaction takes live)

## Pitfalls and fixes (the bug museum)

These are all CLASS-OF-BUG findings from real renders. Each one cost us at
least one wrong render to discover.

### 1. Anonymous X is a hard wall

Every guest URL — `/explore`, `/search`, `/hashtag/X`, `/explore/tabs/sports`
— shows a "Sign in to X" modal with **zero** tweet articles in the DOM. There's
no anon path. Don't waste time trying to bypass the modal — log in.

### 2. Chrome's "in-use" singleton blocks `channel='chrome'` launch

If real Chrome is running, `playwright.launch_persistent_context(channel='chrome')`
hangs at `launchPersistentContext` because Chrome's macOS singleton lock
intercepts the second instance.

**Fix:** don't launch Chrome at all. Pull cookies from Chrome's SQLite via
`browser_cookie3` and inject into Playwright's bundled chromium.

### 3. Reply URLs render the parent tweet at the top

`https://x.com/londibuhle74/status/<id>` loads with the **parent** (Romano's
tweet) at top + the reply lower down. `article[data-testid="tweet"]:first` grabs
the parent. Match by status-id-link instead:

```python
article = page.locator(
    f'article[data-testid="tweet"]:has(a[href*="/status/{tweet_id}"])'
).first
```

### 4. Sticky-nav backdrop-filter leaks blur into the screenshot

X's top header uses `position: sticky` + `backdrop-filter: blur(...)`. When
you `article.screenshot()`, the article's avatar/name row sits behind that
blurred header → the screenshot has a blurry strip across the author info.

**Fix:** inject CSS before screenshot to hide the banner and kill all
backdrop-filters:

```python
page.add_style_tag(content="""
  [role='banner'], header[role='banner'] { display: none !important; }
  div[style*='position: sticky'] { position: static !important; }
  * { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }
""")
```

### 5. Tweet videos need yt-dlp, not the Playwright video element

`<video src="...">` on x.com points to a blob URL, not a downloadable file.
Use yt-dlp:

```bash
.venv/bin/python -m yt_dlp --no-warnings --quiet \
    -f "best[ext=mp4]/best" -o "<slug>.mp4" "<tweet_url>"
```

Auth cookies are already in the Playwright session; yt-dlp can scrape
the video without auth because it discovers the underlying `video.twimg.com`
URL via the embed metadata.

### 6. Capture media region as fractions, not pixels

The screenshot is at device-scale-factor 2 (PNG dimensions = CSS dimensions ×
2). To overlay a video on the static tweet PNG at the correct position
post-render-scaling, store the media bbox as **fractions of the article's
bounding rect** — device-scale-factor agnostic.

```js
const r = videoEl.getBoundingClientRect();
return {
  region_frac: [
    (r.x - articleRect.x) / articleRect.width,
    (r.y - articleRect.y) / articleRect.height,
    r.width / articleRect.width,
    r.height / articleRect.height,
  ]
};
```

The renderer multiplies by the final tweet image dimensions to get pixel
coordinates in the output 1080×1920 frame.

## Output schemas

### `pipeline/x_scrape.py` JSON
```json
{
  "query": "...",
  "scraped_at": "2026-05-08T...Z",
  "tweets": [
    {"id", "url", "author", "handle", "text", "posted_at",
     "likes", "retweets", "replies", "views", "is_verified"},
    ...
  ]
}
```

### `pipeline/x_screenshot.py` manifest JSON
```json
[
  {
    "url", "tweet_id", "slug", "png", "width", "height",
    "media_kind": "video|image|none",
    "media_region_frac": [x, y, w, h],   // fractions of article bbox
    "video_path": "data/x_scrape/shots/<slug>.mp4"  // null if image/none
  },
  ...
]
```

## Rate / quota notes

- We never hit a rate limit during the Madrid render. Tested ~12 sequential
  scrapes including one 55-reply thread without throttling.
- The auth_state.json cookie persists for ~30 days typical X session life.
  When it expires, the next scrape detects the modal and re-imports from the
  Chrome profile.
