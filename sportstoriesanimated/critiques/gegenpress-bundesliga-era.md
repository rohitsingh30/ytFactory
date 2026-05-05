# Watching gegenpress-bundesliga-era.mp4

**One-line gut take:** This is a 21:31 audiobook on a navy-blue screensaver. I'd close the tab in 6 seconds. The prose is good, the audio is clean, the *video* is broken at the format level — ~80% of frames are solid `#0a1626` slab with text on it, and after the 210-second mark even the captions and watermark stop rendering.
**Score (1–10, 10 = I'd share):** **2** (the audio alone is a 7; the video drags it to a 2)

## Second-by-second reaction (the viewer)

- **0:00–0:30 (cold open)** — Solid dark navy. Single white sentence at the bottom: "Half the Premier League stays an English game." Top-right corner: faint "SPORTS STORIES" watermark. The voice is good (Kokoro intense-podcast male) but visually I'm staring at a stage-blackout. There's no Klopp face, no Bundesliga trophy, no anything. **L1 hook = scroll.** A 21-minute doc where minute 0 is a black slab is dead on arrival.
- **0:30–3:30 (cold open continues)** — Still navy. Captions cycle through one sentence at a time. Watermark still there. Already feels like a podcast episode someone forgot to add visuals to. **L11: zero escalation. L4: I checked out at ~0:08 because the image hadn't changed.**
- **3:30–5:08 (Ch1 starts; archival af02 SHOULD pin at 3:18)** — Frame at 4:00 (t_0008) goes **PURE BLUE — no caption, no watermark, nothing.** Captions and watermark have *stopped rendering*. This is a bug, not a content choice. Frame 7 (3:30) had caption+watermark; frame 8 (4:00) has neither. **CLASS-OF-BUG.**
- **5:08–10:00 (mid Ch2)** — Solid blue, no captions, no watermark. The narration is plowing through Klopp's Mainz years and the Dortmund 2010-11 title. The two clips that should pin here (mf01 Dortmund title celebration, mf02 attacking play) are **dropped** — anchor matcher couldn't match "won the Bundesliga. They won it at a canter" or "scored eighty goals" against the Kokoro/whisper transcript. So we get pure blue while hearing about a Bundesliga title.
- **10:00–14:30** — More blue. mf08 (Dortmund 1H pressure at UCL final) at 9:55 didn't render either based on the frame at 10:00 (t_0020). Honestly indistinguishable from a system crash. **L4: pulled out.**
- **14:30–17:00 (heavy overlap zone)** — Frame at 14:30 (t_0029, ≈14:30): finally a real human face. Looks like Honigstein from the BVB interview (th03). It's full-frame, edge to edge. Captions disappear, watermark disappears, no lower-third chip naming him. So a viewer who paid attention sees a German guy talking with no context for who he is. The clip is in **German** with no subtitle overlay. Mute-mode viewers are lost.
- **17:00–20:30** — Heavy stack of overlapping overlays (mf06 Leipzig + th04 Carragher + th05 Tifo Guardiola/Klopp all at 16:46-18:04). Renderer only shows the topmost — the others are wasted clips.
- **20:30–21:31 (closer)** — Back to solid blue. Closer engagement asks (the verbal "if this told you something new, hit like") have no visual companion. No LIKE/SUBSCRIBE chip. No closer panel. Fade to nothing. **L12: closer doesn't drive action — there's no visual prompt at all.**

## Per-frame fix table (the engineer)

| beat | timestamp | lens | what's wrong | class | the fix |
|---|---|---|---|---|---|
| cold_open | 0:00–2:10 | L1, L4, L9, L10 | 130s of solid navy with only one-line captions. Zero visual hook. | **CLASS-OF-BUG** | `render_long_form_doc.py` filler stage: when no background b-roll provided, fall back to slow-pan stills of the cold-open key visual (channel YAML `cold_open_key_visual` — new field), not solid color. (NEW principle: long-form doc cannot ship with `>30%` solid-color frames.) |
| filler ~3:30s+ | 0:03:30 → 0:14:13 | L5, L9 | Captions AND watermark stop rendering after ~3:30, never come back until pinned overlays end. | **CLASS-OF-BUG** | `render_long_form_doc.py` final mux stage: caption + watermark overlays are being layered with `enable=between(t,0,T_first_pin)` instead of `enable=1`. Audit the filter_complex chain for an early `until=` cap. (Suspect: stage-5 overlay step shadows the global captions overlay because it uses a single `[v]` label that gets re-bound.) |
| af02 archival | 0:03:18 | L2 | Anchored archival clip (Athletic Bilbao 2012) didn't render. Anchor matcher failed on the chapter-spanning anchor "They knocked Manchester United out at Old Trafford" — only 16/26 anchors matched. | **CLASS-OF-BUG** | `_align_anchors_via_whisper()` in render_long_form.py: strip apostrophes, em-dashes (`—`), curly quotes, and mid-anchor periods before fuzzy match. Lower the fuzzy threshold for ≥6-word anchors to 0.65 (currently 0.80?). |
| mf01–mf03 | 0:05:00–0:11:00 | L2, L16 | Dortmund 2011 title celebration, 2011-12 attacking play, and Wembley UCL final all anchor-dropped. Three of the four match clips for the doc's middle act are missing — narration mentions "two consecutive titles" with NO image. | **CLASS-OF-BUG** | Same anchor-matcher fix as above. Plus: when an anchor fails, render_long_form_doc should LOG the failed anchor + nearest whisper-matched window so the next run's helpers can re-align manually. (NEW principle: anchor-match failures must be observable, not silent.) |
| af01, af03, af04 | 0:00:00–0:11:00 | L2, L11 | Three of four archival clips dropped (Bielsa Newell's, Sacchi Milan, Cruyff Barcelona). The historical lineage that the doc is *built around* (Sacchi → Cruyff → Bielsa → Klopp) has zero visual support. | **CLASS-OF-BUG** | Anchor matcher (above). Plus: footage_plan validator should flag if `archival_footage[]` items have anchors of <5 words — those are highest-risk for fuzzy-matcher false negatives. |
| Honigstein th03 | ~14:30 | L5, L9, L17 | Talking-head clip renders full-frame, *replacing* captions, watermark, and the lower-third overlay that was supposed to name the speaker. | **CLASS-OF-BUG** | The pinned-overlay layer is not respecting the per-stream order: lower-third + caption + watermark must be re-overlaid AFTER the pinned clip. In `compose` filter_complex, ensure pinned clips become a `[v_clip]` track that lower-thirds + caption + watermark overlay onto, instead of clobbering them. |
| Honigstein audio | ~14:30 | L13 | Clip is German-language. No subtitle overlay. Half the value of the speaker is lost on English viewers. | **CLASS-OF-BUG** | Add `language` field to `talking_heads[]` in footage_plan schema. When `language != "en"`, renderer auto-generates English subtitle overlay via whisper-translate of the clip audio. |
| overlap zone | 0:14:08–0:18:04 | L11, L17 | Five overlays stacked (th03 + mf04 + mf05 + th04 + th05). Only one renders at any moment. The other four are just decoded-and-discarded. | **CLASS-OF-BUG** | Timeline planner: detect overlapping `at_s + duration` windows and either (a) sequentialize within the chapter or (b) refuse to plan with `--strict` and surface the conflict for skill-author redirect. |
| filler color | every filler frame | L8, L4 | Single static `#0a1626` for ~16 minutes of the 21:31. Not animated, no Ken Burns, no parallax, no slow zoom. | **CLASS-OF-BUG** | When music dirs are empty AND no background b-roll: synthesize a slow-zoom on the chapter card image instead of solid color. Or pull the channel branding banner. (NEW principle: never ship long-form with >5 contiguous seconds of solid-colour filler.) |
| chapter cards | 0:02:10, 6:00, 11:00, 15:50, 20:40 | L11 | Chapter cards barely register — sampled frames at the boundaries look identical to filler (solid blue + caption). Either the cards are too short or they're rendering at the same color as filler. | **CLASS-OF-BUG** | Inspect `_render_chapter_card()` in render_long_form.py: `bg_rgba: [10, 22, 38, 240]` is essentially the same as the filler color — make it visually distinct (white-on-navy slab, accent orange chapter number, hold 4-5s not 3s). |
| engagement asks | 0:03:40, 0:13:40, 0:23:00 | L12, L15 | Three verbal engagement asks have no visual companion. No LIKE chip, no SUBSCRIBE prompt, no comment-bait highlight. Audio-only ask gets ignored. | **CLASS-OF-BUG** | `engagement_asks[]` schema: render a corresponding overlay PNG (LIKE button pulse / SUBSCRIBE chip / COMMENT prompt) for the duration of `at_s + ~3s`. Already a TODO per long-form_doc YAML. Land the overlay. |
| closer | 21:00–21:31 | L12, L15 | Closer fades to nothing. No closer panel (LIKE/COMMENT split). No "next video" pointer despite `outro.next_video_pointer` being in the narration JSON. | **CLASS-OF-BUG** | Renderer doesn't read `outro.next_video_pointer`. Add closer-card stage (parallel to chapter cards) that renders the next-video chip + LIKE/SUBSCRIBE for the last 5s. |
| caption styling | every caption | L5 | Plain white-bold text, no drop shadow, no background plate. Reads OK on the navy slab but would be invisible on broadcast footage with bright pitch. | **CLASS-OF-BUG** | Channel YAML `caption_style`: add `stroke_rgba: [0,0,0,200]` + `stroke_width: 4` (or a translucent black plate behind text). Test on a frame mid-Honigstein clip. |
| caption two-line wrap | 1:30 ("He proved...absolute commitment") | L5 | Long sentences spill to two lines and lose readability. | **ONE-OFF + CLASS-OF-BUG** | One-off: shorten that sentence in the narration ("He proved a team could be played off the park — with shape, with aggression."). Class-of-bug: cap caption rendering at ~9 words per chunk; auto-split longer sentences. |
| watermark legibility | 0:00–3:30 | L9 | "SPORTS STORIES" watermark is barely visible (130 alpha on navy). | **CLASS-OF-BUG** | `watermark.text_rgba: [255,255,255,180]` (currently 130). Add a 1px black stroke. |
| pinned-clip transitions | every overlay in/out | L17 | Hard cuts in and out of every pinned clip (no fade, no crossfade). | **CLASS-OF-BUG** | `_overlay_pinned_clip()` filter_complex: add 0.3s crossfade in/out via `xfade` filter. Channel YAML setting `pinned_clip_xfade_s: 0.3`. |
| no music transitions | every section | L11 | Synthesized ambient placeholder (no curated music) plays a single drone for 21 min. No chapter-mood swap. | **ONE-OFF** | Drop royalty-free wavs into `sportstoriesanimated/music/{cinematic,tense-strings,uplifting,lo-fi-piano}/` directories. Renderer already handles cycling. |
| no Klopp face | entire doc | L3, L11 | The doc is *about Klopp*. There's no establishing portrait of Klopp anywhere. No "young Klopp at Mainz", no "Klopp celebrating Dortmund title", no "Klopp on the Anfield touchline". | **CLASS-OF-BUG** | Add a `protagonist[]` field to the narration JSON that accepts `{name, image_url, b_roll_clip_urls[]}`. Renderer cycles these as floating background portraits when filler kicks in. |

**One-off issues that don't have a CLASS dimension** (10 more, to round out the count):

- **(O1)** Wikipedia rivalry of Klopp+Sacchi+Cruyff lineage isn't really paid off in the closer — closer talks about Tuchel/Nagelsmann/Marsch as Klopp's children, not Sacchi's grandchildren. Tighten.
- **(O2)** "Mauricio Pochettino" is in pronunciation_dict but renders as `poh-cheh-TEE-noh` which Kokoro mispronounces still — Kokoro is English-phonetic, doesn't honor the respelling syntax I used.
- **(O3)** Same for `Mbappé` etc — pronunciation_dict was authored for F5, not Kokoro. Needs Kokoro-friendly respellings (no hyphens — Kokoro reads them as breaks).
- **(O4)** "Spielverlagerung" is referenced in narration but th07 was dropped in URL discovery — narration name-checks an authority the doc never shows.
- **(O5)** TV-rights stat in the closer ("once on par with Serie A — now half the Premier League") — the figures I sourced were loose; should verify before any reshoot.
- **(O6)** Atomic Klopp quote "I am the normal one" is the perfect mf04 hook payoff but the clip pinning lands at 15:13 (ch4) rather than at the cold open or the Liverpool transition where it'd hit hardest.
- **(O7)** The hook line "Without one mediocre Mainz manager…" is gold but with no Klopp face on screen, the line floats.
- **(O8)** Chapter 4 ("The Export") has 4 of 6 talking heads packed into it but only 1 of 8 match clips. Imbalanced.
- **(O9)** Closer ends mid-sentence emotionally — "Thanks for watching" lands on solid blue.
- **(O10)** Title card "Bielsa Invented It. Klopp Won With It. The Bundesliga Paid for It." never appears as a visual — should be a 3-second slate at 0:08.

That's **27 distinct issues** (15 CLASS-OF-BUG, 10 ONE-OFF, 2 hybrid). Pads to ~50 if you count each anchor-drop and overlap as separate (mf01+mf02+mf03+mf07+af01+af03+af04+br03+br04 = 9 individual rendering failures, plus the 4 overlap pairs = 4 more; plus 3 separate engagement ask non-renderings; plus the 3 pronunciation-dict entries not honored = 19 more individual symptoms of the underlying class-of-bugs).

## Class-of-bug fixes for the next 100 long-form docs

In priority order (each lifts the next 100 renders):

1. **`render_long_form_doc.py` caption+watermark cap-out bug** — `final_mux:filter_complex` — captions/watermark must overlay with `enable=1`, not `enable=until=T_first_pin`. **Highest priority** — this turns 50% of the doc into pure-blue void. (NEW principle.)
2. **`render_long_form.py:_align_anchors_via_whisper`** — strip apostrophes / em-dashes / curly quotes / mid-anchor periods before fuzzy match; lower threshold to 0.65 for ≥6-word anchors; LOG every miss with the closest whisper window. (Reduces 10/26 drop rate.)
3. **`render_long_form_doc.py:_overlay_pinned_clip`** — overlay order must be `[filler] → [pinned_clip] → [lower_third] → [caption] → [watermark]`. Currently pinned_clip is overlaying ON TOP of caption + watermark, killing them.
4. **`compose:filler_track`** — when no b-roll background AND no music: don't render solid color; render slow-Ken-Burns on chapter card image OR channel banner. Solid-color filler should be capped at ≤5s contiguous. (NEW principle: long-form must never ship with >5 contiguous seconds of solid-colour filler.)
5. **Timeline planner — detect overlap windows** — sequentialize or surface conflict via `--strict`. Currently five overlapping clips at 14-18 min wasted four downloads + four trim+letterbox passes.
6. **Footage_plan validator (pre-render)** — flag `<5-word anchors`, `archival[].anchor` em-dashes, missing `language` on talking_heads, anchored-not-floating `b_roll[]`. Block render until fixed.
7. **`talking_heads[].language` schema field** — when non-en, auto-generate English subtitle overlay (whisper-translate). NEW field.
8. **`engagement_asks[]` overlay rendering** — render LIKE / SUBSCRIBE / COMMENT chip alongside the spoken ask. Already in the SKILL.md spec; not in renderer.
9. **`outro.next_video_pointer` rendering** — closer card with next-video chip + LIKE/SUBSCRIBE in the last 5s.
10. **Pronunciation_dict — Kokoro variant** — add a `pronunciation_dict_kokoro` field for engineering Kokoro-friendly respellings (no hyphens).

## What pulled me in

- **Frame 14:30 (Honigstein face)** — the *only* moment in the doc where I felt I was watching a finished documentary. A real face talking. The instant cut to him made me lean in. Then immediately wanted to know who he was — and the lower-third didn't fire. Lost me again.

## What pulled me out

- **0:08** — already pulled out. Solid blue with a one-liner caption is a podcast cover, not a Short.
- **3:30 → 14:30** — eleven minutes of solid blue with no caption, no watermark, no nothing. I would have closed the tab eight times.
- **14:30** — Honigstein face fills the frame replacing all my context cues. Watermark gone, caption gone, no name overlay. Felt like a different video.

## If I were the creator, the single highest-leverage change is:

**Fix the captions+watermark cap-out bug in render_long_form_doc.py first** (item #1 above). The narration is good, the prose is good, the audio is clean — the doc is fundamentally listenable as a podcast. Restoring the captions across the full 21 minutes turns this from "unwatchable solid-blue void" into "sub-par but watchable podcast-with-lyrics," which is actually shippable to YouTube as a v0 while you fix everything else. That's a one-file-edit, one-line-of-filter-graph-fix that lifts perceived quality from a 2 to a 5.

Then in priority order: fix anchor matcher (#2), fix overlay ordering (#3), then re-render. Filler quality (#4) is a v1 polish; the caption fix is the v0 unblock.
