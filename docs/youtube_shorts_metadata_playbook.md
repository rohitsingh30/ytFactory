# YouTube Shorts metadata playbook (2026)

> Research-only playbook consumed by the publish-metadata generator. Every
> claim cites a primary URL. Compiled 2026-05-24 from YouTube Help,
> Google API docs, Backlinko, VidIQ, Buffer, and analyst aggregators.
> Where the public record is silent or contradictory, the gap is called
> out in the final **What's UNVERIFIED** section.

---

## TL;DR

The single highest-leverage field is the **first 100 characters of the
description**, because (a) it is the entire text a Shorts viewer sees
above the "more" fold inside the player, (b) the first 2-3 sentences are
where YouTube's algorithm weights keyword signals most heavily, and (c)
the first three hashtags placed in the description auto-render as
clickable chips *above* the title in the Shorts UI, so description and
title fight for the same eyeball. The single biggest mistake is
**dumping `#Shorts` plus 10 unrelated hashtags into the title**: it
burns the 100-char title budget, looks spammy in search snippets,
violates YouTube's "hashtags must be directly related" rule (which can
remove the video from search), and contributes nothing the
description-placement would not. Put the hook in the title, put the
hashtags in the description, and never exceed 15 hashtags total (60+
hashtags = YouTube silently ignores ALL of them).
[YouTube Help on hashtags](https://support.google.com/youtube/answer/6390658?hl=en),
[Hashtag Tools 2026 best-practice](https://hashtagtools.io/blog/youtube-shorts-hashtags-title-vs-description-2026),
[Backlinko description guide](https://backlinko.com/hub/youtube/video-description).

---

## Title

### Character count

- **API hard cap: 100 characters**. UTF-8, no `<` or `>`. From the
  official `videos.snippet.title` schema.
  [YouTube Data API v3 reference](https://developers.google.com/youtube/v3/docs/videos).
- **Mobile truncation: ~40-50 characters** inside the Shorts feed and
  search snippets. Anything past that is clipped with an ellipsis.
  [Influencer Marketing Hub character-limits guide](https://influencermarketinghub.com/youtube-character-limits/).
- **Observed optimum: 51 characters / 8 words** is the *median* of
  trending Shorts in a 10K-Shorts dataset; 63% of titles fall in the
  20-60 character band; 65% are 4-9 words including hashtags.
  [TunePocket 10K-Shorts analysis](https://www.tunepocket.com/youtube-shorts-video-titles/).
- **Click-through: titles under 50 characters with curiosity hooks
  outperform hashtag-heavy titles by ~25%** (analyst aggregation, not a
  primary YouTube study).
  [Teleprompter trending-Shorts report 2026](https://www.teleprompter.com/blog/trending-youtube-shorts).

**Rule for ytFactory: target 45-60 characters, hard cap at 70.** Any
hook longer than that loses its tail to the ellipsis on the mobile
Shorts feed and the in-app share card.

### Emojis and unicode

- **Emojis in Shorts titles correlate with +49% views on average,
  +61% at the top end, +17% like rate, +24% comments** in a vidIQ
  analysis of 128 million YouTube videos. Same study finds the effect
  is *negative* for long-form — emojis are a Shorts-only lever.
  [vidIQ 128M-video study](https://vidiq.com/blog/post/emojis-in-youtube-titles/),
  [vidIQ Threads summary post](https://www.threads.com/@vidiq/post/DPoTcobD2Jq/first-the-data-on-youtube-shorts-is-absolutely-clearemojis-more-views-period-49-).
- **Cap: 1-2 emojis maximum**, placed at the start or end of the title.
  More than 2 reads as spam and inflates the truncation budget.
  [vidIQ emoji guidance](https://vidiq.com/blog/post/emojis-in-youtube-titles/).
- Avoid `<` and `>` (API-rejected) and zero-width unicode (search index
  treats them as separators).
  [YouTube Data API v3 reference](https://developers.google.com/youtube/v3/docs/videos).

### Hook formats that hit

VidIQ's 18-format catalogue cross-referenced against the trending-Shorts
dataset surfaces these as the strongest fits for our verticals:

| Format | Example | Best for vertical |
|---|---|---|
| Direct dialogue / "POV: …" | `POV: your sister sends 150 wedding invites to your address` | AITA / storytime |
| Conflict question (subreddit-style) | `AITA for telling my MIL she's not invited?` | AITA / storytime |
| "I was today years old when…" | `I was today years old when I learned Karna's real father` | mythology / history |
| "Have you heard about…?" | `Have you heard about the day Pompeii's clock stopped?` | history / cosmos |
| "X vs Y" comparison | `Ronaldo 2008 vs Messi 2009 — who actually won?` | sports |
| "How we knew…" (channel native) | `How we knew the universe was expanding` | cosmos |
| Number + escalation | `In 79 seconds, Vesuvius killed 2,000 people` | history |
| "The day they…" reveal | `The day Hanuman lifted an entire mountain` | mythology |

[vidIQ 18 hook templates](https://vidiq.com/blog/post/viral-video-hooks-youtube-shorts/),
[Teleprompter trending formats](https://www.teleprompter.com/blog/trending-youtube-shorts).

YouTube has confirmed 64% of viewers decide whether to keep watching
within **2.5 seconds**, so the hook in the title must match the
on-screen hook in the first 0.5 seconds of the video — title-content
mismatch is the #1 reason a Short gets swiped past.
[Async YouTube trends 2025](https://async.com/blog/youtube-trends/).

### What the algorithm does with the title

- Titles are **not displayed inside the swipeable Shorts feed itself**
  (only the description's first line is), but they ARE the dominant
  text signal in: search results, the channel page Shorts tab,
  homepage recommendations, hashtag shelves, and Google search Shorts
  carousels.
  [Backlinko 1.3M-video YouTube SEO study](https://backlinko.com/youtube-ranking-factors).
- Backlinko's study of 1.3M videos found **only a slight correlation
  between exact-keyword titles and rankings** — semantic understanding
  has overtaken keyword density. Don't keyword-stuff; write the hook
  for a human and let the spoken audio / description carry the SEO.
  [Backlinko ranking-factor study](https://backlinko.com/youtube-ranking-factors).

### Per-vertical title preferences

- **AITA / storytime**: lead with the conflict, name the antagonist
  role (MIL, roommate, boss). Subreddit acronym (`AITA`, `TIFU`) is a
  searched keyword — keep it.
  [AITA viral-title pattern survey](https://www.makeshort.ai/).
- **Mythology (Hindi/Hindutava)**: name the character + verb. Hindi
  titles index well when the `defaultLanguage=hi` field is set
  (otherwise the title gets indexed under Latin-script romanisation).
  [YouTube Data API defaultLanguage](https://developers.google.com/youtube/v3/docs/videos).
- **Sports**: name both entities + a year or stat. The algorithm
  surfaces sports Shorts via the related-entity graph, not via
  hashtags, so the entity names in the title carry the entire load.
  Avoid `#shorts` in title — sports Shorts shelves use the entity
  cluster.
  [Versacreative Shorts algorithm 2025](https://versacreative.com/blog/how-the-youtube-shorts-algorithm-works-in-2025/).
- **Cosmos / history (educational)**: "How we knew…" / "The day…" /
  number-led titles dominate the Top-10 viral lists in this vertical.
  Education category (27) preserves the algorithmic boost; Entertainment
  (24) under-performs in this niche because it places the Short in a
  far more competitive recommendation pool.
  [Inside Créateurs category guide 2026](https://insidecreateurs.com/en/blog/tutorials/youtube-categories-2026-complete-list-which-to-choose).

---

## Description

### Length

- **API hard cap: 5,000 bytes.**
  [YouTube Data API v3 reference](https://developers.google.com/youtube/v3/docs/videos).
- **Recommended sweet spot: 200+ words** (Backlinko). Longer
  descriptions give YouTube more topical signal; some top Shorts have
  no description at all, but that's a leakage of free SEO.
  [Backlinko description guide](https://backlinko.com/hub/youtube/video-description).
- **Realistic for Shorts: 60-150 words** is what hit Shorts actually
  ship. The fold-above-"more" line dominates the value; the rest is
  hashtags + boilerplate.
  [Letter Counter description guide](https://lettercounter.org/blog/youtube-shorts-description/),
  [GhostShorts description blueprint](https://ghostshorts.com/blog/how-to-write-youtube-shorts-description-2026).

### First-line hook (the only part viewers see)

- **First 100-125 characters** are the only thing visible above the
  "more" fold in the Shorts player. Same characters double as the
  search-snippet text.
  [Hollyland Shorts description guide](https://store.hollyland.com/blogs/creator-hub/write-youtube-shorts-title-and-description),
  [CRKLR Shorts SEO 2026](https://crklr.com/news/how-to-optimise-youtube-shorts-for-seo/).
- Write the first line as a **natural sentence containing the primary
  keyword** plus a curiosity gap. Do NOT keyword-stuff and do NOT lead
  with social-media links.
  [Backlinko description guide](https://backlinko.com/hub/youtube/video-description).
- **Put the first 2-3 hashtags inside this fold** — they'll render as
  clickable chips above the title AND count toward the discoverability
  signal.
  [Hashtag Tools 2026 placement guide](https://hashtagtools.io/blog/youtube-shorts-hashtags-title-vs-description-2026).

### Hashtag placement: description, NOT title

- The **first three description hashtags auto-pin above the title** as
  clickable links in the Shorts UI — same visibility benefit as putting
  them in the title, without burning the 100-char title budget.
  [YouTube Help on hashtags](https://support.google.com/youtube/answer/6390658?hl=en),
  [Hashtag Tools 2026 placement](https://hashtagtools.io/blog/youtube-shorts-hashtags-title-vs-description-2026).
- The algorithm weights description hashtags **equal to** title
  hashtags for categorisation and discovery.
  [Hashtag Tools 2026 placement](https://hashtagtools.io/blog/youtube-shorts-hashtags-title-vs-description-2026).
- Title-placement of hashtags is therefore strictly worse: same SEO
  weight, less hook real estate.

### CTAs in description

- Like / subscribe / "follow for part 2" CTAs work best **at the end of
  the description**, not the start. Lead-with-CTA destroys the
  fold-above-more visible snippet.
  [ClickMinded description templates](https://www.clickminded.com/templates/seo/youtube-description-template/).
- **No "click the link in description"** for Shorts — links inside the
  Shorts player are NOT tappable (they only become clickable on the
  desktop watch page, which Shorts viewers don't see). The CTA should
  be content-native ("comment your verdict", "subscribe for the rest of
  the saga").
  [GhostShorts description blueprint](https://ghostshorts.com/blog/how-to-write-youtube-shorts-description-2026).

### SEO keyword density

- **2-3 natural repetitions of the primary keyword** across the
  description, never stuffed.
  [Backlinko description guide](https://backlinko.com/hub/youtube/video-description).
- Backlinko's 1.3M-video study found **zero correlation between
  keyword-stuffed descriptions and ranking** for the stuffed term —
  semantic understanding has eaten the keyword-density playbook.
  [Backlinko ranking study](https://backlinko.com/youtube-ranking-factors).
- Each video's description should be **unique** (don't reuse the same
  block across the channel — it dilutes per-video topical signal).
  [Backlinko description guide](https://backlinko.com/hub/youtube/video-description).

---

## Hashtags

### Count and format

- **Sweet spot: 3-5 hashtags.** Six is fine; ten starts looking spammy.
  [Subscribr Shorts hashtag analysis](https://subscribr.ai/p/youtube-shorts-hashtags-virality),
  [Hollyland hashtag count guide](https://www.hollyland.com/blog/tips/how-many-hashtags-should-i-use-on-youtube).
- **Hard limits from YouTube Help:**
  - More than 15 hashtags on a single video → YouTube ignores **all**
    hashtags on that video.
  - More than 60 hashtags → YouTube ignores all hashtags AND can
    remove the video from search/uploads.
  - Hashtags must contain no spaces.
  - Hashtags must be "directly related" or YouTube may remove the
    video.
  [YouTube Help official rules](https://support.google.com/youtube/answer/6390658?hl=en).
- **First 3 hashtags in the description auto-pin above the title.**
  [YouTube Help official rules](https://support.google.com/youtube/answer/6390658?hl=en).

### Mix

- **Multi-layer pattern**: 1 broad (#Shorts), 1 platform/format
  (#YouTubeShorts), 2-3 niche/topic, optional 1 brand/channel.
  [Sprout Social YouTube hashtag guide](https://sproutsocial.com/insights/youtube-hashtags/).
- Avoid the lazy bucket of `#viral #trending #fyp` on videos that
  aren't either — irrelevance flags can demote.
  [YouTube Help guidelines](https://support.google.com/youtube/answer/6390658?hl=en).

### Is `#Shorts` actually required?

- Vertical 9:16 + sub-60s video is **automatically classified as a
  Short** by YouTube without any hashtag. The `#Shorts` hashtag is a
  belt-and-braces signal, not a requirement, and most 2025+ analyses
  agree it's optional.
  [Miraflow Shorts hashtag analysis 2026](https://miraflow.ai/blog/youtube-shorts-hashtags-2026-do-they-still-matter),
  [SendShort Shorts hashtag guide 2026](https://sendshort.ai/guides/use-hashtags-youtube-shorts/).
- **Recommendation: include `#Shorts` anyway** as one of the 5
  hashtags. It costs nothing and the public consensus still treats it
  as best-practice.

### Per-vertical hashtag clusters

(All clusters fit in the 5-hashtag budget; first 3 are the pinned-above-title ones.)

- **AITA / storytime (mystoriesanimated)**:
  `#AITA #redditstories #Shorts #storytime #reddit`
  ([Reddit-storytime template study](https://www.capcut.com/explore/reddit-story-template)).
- **TIFU**: `#TIFU #redditstories #Shorts #storytime #funnystories`
  ([Reddit-storytime template study](https://www.capcut.com/explore/reddit-story-template)).
- **Wiki oddities / TIH**: `#history #didyouknow #Shorts #facts #todayinhistory`
  ([Filmora hashtags for Shorts 2026](https://filmora.wondershare.com/youtube/youtube-shorts-hashtags.html)).
- **Mythology Hindi (hindutavaanimated)**:
  `#mahabharat #ramayan #Shorts #hindumythology #krishna` (or swap last for `#hanuman` / `#shiva` per topic)
  ([Hinduism hashtag list (Display Purposes)](https://displaypurposes.com/hashtags/hashtag/hinduism)).
- **Sports (sportsrecapped)**: entity-led cluster, NOT generic.
  `#football #ronaldo #messi #Shorts #footballshorts` (replace with
  actual entities per video — e.g. `#fcbarcelona #realmadrid`)
  ([SportShorts hashtag page on YouTube](https://www.youtube.com/hashtag/sportshorts)).
- **Cosmos / physics (cosmosdecoded)**:
  `#space #physics #Shorts #astronomy #nasa`
  ([Filmora hashtags for Shorts 2026](https://filmora.wondershare.com/youtube/youtube-shorts-hashtags.html)).
- **History long+short (historyrecapped)**:
  `#history #ancienthistory #Shorts #historyfacts #worldhistory`
  ([Filmora hashtags for Shorts 2026](https://filmora.wondershare.com/youtube/youtube-shorts-hashtags.html)).

---

## Tags (the hidden field)

### Still relevant in 2026?

- **Officially still part of the schema** (`videos.snippet.tags`, max
  500 characters combined including comma separators).
  [YouTube Data API v3 reference](https://developers.google.com/youtube/v3/docs/videos).
- **Weak ranking signal** per Backlinko's 1.3M-video study. YouTube
  itself has stated tags play a "minimal role" in discovery.
  [Backlinko ranking study](https://backlinko.com/youtube-ranking-factors),
  [Backlinko tags hub](https://backlinko.com/hub/youtube/tags).
- **Where tags still earn their keep**: catching keyword misspellings,
  language variants, and disambiguation (the first tag matters most —
  YouTube weights position).
  [Backlinko tags hub](https://backlinko.com/hub/youtube/tags),
  [LaunchLens 2025 tag guide](https://launchlens.tech/blog/youtube-tag-optimization-guide-2025).

### Recommended

- **5-10 tags max.** First tag = exact-match primary keyword
  (whatever phrase the title is hooked around). Next 2-3 = synonyms /
  spelling variants. Remaining = channel name, format name
  ("YouTube Shorts"), and the niche.
  [Backlinko tags hub](https://backlinko.com/hub/youtube/tags),
  [LaunchLens 2025 tag guide](https://launchlens.tech/blog/youtube-tag-optimization-guide-2025).
- **Don't pad to 30 tags.** Diluted relevance hurts more than empty
  slots help.
  [Brookeseidel YouTube tags 2025](https://brookeseidel.com/2025/10/18/youtube-tags-explained-do-they-still-matter-in-2025/).
- **Don't include hashtags as tags or vice versa** — they're separate
  systems with separate limits.
  [Hashtag Tools tags vs hashtags 2026](https://hashtagtools.io/blog/youtube-hashtags-shorts-seo-guide-2026).

---

## Category, language, made-for-kids

### Category ID mapping

Source: [Mixed Analytics 2025 category-ID list](https://mixedanalytics.com/blog/list-of-youtube-video-category-ids/),
[YouTube Data API videoCategories.list](https://developers.google.com/youtube/v3/docs/videos).

| ID | Name | Assignable | Best fit |
|---|---|---|---|
| 1 | Film & Animation | Yes | animated mythology Shorts, animated history shorts |
| 10 | Music | Yes | rhymetimejunction |
| 17 | Sports | Yes | sportsrecapped |
| 20 | Gaming | Yes | n/a |
| 22 | People & Blogs | Yes | catch-all storytime fallback |
| 23 | Comedy | Yes | comedy storytime variants |
| 24 | Entertainment | Yes | broad; only use if no better fit |
| 25 | News & Politics | Yes | avoid (monetisation risk) |
| 26 | Howto & Style | Yes | tutorials |
| 27 | Education | Yes | cosmosdecoded, historyrecapped explainers |
| 28 | Science & Technology | Yes | cosmosdecoded alt |
| 29 | Nonprofits & Activism | Yes | n/a |

IDs 30-44 are non-assignable (YouTube-internal: Movies, Anime,
Documentary, Horror, etc. — cannot be selected by creators).
[Mixed Analytics list](https://mixedanalytics.com/blog/list-of-youtube-video-category-ids/).

**Choice rule**: pick the *most specific* assignable category. Don't
default to 24 (Entertainment) — it dumps the video into the broadest,
most-competed recommendation pool.
[Inside Créateurs 2026 category guide](https://insidecreateurs.com/en/blog/tutorials/youtube-categories-2026-complete-list-which-to-choose).

### Language fields

- `defaultLanguage` = language of title + description text.
- `defaultAudioLanguage` = language spoken in the audio track.
- Both use BCP-47 codes (e.g., `en`, `hi`, `en-US`, `hi-IN`). Use
  `youtube.i18nLanguages.list` for the authoritative supported list.
  [YouTube Data API i18nLanguages](https://developers.google.com/youtube/v3/docs/i18nLanguages/list).
- **Setting these matters for Hindi content**: without
  `defaultAudioLanguage=hi`, the algorithm under-indexes Hindi Shorts
  for Hindi-language recommendations and may surface them to English
  audiences instead.
  [YouTube Data API videos](https://developers.google.com/youtube/v3/docs/videos).

### Made-for-kids

- **Required FTC/COPPA designation.** Set on every upload.
  [YouTube Help made-for-kids](https://support.google.com/youtube/answer/9528076?hl=en).
- **Setting `madeForKids=true` disables**: personalised ads (~50%+
  revenue cut), comments, notifications, info cards, end screens,
  Super Chat, channel memberships, and ALSO limits recommendation
  reach to other kids' content.
  [YouTube Help made-for-kids](https://support.google.com/youtube/answer/9528076?hl=en),
  [VidIQ COPPA explainer](https://vidiq.com/blog/post/coppa-kids-content-youtube/).
- **For ytFactory**:
  - `rhymetimejunction` → **YES**, made for kids (nursery rhymes
    primary audience is preschool).
  - Every other channel → **NO**. AITA, mythology, sports, cosmos,
    history are general-audience even when animated.
  - **Misclassification risk**: FTC can levy up to ~$42,000 per
    mislabelled video.
    [VidIQ COPPA explainer](https://vidiq.com/blog/post/coppa-kids-content-youtube/).

---

## Thumbnail

### Default behaviour

- Thumbnails are **invisible inside the Shorts swipe feed** — the
  first frame of the video is what plays. Thumbnails appear in:
  search, channel page Shorts tab, subscriptions feed, homepage
  recommendations, Google search Shorts carousels.
  [vidIQ Shorts thumbnail guide](https://vidiq.com/blog/post/youtube-shorts-custom-thumbnails/).
- **vidIQ's own data: only 13% of Shorts views come from the Shorts
  feed** — meaning 87% come from surfaces where the thumbnail IS
  visible. The "thumbnails don't matter for Shorts" trope is wrong.
  [vidIQ Shorts thumbnail guide](https://vidiq.com/blog/post/youtube-shorts-custom-thumbnails/).

### When to override the first frame

- Whenever the first frame is text-heavy, mid-blink, or otherwise
  unreadable at small sizes. Pick a frame with a face / clean subject /
  high-contrast hook word.
  [vidIQ Shorts thumbnail guide](https://vidiq.com/blog/post/youtube-shorts-custom-thumbnails/).

### Upload constraints

- **9:16 aspect ratio, 1080 × 1920 pixels.**
- **Custom-thumbnail upload is MOBILE-ONLY** for Shorts (desktop only
  allows selecting an existing video frame). Workaround: bake the
  thumbnail graphic into the video as the very first frame, then pick
  that frame.
  [vidIQ Shorts thumbnail guide](https://vidiq.com/blog/post/youtube-shorts-custom-thumbnails/),
  [Capcut thumbnail size guide](https://www.capcut.com/resource/youtube-short-thumbnail).

---

## Publish timing

### Best windows

[Buffer 1.8M-Shorts analysis](https://buffer.com/resources/best-time-to-post-on-youtube/):

| Day | Best hours (local audience time) |
|---|---|
| Sunday | 5pm, 7pm, 8pm |
| Monday | 5pm, 6pm, 8pm |
| Tuesday | 7pm, 8pm, 9pm |
| Wednesday | 7pm, 8pm, 9pm |
| Thursday | 7pm, 8pm, 9pm |
| Friday | 4pm, 6pm, 7pm |
| Saturday | 11am, 6pm, 7pm |

Buffer's mathematical adjustment means these times are universal for
the *target audience's* local timezone — you do NOT convert.

**Best days of the week**: Friday (top), Saturday, Thursday.
Weakest: Monday, Tuesday morning.
[Buffer 1.8M-Shorts analysis](https://buffer.com/resources/best-time-to-post-on-youtube/).

### Timezone strategy

- For **English-language Shorts targeting US**: post in US-Eastern
  local-time windows above. EST evenings is where the biggest viewer
  pool sits.
- For **Hindi-language Shorts targeting India**: IST evenings 7-10pm.
- For **global English**: 7-9pm US-Eastern is a decent compromise,
  since it catches US prime time and UK late-night.

[SocialPilot best-times-to-post 2026](https://www.socialpilot.co/insights/best-time-to-post-on-youtube),
[Sprout Social best-times-to-post 2025](https://sproutsocial.com/insights/best-times-to-post-on-youtube/).

### Scheduled vs immediate

- **No algorithmic penalty for scheduled vs immediate publishing.**
  The algorithm evaluates a video identically either way; what matters
  is *when* the publish actually happens.
  [Versacreative Shorts algorithm 2025](https://versacreative.com/blog/how-the-youtube-shorts-algorithm-works-in-2025/).
- **The first 1-3 hours after publish are the critical signal
  window** — YouTube tests the Short on a seed audience and uses
  early CTR + retention to decide on broader push. Posting at a peak
  window can lift first-24-hour views by 40-60%.
  [PostEverywhere posting-time data](https://posteverywhere.ai/blog/best-time-to-schedule-youtube-shorts).
- **Recommendation**: schedule rather than publish at random times.
  Schedule to land in the channel's audience-local peak window from
  the Buffer table above.

---

## Per-channel apply table for ytFactory

The downstream publish-metadata generator should treat this as the
machine-readable source of truth.

### `mystoriesanimated` (AITA / TIFU / wiki / TIH)

- **Title template** (≤60 chars):
  - AITA: `AITA for {topic}? Reddit story #shorts` — leave `#shorts`
    OFF the title; put it in description hashtags instead (cleaner,
    same SEO).
  - Final: `AITA for {topic}? 😬` (≤60c, with emoji)
  - TIFU: `TIFU by {topic} | reddit storytime` (≤60c)
  - Wiki oddities: `The {topic} that history forgot 🤯` (≤60c)
  - TIH: `On this day: {topic} ({year}) 📜` (≤60c)
- **Description first-line template**:
  - AITA: `{first_two_lines_of_reddit_post} … was I really the
    asshole? #AITA #redditstories #Shorts`
  - TIFU: `{first_two_lines_of_reddit_post} … #TIFU #redditstories #Shorts`
  - Wiki: `{one_sentence_lede}. Most people have never heard this story.
    #history #didyouknow #Shorts`
- **Hashtags** (in description, in order — first 3 pin above title):
  - AITA: `#AITA #redditstories #Shorts #storytime #reddit`
  - TIFU: `#TIFU #redditstories #Shorts #storytime #funnystories`
  - Wiki/TIH: `#history #didyouknow #Shorts #facts #todayinhistory`
- **Tags** (≤500 chars combined, first tag = exact title hook):
  `aita, reddit stories, am i the asshole, reddit storytime, aita reddit, mystoriesanimated, youtube shorts, reddit aita`
- **`category_id`**: `22` (People & Blogs) for AITA/TIFU;
  `27` (Education) for Wiki/TIH.
- **`defaultLanguage` / `defaultAudioLanguage`**: `en` / `en`.
- **`madeForKids`**: `false`.
- **Notes**: AITA hits best with conflict-question titles; never put
  the resolution in the title (kills the loop). TIH benefits from the
  date in the description first line for date-anchored search.

### `hindutavaanimated` (Hindi mythology)

- **Title template** (≤60 chars, Devanagari + 1 emoji optional):
  - `{character} ने जब {action} किया 🚩` (Devanagari)
  - English fallback for cross-discovery: `When {character} {action_in_english}`
- **Description first-line template**:
  - `{one_sentence_setup_in_Hindi}. यह कहानी जानिए। #mahabharat #ramayan #Shorts`
- **Hashtags**:
  - Mahabharat episodes: `#mahabharat #krishna #Shorts #hindumythology #arjuna`
  - Ramayan episodes: `#ramayan #ram #Shorts #hindumythology #hanuman`
  - Generic puraan: `#hindumythology #puran #Shorts #sanatandharma #mythology`
- **Tags**:
  `mahabharat, ramayan, hindu mythology, krishna, hindi mythology shorts, hindutavaanimated, pauraanik kathayein, mythology shorts hindi`
- **`category_id`**: `1` (Film & Animation) — animated stills + Hindi
  narration fits Film & Animation better than Education.
- **`defaultLanguage` / `defaultAudioLanguage`**: `hi` / `hi`.
  Setting `hi` is **critical** — without it, Shorts get recommended to
  English audiences who swipe past, tanking retention metrics.
  [YouTube Data API i18nLanguages](https://developers.google.com/youtube/v3/docs/i18nLanguages/list).
- **`madeForKids`**: `false`.
- **Notes**: Use Devanagari in title (NOT romanised Hindi). The Hindi
  mythology Shorts audience searches in Devanagari and the algorithm
  indexes the script directly when `defaultLanguage=hi` is set.

### `sportsrecapped` (sports Shorts + docs)

- **Title template** (≤60 chars, entity-led):
  - Head-to-head: `{player_A} vs {player_B} — {hook_stat}` (≤60c)
  - Match recap: `The day {entity} {verb} {opponent}` (≤60c)
  - Player profile: `{player}'s {year} season in {duration}s 🔥`
- **Description first-line template**:
  - `{stat_or_quote_from_video}. {entity_a} vs {entity_b}, {year}.
    #football #{entity_a_handle} #Shorts`
- **Hashtags**: entity-led, NOT generic. Example for a Ronaldo–Messi
  Short: `#ronaldo #messi #Shorts #football #footballshorts`. Replace
  per video.
- **Tags**:
  `{player_a}, {player_b}, {team_a}, {team_b}, football shorts, soccer shorts, sportsrecapped, {league}`
- **`category_id`**: `17` (Sports).
- **`defaultLanguage` / `defaultAudioLanguage`**: `en` / `en`.
- **`madeForKids`**: `false`.
- **Notes**: Sports algorithm is entity-graph-driven. Entity tags
  (player + team) carry more weight than generic `#football`. Do NOT
  use `#viral` / `#trending` — they don't index for sports and YouTube
  may flag them as irrelevant.
  [YouTube Help on hashtags](https://support.google.com/youtube/answer/6390658?hl=en).

### `cosmosdecoded` (physics + space)

- **Title template** (≤60 chars, "How we knew" channel native):
  - `How we knew {phenomenon}` (≤60c)
  - `The day we proved {phenomenon}` (≤60c)
  - `Why {phenomenon} shouldn't exist 🌌` (≤60c)
- **Description first-line template**:
  - `In {year}, {scientist_or_mission} showed that {one_line_finding}.
    Here's how. #space #physics #Shorts`
- **Hashtags**: `#space #physics #Shorts #astronomy #nasa`
  (swap `#nasa` for `#cern` / `#ligo` / `#esa` per topic).
- **Tags**:
  `physics, astronomy, space shorts, {phenomenon}, {mission_or_scientist}, cosmosdecoded, how we knew, science shorts`
- **`category_id`**: `28` (Science & Technology) — preferred over `27`
  (Education) for physics+space; both work, 28 places the Short in a
  more focused recommendation pool.
- **`defaultLanguage` / `defaultAudioLanguage`**: `en` / `en`.
- **`madeForKids`**: `false`.
- **Notes**: The "How we knew" framing is the channel's hook — keep it
  in the title for series recognition (binge-watch signal). Cite
  agencies (NASA / LIGO / CERN) by name in the description first line —
  the algorithm's entity graph recognises them.

### `historyrecapped` (history Shorts + long-form)

- **Title template** (≤60 chars):
  - Date-anchored: `In {year}, {event_one_line}` (≤60c)
  - "The day…": `The day {event}` (≤60c)
  - Number-led: `In {N} seconds, {event_one_line} 📜` (≤60c)
- **Description first-line template**:
  - `On {date}, {one_sentence_event}. The rest of the story is wilder
    than the textbooks. #history #ancienthistory #Shorts`
- **Hashtags**: `#history #ancienthistory #Shorts #historyfacts #worldhistory`
  (swap third niche per topic — `#ww2`, `#ancientrome`, `#vikinghistory`, etc.).
- **Tags**:
  `history shorts, {event}, {year} history, ancient history, world history, historyrecapped, history facts, {era}`
- **`category_id`**: `27` (Education) for explainer-tone Shorts;
  `24` (Entertainment) only if the Short leans dramatic / cinematic.
- **`defaultLanguage` / `defaultAudioLanguage`**: `en` / `en`.
- **`madeForKids`**: `false`.
- **Notes**: Date anchors in title and description both serve search
  (people search by year/decade). For animated Shorts in this channel
  (illustrated panel renders), category `1` (Film & Animation) is also
  acceptable but `27` retains the educational recommendation cluster
  better.

---

## What's UNVERIFIED

Research gaps where the public record is silent, contradictory, or
laundered through analyst aggregators without a primary YouTube data
source:

1. **Per-vertical algorithmic timing**. Buffer's 1.8M-Shorts study
   gives universal best times but explicitly states "no
   vertical-specific breakdowns" — there is no public dataset
   isolating the optimal post-time for mythology vs sports vs
   cosmos. The per-vertical timezone heuristics above are inferred
   from audience-location assumptions, not measured.
   [Buffer 1.8M-Shorts analysis](https://buffer.com/resources/best-time-to-post-on-youtube/).
2. **Exact algorithmic weight of `defaultLanguage` vs spoken-audio
   detection**. YouTube confirms both fields exist but does not
   publish the relative ranking weight, and YouTube's audio language
   detection (ASR-based) can override or supplement the declared
   field. The Hindi recommendation impact above is based on creator
   testimonials and the API documentation, not measured A/B data.
3. **Whether the `#Shorts` hashtag still moves the needle at all in
   2026.** The pre-2024 "always include #Shorts" advice predates
   YouTube's automatic 9:16/sub-60s classification. Public analyses
   range from "essential" to "redundant noise". No primary YouTube
   statement post-2024 confirms either way.
   [Miraflow 2026 analysis](https://miraflow.ai/blog/youtube-shorts-hashtags-2026-do-they-still-matter).
4. **Whether emojis in title harm long-form crossover when a channel
   posts both formats**. vidIQ's 128M-video study shows
   emoji-positive for Shorts and emoji-negative for long-form, but
   doesn't address whether a *channel that mixes both* takes a
   penalty on either side from inconsistent emoji usage.
   [vidIQ emoji study](https://vidiq.com/blog/post/emojis-in-youtube-titles/).
5. **The 60-hashtag absolute cap behaviour**. YouTube Help states "we
   ignore each hashtag" at >60, but multiple analyst sources state
   the cap kicks in at 15 hashtags — there are conflicting reports
   on whether the limit is 15 (hashtags ignored), 60 (video
   removed), or both stages compound. The conservative read is to
   stay at ≤5 and never test the limit.
   [YouTube Help](https://support.google.com/youtube/answer/6390658?hl=en).
6. **Whether scheduled publishing has any first-hour-burst penalty
   relative to immediate publish**. Multiple sources state "no
   penalty", but none cite a YouTube engineering statement; this is
   an analyst consensus.
7. **Backlinko's zero-correlation finding for descriptions** comes
   from a long-form video dataset, not a Shorts-specific one. It is
   plausible that descriptions matter MORE for Shorts (where the
   first 100 chars are the only player-visible text) — but no public
   study has isolated Shorts.
   [Backlinko study](https://backlinko.com/youtube-ranking-factors).
8. **Custom thumbnail impact on search-tab CTR for Shorts
   specifically**. vidIQ's data on the 13% feed / 87% non-feed split
   is sourced from their own tool's user base, which may not be
   representative of all creators.
   [vidIQ Shorts thumbnail guide](https://vidiq.com/blog/post/youtube-shorts-custom-thumbnails/).
9. **The TunePocket "10K trending Shorts" dataset** is the cleanest
   public title-length analysis but is gated behind a third-party
   tool with no methodology paper. The 51-char median and 8-word
   median are widely re-cited but never independently reproduced.
   [TunePocket analysis](https://www.tunepocket.com/youtube-shorts-video-titles/).
10. **Whether `defaultAudioLanguage=hi` actually changes recommendation
    distribution for Hindi Shorts** — there is no public A/B
    confirmation. The above guidance assumes it does because the field
    is documented as a recommendation signal.
    [YouTube Data API videos](https://developers.google.com/youtube/v3/docs/videos).
