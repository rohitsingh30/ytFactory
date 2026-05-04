# AI Recap — channel notes

**Slug:** `airecap`
**Format:** Daily 60s animated recap of one AI/tech announcement
**Primary platform:** X (highest RPM for this niche)
**Secondary platform:** YouTube Shorts (cross-post)
**Status (2026-05-03):**
- ✅ Channel directory + `config.yaml` scaffolded
- ✅ X credentials installed and verified live (test handle: @jjab40)
- ✅ Stage 8b uploader (`pipeline/x_upload.py`) wired and dry-run validated
- ⏳ AI news scraper (`pipeline/sources/ai_news.py`) — TODO
- ⏳ Script generator stage — TODO
- ⏳ First end-to-end render → ship — TODO

## Why this channel exists

X creator-monetization research (2026-05-03) ranked niches by
verified-impression RPM. AI/tech is #2 ($0.25-0.30 per 1M verified
impressions, vs $0.05-0.10 for entertainment). The AI audience also
over-indexes on Premium subscribers — the only impressions that pay.

The Rundown (text newsletter) hit 567K X followers and 120K newsletter
subs in <3 months on the same thesis. The wedge for ytFactory is
**animated explainers** vs their text-thread incumbents.

## Format spec

- **3s hook** — name the news, hint at why it matters
- **10s what happened** — concrete facts, no hedging
- **35s why it matters** — second-order effects, who benefits, what
  ships next
- **12s CTA** — "follow for daily AI recaps"

One announcement per Short. Don't try to cram three. Daily cadence
beats density.

## Sourcing rules

Pick stories that:
1. Are **factual** (no defamation risk — stick to verifiable model
   launches, papers, funding, benchmarks)
2. Have **24h+ legs** — model launches, papers, big shifts. Skip
   one-tweet drama that'll be dead by ship time
3. **Animate well** — architectures, benchmarks, demos. Skip pure
   text/policy stories that need a real screenshot
4. **Don't already have viral text coverage** — if @rowancheung or
   @TheAIGRID already shipped a thread, the algo punishes a dupe

## Tone

Same energy as Stripe's developer blog or Linear's launch posts:
confident, specific, no hype words ("revolutionary", "game-changing",
"insane"). Numbers > adjectives. Concrete > abstract.

Never:
- Use the word "delve"
- Open with "BREAKING:" — that's text-thread territory
- Editorialize ("this is huge") — let the facts hit, the viewer
  decides

## What still needs building

- `pipeline/sources/ai_news.py` — daily news scraper
- airecap script generator (LLM stage that picks the story + writes
  the 60s narration in our existing JSON shape)
- Production X handle registration (currently shipping under @jjab40
  for the bring-up test; switch to a dedicated handle before scale)
- YouTube channel creation (`AI Recap`)
- First 5 mock renders to validate the visual style before going live

## Handle setup for shipping

For the bring-up test, the channel YAML's `x.account` is `airecap`
which maps to `~/.config/ytfactory/x_credentials_airecap.json`. The
credentials file currently authenticates as **@jjab40** (personal
account, validated 2026-05-03 with `verify_credentials`).

To switch to a production handle later: run
`scripts/setup_x_credentials.py airecap` again with the new handle's
keys; it overwrites in place and re-verifies. No config changes
needed.
