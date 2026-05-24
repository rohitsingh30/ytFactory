# How viral Shorts CTAs actually look — 2026 visual reference

**Purpose.** Ground future CTA mockups for ytFactory's AITA / storytime
Shorts in **observed, cited** visual conventions — not pattern-matched
priors. The previous mockup ("frosted-glass blur, voting chips,
gradient backgrounds") was correctly called out as fabrication. This
document collects what the public record actually documents, with
URLs, hex codes, and pixel values where the source provides them.

**Honesty contract for this doc.** Every claim is tagged:
- **[OBSERVED]** — primary source describes the exact treatment with
  hex / px / font-name level specificity.
- **[REPORTED]** — secondary source (design blog, caption-tool guide)
  describes the convention; corroborated by ≥2 sources but no frame
  capture in the public record I could access.
- **[CODEBASE]** — values are already in `ytFactory` source, where
  someone on this project previously reverse-engineered the look.
- **[UNVERIFIED]** — the public record gapped out; do NOT assume.

If a section is missing a tag, the underlying claim is unverified —
treat as a prior, not a finding.

---

## TL;DR — the three patterns most defensible from the public record

1. **The dominant viral storytime overlay in 2025-26 is NOT a "closer
   panel" — it is Hormozi-style word-by-word captions covering the
   middle band of the frame, with one keyword highlighted per beat.**
   [REPORTED, multi-source: Ascynd, Blitzcut, Edimakor, SendShort.]
   The viral retention argument: a 3-second "subscribe" end-card on a
   20-sec Short is 15% of runtime and breaks the loop, which costs
   replays — and replays count as views since YouTube's March 2025
   policy change. Source: [virvid.ai/blog/looping-structure-shorts-retention-2026](https://virvid.ai/blog/looping-structure-shorts-retention-2026).

2. **For Reddit-card overlays specifically, the convention is Reddit's
   own dark-theme tokens, top-of-frame, with Subway-Surfers / Minecraft
   parkour gameplay below.** Dark BG `#1A1A1B`, inset card `#272729`,
   orange accent `#FF4500`. [CODEBASE — `pipeline/social/reddit_card.py:56-62`
   already implements this for ScrollPulse, citing Reddit's own design
   tokens.] [REPORTED — corroborated by MakeShort.ai, ClipGOAT.com as
   the default for Reddit-story Shorts.]

3. **Subscribe CTA, when present, is a verbal CTA embedded in the
   narration, not a graphical overlay.** "Follow for more <topic>" or
   "comment YTA or NTA" is delivered in the voiceover during the last
   beat; no graphical panel breaks the loop. [REPORTED, virvid.ai;
   echoed by storyshort.ai and zebracat.ai.] The graphical "animated
   subscribe button" overlay exists primarily as a stock-asset category
   (Uppbeat, Lenos, AutoAE) used by smaller / non-viral channels.

---

## Channels and videos examined

What I could and could **not** verify is important. WebFetch cannot
render JavaScript-driven YouTube video pages, so I could not capture
in-frame screenshots myself in this research session. The channels
below were surfaced via Google searches that returned them as
prominent Reddit-stories Shorts producers; their visual treatment is
**reported** by indexing sites (NoxInfluencer, SocialBlade) and tool
marketing pages, not personally observed in this session.

### Real channels surfaced (handles + indexing-site profile URLs)

| Channel | URL | What the public record says |
|---|---|---|
| **Ask Corey** | [youtube.com/@AskCorey](https://www.youtube.com/@AskCorey/featured) | AITA + relationship drama Shorts; long-form too. Format: narration over stock visuals. [REPORTED] |
| **AITA Stories** | [youtube.com/@AITA.Stories-Off](https://www.youtube.com/@AITA.Stories-Off) | "Gripping revenge stories with betrayals, twists, and family drama." [REPORTED] |
| **TheMJway** | [youtube.com/shorts/UlX0YEzJRmU](https://www.youtube.com/shorts/UlX0YEzJRmU) | Single Short surfaced; visual treatment not extractable via WebFetch. [UNVERIFIED] |
| **AITA & EP Stories** | [youtube.com/@aitaepstories](https://www.youtube.com/@aitaepstories) | AITA + Entitled Parents narration over gameplay background. [REPORTED] |
| **Storytime On Reddit** | [socialblade.com/youtube/channel/UCNRk8NQTlNylzhfPz-HgW7A](https://socialblade.com/youtube/channel/UCNRk8NQTlNylzhfPz-HgW7A) | SocialBlade-tracked Reddit-stories channel. Subscriber count not retrievable via WebFetch (403). [UNVERIFIED] |
| **Storytime With Reddit** | [socialblade.com/youtube/channel/UCYUunpzH7_WtYwvss2Ja9GQ](https://socialblade.com/youtube/channel/UCYUunpzH7_WtYwvss2Ja9GQ) | Same category, indexed by SocialBlade. [UNVERIFIED] |
| **Reddit Stories** | [socialblade.com/youtube/channel/UCnNgzZByxwaaKn-k8avg23w](https://socialblade.com/youtube/channel/UCnNgzZByxwaaKn-k8avg23w) | Same. [UNVERIFIED] |
| **TTS Reddit Stories** | [socialblade.com/youtube/c/ttsredditstories](https://socialblade.com/youtube/c/ttsredditstories) | TTS-narration variant. [UNVERIFIED] |

**Specific individual videos with verifiable URLs:**

- [youtube.com/watch?v=rmy22OdljyM](https://www.youtube.com/watch?v=rmy22OdljyM) — Ask Corey, "AITA for exposing my coworker after he exposed me…"
- [youtube.com/watch?v=mSTgWuRyQjE](https://www.youtube.com/watch?v=mSTgWuRyQjE) — Ask Corey, "AITA for not telling a man that the research he was mansplaining to me was my own…"
- [youtube.com/watch?v=ELylRwqeMwg](https://www.youtube.com/watch?v=ELylRwqeMwg) — Ask Corey, wedding cake AITA
- [youtube.com/watch?v=NbnkbPB2vhw](https://www.youtube.com/watch?v=NbnkbPB2vhw) — Ask Corey, family trip / fiancé's dog
- [youtube.com/watch?v=H_UD_cm7xhk](https://www.youtube.com/watch?v=H_UD_cm7xhk) — Ask Corey, family cruise
- [youtube.com/shorts/UlX0YEzJRmU](https://www.youtube.com/shorts/UlX0YEzJRmU) — TheMJway, r/AmITheAsshole Short

These are real, resolvable URLs. **A human (or a Playwright agent
that can render JS) needs to open them and screenshot the in-frame
CTA treatment.** That work is not in this session because WebFetch
returns only footer/nav for YouTube video pages.

---

## Recurring visual patterns (each tagged with source confidence)

### Typography

**Dominant convention: Komika Axis or Montserrat Black / Anton, ALL
CAPS, word-by-word.** [REPORTED]

- **Komika Axis** is the MrBeast / high-energy YouTube Shorts caption
  font. Specifically cited as "the MrBeast brand" by Submagic and
  Virlo. Source: [submagic.co/blog/how-to-make-captions-like-mrbeast](https://www.submagic.co/blog/how-to-make-captions-like-mrbeast)
  ("Komika" + "CCSignLanguage as alternative"). Casing: uppercase.
  Stroke: heavy black. 2 words per subtitle line. **No specific
  pixel sizes provided in that source — the spec stays font-name level.**
- **Montserrat Black (900 weight)** is the Hormozi spec, used heavily
  in business/educational Shorts but also in the storytime category
  because it shares the "punch keyword" payoff structure. Sources:
  [ascynd.io/en/blog/hormozi-captions](https://ascynd.io/en/blog/hormozi-captions),
  [blitzcutai.com/blog/best-caption-fonts-tiktok](https://blitzcutai.com/blog/best-caption-fonts-tiktok).
- **The Bold Font (Anton)** and **Bebas Neue** are the free
  alternatives most caption tools default to when avoiding Komika's
  licensing. [REPORTED]
- **Playfair Display** (serif) appears in "elegant" storytime as the
  exception, per SendShort's list. [REPORTED — no specific viral
  reference cited.]

**The "bold serif red-and-blue closer" treatment my prior mockup
assumed is NOT a documented viral convention.** I found zero primary
sources recommending serif type for storytime CTAs, and zero viral
references to the red+blue YTA/NTA bicolor split as a graphical
element. That treatment is a fabrication of my model's priors and
should be discarded. [OBSERVED ABSENCE]

### Font size, stroke, position — the Hormozi spec

This is the most concretely documented spec in the public record.
[OBSERVED, single source: [ascynd.io](https://ascynd.io/en/blog/hormozi-captions)]

| Property | Value |
|---|---|
| Font family | Montserrat Black, Anton, or Bebas Neue |
| Font size on 1080×1920 | 80–120 px (10–15% frame height) |
| Casing | ALL CAPS |
| Fill (default) | `#FFFFFF` |
| Fill (highlighted keyword) | `#FFD93D` (yellow) or `#39FF14` (green) |
| Stroke color | `#000000` |
| Stroke width | 8–12 px on 1080×1920 |
| Letter spacing | 0 to −2% |
| Y position | ~60–70% from top (1150–1350 px on 1920-height) |
| Words per reveal | 1–3 per beat |
| Word timing | 200–500 ms per word |
| Animation | None, or instant snap + ≤105% scale-up. **No bounce, no spin, no fade.** |

Note that Ascynd is a caption-tool vendor; this is their stylesheet
for "Hormozi mode" not an independent measurement of an in-the-wild
viral video. Treat as the closest available authoritative spec but
not as primary observation of a specific viral Short.

Blitzcut's variant: same fonts, slightly different highlight palette
— `#f7c204` (yellow) and `#02fb23` (green). [REPORTED]
Source: [blitzcutai.com/blog/best-caption-fonts-tiktok](https://blitzcutai.com/blog/best-caption-fonts-tiktok).

### Position

**Lower-middle band, below the face/visual subject, above the
platform UI.** [REPORTED, Ascynd]

In percentage terms: Y ≈ 60-70% from top of a 1080×1920 frame, so
roughly the band between 1150 px and 1350 px. That's deliberately
above YouTube's bottom-of-Short UI (like/dislike/comment column on
the right, channel handle bottom-left, scrub bar across the bottom
~80-100 px). Caption tools that auto-place text outside this band
get clipped by platform chrome.

The "full-overlay" / mid-screen / centered placement that some hook
generators recommend is a **hook-frame-only** convention — the FIRST
1-3 seconds may use centered or upper-middle title text, then captions
shift to the standard lower-middle position. [REPORTED]

### Background treatment behind text

- **None** for Hormozi-mode captions. Text floats over the video with
  the black stroke doing the legibility work. [REPORTED]
- **Solid `#1A1A1B` Reddit-dark-theme card** for Reddit-post overlays
  on storytime Shorts that show the original post text on-screen
  during the hook. [CODEBASE: `pipeline/social/reddit_card.py:56`.]
- **No frosted-glass blur, no gradient overlay, no border radius
  visible in cited viral examples.** My prior mockup's
  "frosted-glass" was fabrication. [OBSERVED ABSENCE]

### Reddit post card overlay (when used)

This is the one place a graphical overlay IS the convention.
Treatment, per `pipeline/social/reddit_card.py` (already in this repo,
ScrollPulse channel):

| Element | Hex | Notes |
|---|---|---|
| Frame BG | `#1A1A1B` | Reddit dark theme |
| Inset card BG | `#272729` | Where post sits |
| Orange accent | `#FF4500` | r/ pill, upvote chevron, awards |
| Body white | `#FFFFFF` | Title text |
| Body subtle | `#B4B4B4` | Author / age / meta |
| Body meta | `#8C8C8C` | Karma counters / comment counter |
| Pill grey | `#3C3C3E` | Subreddit name capsule |
| Card size | 1080 × 1152 px | Sits in top 60% of 1080×1920 frame; gameplay below in bottom 768 px |

Source: `/Users/rohit/ytFactory/pipeline/social/reddit_card.py` lines
27 (card dimensions) and 56-62 (color tokens). These match Reddit's
actual app design language and are corroborated by tool marketing
(MakeShort.ai, ClipGOAT.com) as the default Reddit-card preset.

This is what should be in any AITA-Short mockup that wants to show
the post on-screen during the hook. **Not a bespoke "black panel
with red and blue serif" — Reddit's actual look.**

### Voting / poll widget shape — UNVERIFIED

I could not find a single primary source documenting a graphical
"YTA vs NTA two-button poll" widget on a viral AITA Short.

- VidIQ, Tubefilter, SendShort, OpusClip, Ascynd, Blitzcut, Submagic,
  CapCut, virvid.ai, storyshort.ai, zebracat.ai — none describe a
  YTA/NTA poll-bar treatment as a viral convention.
- The convention surfaced repeatedly is **verbal** — narrator says
  "AITA? Comment Y or N below" — and the on-screen text is a single
  caption line in the same Hormozi style as the rest of the video,
  not a custom poll widget.
- Search "comment YTA OR NTA poll overlay viral" → zero matches
  describing this widget in the public design-blog record.

**Conclusion: the two-column YTA/NTA voting chip widget is not a
documented viral convention.** If we ship one, we are inventing a
format, not replicating one. That can be a creative choice — but
it should be made consciously, not by claiming we're copying what
works. [OBSERVED ABSENCE]

### Color palette

- White fills with black strokes dominate. [REPORTED, multi-source.]
- Yellow (`#FFD93D` or `#f7c204`) and green (`#39FF14` or `#02fb23`)
  are the only two highlight colors with multi-source backing.
- Reddit orange `#FF4500` for any Reddit-card overlay. [CODEBASE]
- **Red+blue is not documented as a viral storytime palette.** My
  prior mockup invented this. [OBSERVED ABSENCE]

### Layering — overlay vs full-frame

- Captions are **layered over** the underlying video (gameplay,
  AI panels, stock b-roll), never full-frame overlay. [REPORTED]
- Reddit post card is **upper 60%** of frame, gameplay fills lower
  40%, captions layer over both when narrator speaks lines from the
  post. [CODEBASE — ScrollPulse layout in `reddit_card.py`.]

### Entry animations (for static-mockup purposes, what NOT to draw)

- **No bounce, no spin, no fade** in the dominant Hormozi spec. Words
  appear via instant snap or ≤105% scale-up. [REPORTED, Ascynd.]
- MrBeast-style adds "quick zoom-ins or color changes that match the
  video's pace." [REPORTED, Submagic / SendShort.]
- The "slide-up from bottom" pattern my prior mockup implied is **not
  characteristic of viral storytime Shorts** — it's a TikTok-native
  smooth motion more associated with lifestyle / GRWM content.
  [OBSERVED ABSENCE in storytime sources.]

---

## What viral storytime creators do NOT do (curated negative list)

These are the explicit "anti-patterns" the public record either calls
out by absence or actively warns against:

1. **No big bottom-third closer panel.** The 3-sec end-card breaks the
   loop. Source: [virvid.ai/blog/looping-structure-shorts-retention-2026](https://virvid.ai/blog/looping-structure-shorts-retention-2026):
   *"A three-second 'follow for more' at the end of a 20-second Short
   is 15% of your runtime and it breaks the loop entirely."* The
   replays-as-views policy change (YouTube March 2025) made this
   worse — every viewer who would have replayed 3× now contributes 3
   views, but only if your last frame loops cleanly into your first.

2. **No serif type in the caption stack.** Every primary source lists
   sans-serif (Komika Axis, Montserrat, Anton, Bebas Neue, Oswald,
   Roboto, Inter). Playfair Display appears once in a list, no viral
   reference. The "red Times-New-Roman YTA" trope is a 2018 textbook
   meme, not a 2026 viral convention.

3. **No frosted-glass / blurred backplates behind captions.** Hormozi
   spec explicitly says fill + stroke, no backplate. Tool defaults
   (Submagic, OpusClip, CapCut) match this.

4. **No red+blue bicolor splits for poll/vote treatments.** Unsourced
   in the design-blog record. If we want a YTA/NTA voting widget, it
   is invented, not copied.

5. **No graphical "animated subscribe button" overlay slammed into
   the closing seconds.** Stock-asset packs (Uppbeat, Lenos, AutoAE)
   exist for this, but the viral-retention literature considers it
   harmful for Shorts <30s. Verbal CTA is the documented preference.

6. **No mid-screen full-frame poll overlays.** Captions are
   lower-middle; poll widgets (where they exist) sit in the same band
   and are limited to a single line of caption text.

---

## Recommended mockup direction for ytFactory's AITA Shorts

Each design choice below cites the source it's grounded in. Where the
public record is silent, the choice is marked as a **judgment call**
the user must approve.

### Captions (the primary on-screen CTA carrier)

- **Font:** Anton or Bebas Neue (Komika Axis only if we license it;
  otherwise these are the free Komika-shaped alternatives). Weight:
  display-heavy 900. [REPORTED, multi-source]
- **Case:** ALL CAPS. [REPORTED]
- **Fill:** `#FFFFFF`. **Stroke:** `#000000`, width 10 px on
  1080×1920. [REPORTED, Ascynd]
- **Highlight color** (one keyword per beat): `#FFD93D` yellow.
  [REPORTED]
- **Position:** Y between 60% and 70% of frame height. Center-X.
  [REPORTED]
- **Size:** 100 px (mid-point of 80-120 spec). [REPORTED]
- **Words per beat:** 2-3. [REPORTED]
- **Reveal:** instant snap. No bounce. [REPORTED]

### Reddit post hook card (frames 1-3 only)

Use the existing ScrollPulse-style card from
`pipeline/social/reddit_card.py`. It's already correct.

- BG `#1A1A1B`, inset `#272729`, accent `#FF4500`, sub-pill grey
  `#3C3C3E`. [CODEBASE]
- Card sits top-of-frame, 1080 × 1152 px on a 1080×1920 canvas.
  [CODEBASE]
- After the hook, the card slides off (or hard-cuts off) and the
  underlying visual (gameplay / AI panels / b-roll) carries the
  remainder of the Short. **Judgment call** — the public record
  doesn't tell us whether to slide or hard-cut; ScrollPulse's
  existing implementation should be checked.

### Verbal CTA (delivered in narration, NOT graphical)

- "Comment YTA or NTA" or "Was she wrong? Tell me below" in the
  narration of the last beat. [REPORTED, multi-source]
- No graphical poll widget. [OBSERVED ABSENCE — until we explicitly
  decide to invent one and own that as a creative choice.]

### Subscribe prompt — DO NOT ADD a graphical closer panel

- The loop matters more than the subscribe ask. Last frame should
  cut cleanly back to first frame's hook. [REPORTED, virvid.ai]
- If we MUST have a subscribe ask, embed it verbally in the same
  closing beat as the YTA/NTA verbal CTA. No on-screen graphic.

### What this implies for the next mockup

The next mockup should show:
1. The hook frame: Reddit dark-theme post card top-of-frame, gameplay
   visible behind/below, **one** caption line lower-middle in white
   Anton 100px with black stroke and one yellow-highlighted keyword.
2. The mid-frame: Reddit card gone, gameplay/AI-panel full frame,
   captions same style continuing word-by-word.
3. The closing frame: same visual layout as mid-frame — NO closer
   panel, NO subscribe button, NO YTA/NTA chip widget. Just the
   final caption line of the narration.

If the user wants a YTA/NTA voting widget, that is a deliberate
**invented** format — log it as such (we are not copying viral
convention, we are inventing one) so the next agent doesn't think
it has visual-language precedent.

---

## What is UNVERIFIED — gaps the next researcher needs to close

The honest list of where this document falls short. A follow-up
research pass with Playwright (which CAN render JS-driven YouTube)
should fill these in.

1. **No frame captures from actual viral storytime Shorts in this
   session.** WebFetch returned JS-stripped HTML for every
   `youtube.com/shorts/...` and `youtube.com/watch?v=...` URL I
   tried. The caption-spec values above come from caption-tool
   marketing pages (Ascynd, Blitzcut, Submagic, SendShort), which are
   credible but not equivalent to measuring an in-the-wild viral
   Short's captions in pixels.
2. **No subscriber-count / view-count data** for the channels listed.
   NoxInfluencer and SocialBlade both returned HTTP 403 to WebFetch.
   The channels are real; their "viral" status is asserted by the
   indexing sites but not numerically confirmed in this session.
3. **No primary observation of YTA/NTA poll widgets.** I searched
   for them in the public record and found none. This could mean
   (a) they don't exist as a viral convention, or (b) they exist
   but the design-blog ecosystem hasn't catalogued them. The next
   pass should look at the Ask Corey / AITA Stories actual frames.
4. **The 500k+ view threshold the brief asked for is not confirmed
   for any of the URLs listed.** They are surfaced as representative
   by Google's index, but the view counts need eyeball verification.
5. **TikTok cross-references** — TikTok creators who crosspost to
   Shorts were not differentiable from YouTube-native creators in
   this research pass. Same channel handles often span both
   platforms but with different visual conventions per platform.

**The honest call:** if the next mockup decision is high-stakes (e.g.
we're about to ship 1,000 AITA Shorts with one CTA design), do not
ship off this document alone. Open the channels in this list, watch
3-5 viral Shorts per channel, screenshot the in-frame CTAs, and
update this doc with **[OBSERVED]** entries before locking the design.

---

## Image references collected (for the next mockup designer)

These are real URLs the next designer / Playwright agent should open.
**Cited as URLs to study, not as already-analyzed sources.**

- [Ask Corey @AskCorey](https://www.youtube.com/@AskCorey/featured) — channel homepage; sample any Short on the page.
- [Ask Corey — coworker exposed](https://www.youtube.com/watch?v=rmy22OdljyM)
- [Ask Corey — mansplained research](https://www.youtube.com/watch?v=mSTgWuRyQjE)
- [Ask Corey — wedding cake](https://www.youtube.com/watch?v=ELylRwqeMwg)
- [Ask Corey — family cruise](https://www.youtube.com/watch?v=H_UD_cm7xhk)
- [AITA Stories @AITA.Stories-Off](https://www.youtube.com/@AITA.Stories-Off) — channel homepage.
- [AITA & EP Stories @aitaepstories](https://www.youtube.com/@aitaepstories) — entitled-parents + AITA, gameplay background.
- [TheMJway — r/AmITheAsshole Short](https://www.youtube.com/shorts/UlX0YEzJRmU)
- [Storytime On Reddit (SocialBlade)](https://socialblade.com/youtube/channel/UCNRk8NQTlNylzhfPz-HgW7A)
- [Storytime With Reddit (SocialBlade)](https://socialblade.com/youtube/channel/UCYUunpzH7_WtYwvss2Ja9GQ)

### Primary design-spec sources cited above

- [ascynd.io — Hormozi-caption exact style guide](https://ascynd.io/en/blog/hormozi-captions) — the most pixel-specific spec available in the public record.
- [blitzcutai.com — TikTok caption fonts of top creators 2026](https://blitzcutai.com/blog/best-caption-fonts-tiktok) — corroborating font list + hex highlight colors.
- [submagic.co — captions like MrBeast](https://www.submagic.co/blog/how-to-make-captions-like-mrbeast) — Komika Axis + 2-words-per-line rule.
- [sendshort.ai — top 7 YouTube Shorts fonts](https://sendshort.ai/guides/youtube-shorts-font/) — broader font list, less storytime-specific.
- [virvid.ai — looping structure retention 2026](https://virvid.ai/blog/looping-structure-shorts-retention-2026) — the "no closer panel" argument, with the 15%-of-runtime math.
- [storyshort.ai — CTAs for YouTube Shorts](https://storyshort.ai/en/blog/how-to-write-ctas-for-youtube-shorts) — verbal-CTA preference.
- [zebracat.ai — best YouTube Shorts CTAs](https://www.zebracat.ai/post/best-youtube-shorts-call-to-actions) — 22% lift from caption-embedded CTAs vs end-card CTAs.
- [edimakor.hitpaw.com — Hormozi captions 2026](https://edimakor.hitpaw.com/subtitle-tips/how-to-make-alex-hormozi-style-captions.html) — secondary corroboration of Hormozi spec.

### Codebase reference

- `/Users/rohit/ytFactory/pipeline/social/reddit_card.py` — already
  implements Reddit-card overlay with the correct Reddit design
  tokens (`#1A1A1B`, `#272729`, `#FF4500`). Use this for any
  AITA Short that surfaces the original post on-screen.

---

## One-line per finding for fast scan-back

- **Captions** → Anton/Bebas Neue 900, ALL CAPS, 100 px on 1080×1920, white #FFFFFF + black 10px stroke, yellow #FFD93D highlight, lower-middle (Y 60-70%), 2-3 words per beat, instant snap.
- **Reddit post card (when used)** → Reddit's own tokens (#1A1A1B / #272729 / #FF4500), top 60% of frame, codebase already does this right.
- **Subscribe CTA** → Verbal in narration; NO graphical closer panel; preserves the loop and the replays-as-views advantage.
- **YTA/NTA voting widget** → Not a documented viral convention. If we ship one, we're inventing.
- **What NOT to draw** → No serif, no frosted glass, no red+blue, no bounce/spin/fade, no full-overlay closer panel.

---

*Compiled 2026-05-24. Caveats above are load-bearing — the public
record I could access in this session is design-blog dominant, not
in-frame-measurement dominant. The next pass with Playwright-backed
frame capture is what would upgrade most **[REPORTED]** entries to
**[OBSERVED]**.*
