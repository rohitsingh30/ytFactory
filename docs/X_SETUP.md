# X (Twitter) uploader — one-time setup

Mirrors the YouTube OAuth flow (`docs/YOUTUBE_SETUP.md` if you have it,
otherwise the comments in `pipeline/upload.py`). You do this **once per
X handle** you want to ship to.

**Status:** the uploader (`pipeline/x_upload.py`) is built and tested.
What you do here is the per-handle bring-up — generate keys at X, then
run the helper script to install + verify them locally.

## 1. Create an X developer account

1. Go to <https://developer.x.com/en/portal/dashboard>
2. Sign in with the X account that owns the handle you want to upload to
3. Apply for a **Free** tier account
   - Free tier limit: **500 posts / month / app**, includes media (video) uploads
   - One Short per day = ~30/month → free tier is plenty for now
   - Upgrade to Basic ($200/mo) only when you outgrow this

## 2. Create an app

1. In the developer portal, **Projects & Apps → Add App**
2. App name: `ytfactory-<channel>` (e.g. `ytfactory-historyrecapped`)
3. **App permissions: Read and Write** (required to post tweets + upload media)
4. **Type of App: Web App, Automated App or Bot**
5. **App info → Callback URI / Redirect URL**: `https://example.com/callback`
   - X tightened format validation — `http://localhost:*` is now rejected
   - The callback is never actually invoked: OAuth 1.0a access tokens
     are generated directly in the dev portal, not via callback flow.
     Any well-formed HTTPS URL passes the validator
6. **Website URL**: `https://github.com/` (any HTTPS URL works — X just
   validates the format, doesn't fetch it)
7. Save

## 3. Generate keys

Inside the app's **Keys and tokens** tab, generate and copy:

- **Consumer keys** → API Key + API Key Secret (these are app-level)
- **Access Token and Secret** → click "Generate" under "Access Token and Secret"
  - Make sure permissions show **Read and Write** before generating
  - If they don't, fix step 2#3 first, then regenerate

You'll have 4 strings:

```
consumer_key
consumer_secret
access_token
access_token_secret
```

## 4. Install credentials via the helper script (recommended)

The X uploader reads per-account credentials from
`~/.config/ytfactory/x_credentials_<account>.json` where `<account>` matches
the `x:` block in your channel YAML.

**Use the helper script** — it prompts for each value with hidden input
(no terminal echo, no shell history, no chat-log exposure), validates the
shape of each token, calls X's `verify_credentials` to confirm they
actually work, and only writes the file if everything checks out:

```bash
.venv/bin/pip install 'tweepy>=4.14'   # one-time
.venv/bin/python scripts/setup_x_credentials.py airecap
```

Replace `airecap` with whichever channel you're setting up. The script
catches the most common mistakes automatically:

- pasting the **Bearer Token** (starts with `AAAAA…`) instead of the
  **Access Token** (`<user_id>-<chars>` format) → rejected with a
  pointer to the right field
- malformed key lengths → rejected before hitting the X API
- handle typo → script verifies against the actual `screen_name` X
  returns and uses the real value

**Manual fallback** (if you prefer to write the JSON yourself):

```bash
mkdir -p ~/.config/ytfactory
chmod 700 ~/.config/ytfactory
# open in your editor — DO NOT paste credentials in chat or terminal scrollback
$EDITOR ~/.config/ytfactory/x_credentials_<account>.json
```

The file shape:

```json
{
  "consumer_key": "...",
  "consumer_secret": "...",
  "access_token": "...",
  "access_token_secret": "...",
  "handle": "airecap"
}
```

Then `chmod 600` it. Repeat for each handle (`historyrecapped`,
`mystoriesanimated`, etc.).

## 5. Wire the channel YAML

Add an `x:` block to each channel's `config.yaml` (sits alongside the
existing `upload:` block):

```yaml
x:
  account: historyrecapped         # matches credentials file
  enabled: true
  tweet_template: |
    {script.hook}

    📖 {raw.url}

    #history #ww2 #shorts
```

## 6. Test

```bash
.venv/bin/python -m pipeline.x_upload \
    --channel historyrecapped \
    --slug battle-of-britain-few \
    --dry-run
```

Dry run prints the tweet text + media path without posting. Drop
`--dry-run` to actually ship.

## Limits worth knowing

- **Video**: max 512 MB, max 2:20 on free accounts (4h on Premium).
  Our 50-60s Shorts fit fine.
- **Codec**: H.264 + AAC, MP4. Our pipeline already outputs this.
- **Rate limits**: 300 tweets / 3h / user, 50 media uploads / 24h / app.
  One Short/day stays well under.
- **Duplicate detection**: posting the exact same tweet text twice in
  ~24h returns a 187 error. The uploader writes a per-render record
  (`<channel>/uploads/<slug>.x.json`) and short-circuits re-runs, same
  pattern as the YouTube uploader.
