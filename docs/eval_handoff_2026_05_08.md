# Content eval handoff — 2026-05-08

For a creative reviewer evaluating the videos themselves.

## What we made + why

**Channel:** MyStoriesAnimated (YouTube)
**Audience:** Reddit-storytime + animated-AITA / TIFU watchers — same
audience as channels like AITA TikTok, ColdFusion's lighter cuts,
the cartoon Reddit-narration tier on YT Shorts.

**Goal of the 4 shipped Shorts:** prove the end-to-end pipeline can
produce **listenable + watchable** narrative Shorts on autopilot
once the user picks "TIFU" from a menu. No hand-editing of
narration, no manual audio fixes, no per-frame retouching. The 4
TIFU stories were:

1. Dentist named Dr. Martin / mom's baby named Martin (mortified-mom voice)
2. "Love you too" accidentally told to boss on a work call (panicked-male voice)
3. Asked a trainee out, then he showed up at her training (embarrassed-female voice)
4. Roasted *The Mechanic* (2011) for 15 years — turns out it was a
   leaked work-in-progress cut (amused-male voice)

**Format:** 65-71s vertical 1080×1920 mp4. Crayon-style cartoon
characters, word-level yellow karaoke captions, female narrator
voice (sarah.wav clone via Cloud Run Chatterbox), bell+thumb_up
icons baked into the last frame as the closer.

## Where the videos are

### Local mp4 files

```
/Users/rohit/ytFactory/mystoriesanimated/reddit_tifu/shorts/
├── tifu-tifu-by-accidentally-making-my-dentist-think-i-named-my.mp4   68.8s, 20.6 MB
├── tifu-tifu-by-accidentally-replying-love-you-too-to-my-boss.mp4     70.0s, 23.2 MB
├── tifu-tifu-by-asking-for-the-number-of-a-trainee.mp4                70.6s, 13.4 MB
└── tifu-tifu-by-thinking-the-mechanic-2011-was-a-campy-cult-fil.mp4   64.6s, 19.0 MB
```

Open any locally with QuickTime / VLC. The reviewer's lens is "would
I keep watching past 2 seconds" + "do I know what's happening on
mute" + "would I stop scrolling for the punchline".

### YouTube — uploaded private + scheduled to auto-publish

These will go public at the times below (UTC + IST). Until then they
show "Privacy: Private (scheduled)" if you have channel access, and
the URLs return "video unavailable" to a stranger.

| publish (UTC) | publish (IST) | story | URL |
|---|---|---|---|
| 07:14 | 12:44 | dentist Dr. Martin | https://youtu.be/AZV1pZ8v3h8 |
| 09:14 | 14:44 | "love you too" boss | https://youtu.be/e_zauVYi5z0 |
| 11:14 | 16:44 | trainee number | https://youtu.be/QvcCTZmUjHA |
| 13:14 | 18:44 | *Mechanic 2011* roast | https://youtu.be/pL2oZfTecSw |

After 18:44 IST today all four are public on
https://youtube.com/@MyStoriesAnimated.

## What's coming (the 100-shorts queue)

103 more scripts pulled across 5 niches; render queue running detached:
- **AITA-animated** ×30 — Reddit r/AmItheAsshole, crayon character art, animated.
- **AITA-text** ×34 — same source, Reddit-text-on-pastel-bg style.
- **AITA-cooking** ×20 — same source, text-on-cooking-footage style.
- **TIFU** ×6 (10 total minus the 4 already shipped) — Reddit r/tifu, crayon.
- **Wiki Oddities** ×4 — Wikipedia "unusual deaths in the 21st century".
- **Today in History** ×5 — Wikipedia "On this day".

All 99 will be rendered + landed in
`mystoriesanimated/<niche>/shorts/<slug>.mp4` over the next ~10 hours.

Then a **launchd cron** uploads them at **10/day, 2.4-hour intervals
starting 07:00 IST tomorrow** — so the channel posts continuously for
~10 days without me touching anything. Niche rotation alternates so
the public feed shows variety (AITA → cooking → TIFU → TIH → wiki)
rather than 30 AITA-animated in a row.

## How to evaluate (creative reviewer)

Pick any 1-2 mp4s from the local folder above. Watch them once.

### Hook (first 1.5s)

- Does the first frame stop your scroll thumb?
- Is the spoken hook a curiosity-gap question or a strong-claim verb?
- Are the captions readable on a phone in mute mode?

### Pacing (mid)

- Does the audio race or drag? (Validated 142 WPM — should sound
  natural, not chipmunk.)
- Do the images change fast enough to keep attention but slow enough
  to read?
- Does the story escalate, or stay flat?

### Closer

- Does the last frame have visible LIKE / COMMENT / SUBSCRIBE icons?
- Does the narrator end on a comments-bait question ("Was I out of
  line?", "What would you have done?")?
- Would you actually leave a comment?

### Visual quality

- Hands look right? (Was a real bug; gated now via haiku vision.)
- Same character throughout, or does she morph?
- Crayon style consistent across frames, or does it whip from
  sketchy → polished → storybook?

### The thumb-stopping test

If frame 0 was the static thumbnail, would you stop scrolling for it?

## Open quality issues you might still spot

- Some frames drift from crayon style to a more polished look.
- Characters change subtle features (hair shade, nose shape) across
  frames — the channel character lock isn't 100% perfect yet.
- Closer panel is just bell+thumb icons, no spoken "LIKE if YTA"
  text overlay. (This is by design as of 2026-05-02 — the spoken
  narration carries the LIKE/COMMENT line.)
- The 4 mp4s are 64-71 seconds each, just over YouTube's 60s Shorts
  cap. They post as **regular videos**, not Shorts. The rewriter
  has been retuned to produce ≤140 words for the next batch so they
  fit the cap.

## Decision the reviewer can give

- **Ship the 100-shorts cron as-is** — quality bar is good enough.
- **Block the cron** — fix specific issue X first, e.g. "no, the
  hands are still off" or "I want the closer panel to spell out the
  CTA text".
- **Mixed** — ship some niches (TIFU, AITA-animated) but hold others
  (cooking, TIH) until reviewed.

The cron is loaded but doesn't fire until 07:00 IST tomorrow. To
pause it before then:

```bash
launchctl unload ~/Library/LaunchAgents/com.ytfactory.upload-next.plist
```

To resume:

```bash
launchctl load ~/Library/LaunchAgents/com.ytfactory.upload-next.plist
```

## Single-question gut check

> Would you, in 2 seconds, stop scrolling for any of these 4?

If yes → green-light the 100-shorts run.
If "maybe" → block + tell me what was missing.
If "no" → block + we go back to the drawing board on hook + visuals.
