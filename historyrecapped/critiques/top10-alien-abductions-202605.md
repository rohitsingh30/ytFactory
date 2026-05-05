# Watching top10-alien-abductions-202605.mp4

**One-line gut take**: I would scroll past this in under 2 seconds. The silhouettes on a hill don't tell me this is about alien abductions, the source channel's watermark is visible in multiple frames, the final four minutes are a frozen still of an AI-generated kid pointing at an alien, and at one point we are showing vintage home movies of a small child while the narration is talking about a man being abducted from a cabin.

**Score (1–10)**: **1/10** — user is right, this is unshippable.

---

## Second-by-second reaction (the viewer)

- **0.0–1.5s (the hook)** — Black-and-white silhouettes of five people walking up a hill at dusk, vignette + grain filter. No alien, no UFO, no rank card, no title. Could be a Cymbalta ad, a real-estate ad, or the cold open of a wellness documentary. The narration says "Ten people. Ten places." — which on a phone the user can't hear because they have it muted. **Lens L1, L9, L10 all flag immediately.**

- **5s** — Suddenly cut to a Google Earth view of Europe with the Google watermark visible in the bottom-right and a small "Apollo" label baked in. Why am I looking at Europe? The Hill case is in New Hampshire. Schirmer is in Nebraska. None of the ten cases are in Europe. **L2 flags hard.**

- **30s** — Beach campfire at night. Pretty. Has nothing to do with alien abductions. Generic moody b-roll.

- **60s** — Stylized silhouette of a person under stars with bokeh light orbs, blurry. This is the only frame in the first minute that even gestures at the theme. It is also blurry and out of focus.

- **1:45 (#10 Schirmer)** — Extreme close-up of cigarette butts in dirt. The narration is establishing a 22-year-old police sergeant on highway patrol in Nebraska in 1967. Why am I looking at trash on the ground.

- **3:00 (#10 Schirmer)** — Google Earth globe, **black censorship bar across part of the frame**, **"Apollo" text overlay**, **Google watermark visible in corner**. The black bar is from the source channel covering up something they didn't have rights to. We just inherited their workaround. **L6 + L8 flag.**

- **4:00 (#9 Buff Ledge)** — Cuts to a fake CRT-TV-frame UI containing a 1990s blonde woman in a talking-head interview. **Down the left edge of the frame, in legible text: "Credit: Strange But True S03E09".** We have just embedded a competing UK paranormal TV show's source attribution into our video. **L8 + L13 + ContentID risk = catastrophic.**

- **5:20 (#9)** — Same TV-frame UI, now with abstract red/green color blobs and an "UNEXPLAINED" channel watermark in the top-left. So now we have *two* sourceable watermarks visible: one to *Strange But True*, one to a YouTube channel called *Unexplained*.

- **7:40 (#8 Allagash)** — Same TV-frame UI continues, "Credit: Strange But True S03E09" still present.

- **10:00 (#7 Pascagoula)** — A vertical mugshot-style portrait of a bearded man in a striped shirt floats over an orange sunset background. This MIGHT be Calvin Parker, but with no caption I can't tell, and the narration is currently talking about Charles Hickson and the river pier.

- **12:30 (#6 Kelly-Hopkinsville)** — Out-of-focus stock photo of a depressed-looking man in a grey sweater on a couch, face obscured. The narration is describing **eleven witnesses firing shotguns at glowing-eyed creatures for four hours** and we are showing a stock-photo therapy ad.

- **15:00 (#5 Manhattan)** — Generic stylized face with fire/sparks composited over it. The narration is in the middle of describing a housewife being floated out of a 12th-floor window over the East River.

- **18:00 (#4 Strieber)** — **Vintage home-movie footage of a small child wearing only shorts, shaky 8mm camera**. The narration at this moment is describing Whitley Strieber being abducted from a cabin. The visual is inappropriate to the narration, and showing identifiable child footage during alien-abduction narration is the kind of thing a viewer screenshots and posts to social media to drag the channel.

- **20:40 (#3 Villas Boas)** — Stock shot of a young woman from behind looking out a bright window. Narration is describing a 23-year-old Brazilian farmer plowing a field at night.

- **23:00 (#2 Walton)** — Cuts to a static AI-generated image: a kid in a red baseball jacket pointing up at a tall grey alien standing next to him in a green hedge-maze landscape with two planets in the sky. **And then this same static frame holds for the next four full minutes through the end of the video.** Walton's climax (the 5-day disappearance, the polygraph passes), the entirety of #1 Betty and Barney Hill (the founding case, the star map, the Project Blue Book file), and the closer all play over this one frozen image.

- **26:58 (closer)** — Same static frame. Video ends.

---

## What pulled me in
Honestly, nothing. The narration script itself is solid (this critique is not about the writing). But the visuals never reinforce the writing, and at every moment a viewer would actually look at the screen, the screen is showing something unrelated, watermarked, censored, or static.

## What pulled me out
- **0.3s** — silhouettes don't assert the topic
- **5s** — Google Earth on screen
- **180s, 460s, 600s** — Strange But True / UNEXPLAINED watermarks
- **18:00** — vintage child footage during abduction narration
- **23:00 → end** — final 4 minutes are a frozen AI-generated thumbnail

---

## Per-frame fix table (the engineer)

| beat | timestamp | lens | what's wrong (concrete) | class | the fix |
|---|---|---|---|---|---|
| Hook | 0.0–4s | L1, L9, L10 | Generic silhouettes on hill — no UFO, no rank card, no title, no asserted topic. Mute viewer has zero idea this is an abductions video. | **CLASS-OF-BUG** | Add a mandatory **rank-card overlay** for every rank's first 2.5s — large title `#10 — THE SCHIRMER ABDUCTION · ASHLAND, NE · 1967` on a translucent panel. Implement in `render_footage_only.py` as a new `_burn_rank_cards` pass that reads `narrations/<slug>.json["ranks"][i].title_card_text` and `chapter_timestamps`. Mirrors the closer-panel render path that already exists for Shorts. |
| Hook | 0.0s | L10 | Frame 0 is silhouettes. Useless as a thumbnail. | **CLASS-OF-BUG** | Generate a deliberate `<slug>_thumb.jpg` from the narration JSON's `hook` + `topic` + a high-contrast grey-alien iconography composite (this is footage_only, so reuse a Wikimedia or stock high-impact still). Save next to the mp4 for upload-time selection. |
| All ranks | every | L2 | Visual content has zero correlation with what's being narrated — single 85-min source compilation cycled through. The shotlist's per-rank `match_text` is meaningless because the visuals it picks are random b-roll from one source. | **CLASS-OF-BUG** | This is the v1 wallpaper-mode shortcut I took. Real fix: the per-rank shotlist *plan* in the JSON (each rank's footage_queries with kind: archival/photo_pan/doc_cutting) needs to be *executed* with per-window source resolution. The renderer already supports per-window `source_url` (verified in `_resolve_asset`); the gap is that I didn't fill out 30+ verified per-rank URLs. Build `scripts/_shared/find_paranormal_footage.py` that, given a rank's `key_facts` + witness names + year, queries archive.org metadata API + Wikimedia Commons file API + Pexels API and produces a verified per-window URL list. |
| Multiple | 3:00, 4:00, 5:20, 7:40, 10:00 | L8, L13 + ContentID | Source channel watermarks visible: "Credit: Strange But True S03E09" (left edge), "UNEXPLAINED" (top-left), "Google" + "Apollo" + black censorship bar (Google Earth segments). | **CLASS-OF-BUG** | Two layers of fix: (a) Pre-render watermark scan — sample 5 random frames from the downloaded source via ffmpeg+ffprobe, OCR each with `pytesseract`, fail emit if any frame contains channel-attribution text matching `r"(?i)credit|episode|s\d+e\d+|copyright"`. (b) Post-trim watermark cropping — for sources that pass the OCR scan but still have corner watermarks, allow a `crop` field in the shotlist window to surgically remove a corner band. The current single-source wallpaper pattern is fundamentally unsafe for ContentID; the real fix is moving to per-rank archive.org PD and Wikimedia stills (see row above). |
| Closer | 23:00–26:58 | L4, L11, L12 | Final 4 minutes are a single static frame (the source compilation's endcard — kid pointing at alien on alien planet). Walton's climax + entire Hill rank + closer all play over a frozen thumbnail. Retention dies. | **CLASS-OF-BUG** | The shotlist's last window (`in_s: 2900, out_s: 3050`) lands inside the source's static endcard. The renderer needs a **silent-pad guard**: ffprobe each trimmed clip for visual variation (pixel diff between sampled frames); if `mean_pixel_diff < threshold` for >5s, treat as static and either replace the window with a different source segment or fail emit. Add to `_build_silent_video` after the trim step. |
| Mux | overall | L4 | `concat` re-encodes 35min of libx264 even though every input clip already shares output codec params. ~25min wall time on contention. | **ONE-OFF** for the renderer | Change concat to `-c copy -avoid_negative_ts make_zero` when all input clips share codec/pix_fmt/fps (which they do because the trim step writes uniform params). Add a probe + branch in `_build_silent_video`. Drops concat from 25min to ~30s. |
| All | every | L5, L9 | **No captions.** caption_mode defaulted to `"none"` because the renderer's logic is `"none" if aspect == "16:9" else "shorts"`. For Top-10 list long-form (where each rank has a title and key facts that ARE the visual story), captions are mandatory. | **CLASS-OF-BUG** | Add a third `caption_mode = "long_form"` that renders sentence-level yellow italic captions matching the existing `historyrecapped/long_form/` style (per `learnings/long_form_captions.md` 2026-05-04: "captions ARE on for long-form"). The default for 16:9 should be `"long_form"`, not `"none"`. `"none"` becomes opt-in via `kathaa: caption_mode: none`. |
| 18:00 | #4 Strieber | L13, brand-safety | Vintage home-movie footage of an identifiable small child plays during alien abduction narration. The frame is from the source compilation's b-roll — content-moderation flagged on at least three platforms. | **CLASS-OF-BUG** | Add a YOLO/MediaPipe person-detection pass in the pre-render watermark scan above; if any sampled frame contains a child (face under detected age threshold), fail emit. Defense-in-depth against single-source wallpaper-mode unintended content. |
| Per-rank | each rank start | L11, L12 | No "rank starting" visual cue. Viewer cannot tell when one case ends and the next begins. Listening alone, the only marker is the spoken word "Number ten…" — but the visuals never reinforce it. | **CLASS-OF-BUG** | Same fix as row 1 (mandatory rank cards). Additionally: insert a 0.5s blackout transition between ranks (driven from `chapter_timestamps`). |
| Per-rank | each rank | L6, L8 | Cuts between visual segments are jarring — vertical phone-portrait stock photos appear letterboxed into 16:9 awkwardly, then cut to landscape Google Earth, then cut to TV-frame UI, then back. Aspect is technically 16:9 but the source compilation mixes vertical/portrait/CRT-frame visuals. | **CLASS-OF-BUG** | Tied to the per-rank source resolution fix (row 3). Once each rank has its own coherent set of stills/clips, the within-rank visual style stays consistent. The blurred-letterbox path the renderer already has handles aspect mismatches gracefully when the input is a clean still — it's only ugly when the input is itself a styled compilation segment. |

---

## Class-of-bug fixes for the next 100 long-form Top-10s

Ranked by leverage — fixing #1 lifts every future render in this format; the rest are progressively narrower.

1. **Mandatory rank-card overlay** — `historyrecapped/scripts/render_footage_only.py:_burn_rank_cards` (NEW pass) — reads `narrations/<slug>.json["ranks"][i].title_card_text` and `chapter_timestamps`, burns a 2.5s panel at each rank boundary. The narration JSON already has all the data; no schema change. (principle: NEW — *every long-form list video must have rank chapter cards*; mirrors the closer-panel pattern in `compose.py`.)
2. **Caption mode default for 16:9 long-form** — `render_footage_only.py:render` line ~665 — change `"none" if aspect == "16:9" else "shorts"` to `"long_form" if aspect == "16:9" else "shorts"`, kathaa opts out via `cfg["kathaa"]["caption_mode"] = "none"`. Wire `caption_mode == "long_form"` to the existing sentence-level yellow-italic caption renderer in `render_long_form.py`. (principle: long_form_captions.md 2026-05-04.)
3. **Per-rank source resolution helper** — `scripts/_shared/find_paranormal_footage.py` (NEW) — function `find_per_rank_footage(rank: dict) -> list[ShotlistWindow]` queries archive.org metadata API + Wikimedia Commons File API + Pexels API given the rank's `key_facts`, returns a list of verified-downloadable URLs with kind tags. The /make-top10 skill calls this *before* writing the shotlist, so fictional URLs never reach disk. (principle: every URL in a shotlist must be verified-downloadable at author time, not at render time.)
4. **Pre-render watermark + child-detection scan** — `pipeline/footage.py:_safety_scan` (NEW) — for every downloaded source, sample N=5 random frames via ffmpeg, OCR each with `pytesseract`, run face-age heuristic via mediapipe; fail emit on watermark match (`r"credit|episode|s\d+e\d+|copyright"`) or under-18 face detection. Block the wallpaper-mode pattern from going to render unless the scan passes. (principle: NEW — *single-source wallpaper renders must pre-screen for embedded source attribution and unintended content*.)
5. **Static-frame guard at trim time** — `_build_silent_video:_check_window_variance` (NEW) — after each window trim, pixel-diff the first vs last second of the clip; if `mean_diff < threshold` (suggesting static endcard), warn or fail. Catches the "final 4 minutes are a frozen thumbnail" failure mode. (principle: NEW.)
6. **`-c copy` concat fast path** — `_build_silent_video` concat block — when every input clip's `ffprobe` shows identical codec/pix_fmt/fps, use `-c copy` not `-c:v libx264`. Drops concat from ~25min to ~30s on 30-min long-form. (principle: NEW — *don't re-encode when codec params already match*.)
7. **Better-than-default thumbnail generation** — new pass to write `<slug>_thumb.jpg` from the rank-1 image + topic-card composition. Used at upload time. (principle: NEW.)

---

## If I were the creator, the single highest-leverage change is:

**Stop using single-source YouTube compilations as visual wallpaper for long-form Top-10 list videos.** The per-rank visual storytelling (`#3` cuts a still of Antônio Villas Boas, then a Ken Burns push on the Olavo Fontes medical report scan, then a tractor-at-night stock for the "plowing alone" beat) is the entire reason this format works on competing channels. The /make-top10 skill *already authors* this plan in the JSON — the gap is the renderer can resolve per-window URLs but the user (me) shortcut to a single compilation source for v1 because URL-verification was hard. The fix is the helper script in row 3 above — if `find_per_rank_footage()` returns verified-downloadable per-rank URLs at author time, the shotlist contains 30+ real per-window sources and the rendered video has actual storytelling alignment instead of cycling through someone else's b-roll.

Tied for #1: **add rank-card overlays.** Even with the wallpaper-mode visuals, if every rank had a 2.5s `#7 — THE PASCAGOULA INCIDENT · MISSISSIPPI · 1973` card at its start, mute viewers would at least know what they're watching when. That's a 50-line patch, not a content rebuild.
