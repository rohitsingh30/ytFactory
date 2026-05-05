---
name: SportsStoriesAnimated channel — Tifo Football style with real footage cut-ins
description: ytFactory production channel for animated football moments; imitates Tifo Football's minimalist tactical aesthetic but cuts to real broadcast footage at the exact narrated moment
type: project
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
Production channel: **SportsStoriesAnimated** — YAML at `channels/sportstoriesanimated.yaml`. (Renamed 2026-05-01 from working name `sports_moments`. The internal `source_adapter` ID is still `sports_moments_manual`; that's an internal adapter key and was kept stable to avoid breaking the registration. Some historical paths and `.claude/settings.local.json` permission entries also still reference the old `sports_moments` string — leave those alone unless they break.)

Visual + tonal model is **Tifo Football** on YouTube — minimalist line-art illustrations, top-down tactical pitch diagrams, calm analytical narrator, cream/muted palette, educational rather than hype tone.

**Key differentiator from Tifo:** at the climactic beat, cut from animation to the **actual broadcast clip** of the moment, frame-accurately matched to what the narrator just described. Tifo can't always do this for licensing reasons; we're treating takedowns as expected cost.

Footage scrub + caption-emoji density rules: see `footage_scrub_watermarks.md` (channel-local) and `/docs/shorts_caption_emoji_density.md` (cross-channel).

**Why:** user explicitly steered away from generic "bold sports comic" aesthetic toward Tifo's editorial/tactical style. The real-footage cut-in is "the main thing here" — animation is build-up; the footage is the payoff.

**How to apply:**
- Channel YAML aesthetic should be Tifo-like (line-art, cream/teal/orange palette, calm British-style narration via Kokoro `bm_george` or similar), not bold comic-book.
- Stories should be tactical/historical/iconic-moment-driven (a play, a tactical shift, a career-defining moment), not generic highlight reels.
- **Players are rendered as generic cartoon characters identified by KIT + JERSEY NUMBER + body type, NOT by facial likeness.** Messi = "small left-footed forward, Argentina sky-blue/white stripes, #10". User explicitly chose this over IP-Adapter / LoRA likeness work — viewers fill in identity from context. Avoids publicity-rights gray zone and a 15-25 GB model download.
- This means cast.py schema is reused as-is — supporting[] `description` field already carries kit/number/body-type strings into per-beat prompts via prompts.py. No likeness conditioning needed.
- **DO NOT use the AnimateDiff motion path (`motion_provider: animatediff_lcm` etc.) for this channel.** User saw the AnimateDiff output and explicitly rejected it ("use the other format idiot, remove reference to this type of animation all together"). Use the static-image-per-beat slideshow path with `image_provider: z_image_turbo` + Ken Burns zoom — same as today_in_history / aita_animated / wiki_oddities. When the user says "animation" for this channel, they mean "stylized cartoon imagery," not literally moving cartoon frames. The footage cut delivers the motion punch; the rest stays still.
- New beat `kind: footage` resolves via pipeline/footage.py (yt-dlp + ffmpeg trim + audio-duck), with frame-precise in_s/out_s in the script JSON. The footage cut is what delivers "the real player" — the static cartoon images don't need to.
- Pitch diagrams are an optional accent primitive, not the main vehicle. The cartoon scenes carry the channel.
- **Channel YAML sets `narrator_visual_mode: voice_only`.** This switches `pipeline/prompts.py` to a voice-only authoring preamble (forbids "the character" / "the narrator" wording, lists dossier supporting[] for the LLM to name explicitly), and `make_shorts.py` clears the channel-wide `character_description` so no analyst persona is painted into per-beat image prompts. Every beat must depict either a named real person from the dossier (kit + #) or a pure scene/object shot. See feedback_sports_no_narrator_on_screen.md.
- Bootstrap path is `feed: manual` — script JSON specifies the YouTube URL + timestamps; auto-find-the-moment is v2.
- **Class-of-bug — "football" → American football bias** (branding render 2026-05-01). The bare word "football" caused z_image_turbo to render an oval gridiron / rugby-shaped ball for the channel avatar. Fix is in the channel YAML's `image_style_prefix` (appends "association football soccer context, never american football or rugby") AND in the prompt-author guidance: any LLM or human writing key_visual / scene strings for this channel must say "soccer ball", "association football", or describe panels explicitly ("black hexagonal and white pentagonal panels, perfectly round sphere"). Never bare "football".
- Branding assets (channel icon 800×800, banner 2048×1152) live at `data/intermediate/sportstoriesanimated/branding/{avatar.png,banner.png}`. Generation script: `scripts/render_sportstoriesanimated_branding.py`. Re-render with `scripts/render_sportstoriesanimated_avatar_v2.py` for the avatar after a soccer-disambiguation prompt fix.

**Formats on this channel:**
- Single-moment Short — `/make-script` → `sportstoriesanimated/raw/<slug>.json`. Two shipped (Aguero 93:20, Iniesta 2010 WC).
- Top-N tier-list — `/make-ranking` (channel YAML `ranked` variant). One shipped (top3-stoppage-goals).
- **Last-N rivalry recap** (NEW 2026-05-04) — `/make-rivalry-recap` (reuses `ranked` variant). **Footage-only** (no AI imagery, lowest-memory render). See [rivalry_recap_format.md](rivalry_recap_format.md).
- **Long-form documentary 20-30 min** (NEW 2026-05-04) — `/make-sports-doc`. Real broadcast match footage + commentator/YouTuber talking-head clips + b-roll + chapter cards + lower-thirds. 16:9 horizontal. Same OAuth as Shorts; separate `long_form_doc:` config block. Helpers: `scripts/sportstoriesanimated/{find_match_clips,find_commentary_takes,find_b_roll}.py` + render at `scripts/sportstoriesanimated/render_long_form_doc.py`. See [long_form_doc_format.md](long_form_doc_format.md).
  - **Multilingual broadcast pools degrade `find_match_clips`** (2026-05-05, Ronaldinho lost-years run) — when sources span Portuguese / Spanish / English, the helper's bag-of-tokens fuzzy match returns mostly coincidental hits (right token, wrong context). Author windows from broadcast structure instead; reserve helper for distinctive multi-token phrases. See [long_form_doc_helper_multilingual.md](long_form_doc_helper_multilingual.md).
  - **`narration_anchor` MUST be respelling-free** (2026-05-05, same run) — TTS speaks `ron-al-DEEN-yo` / `bare-na-BAY-oo` as phonemes; whisper transcribes them as English-spelled words; anchors copied verbatim from respelled prose silently miss. Ronaldinho v1 dropped 14 match + 2 archival + 3 b-roll overlays this way (12/34 matched). See [long_form_doc_anchor_no_respellings.md](long_form_doc_anchor_no_respellings.md).

**Mature pipeline as of 2026-05-03 (after 19+ iterations across Aguero + Iniesta):**
- Full end-to-end cookbook lives in `PIPELINE.md` "SportsStoriesAnimated — end-to-end cookbook" section (the 9-step recipe — raw → dossier → pronunciation patch → cast → source clip + whisper-aligned cut points → script with footage block → render → verify → upload).
- Every gotcha encountered is in `PIPELINE.md` "every gotcha we paid for" symptom-driven table (footage black on first frame from `-ss` ordering, fps mismatch losing 2.7s of tail, blurred-letterbox vs center-crop, ASR-aligned cut points, narration-duck during footage, silence insert at next-beat-start, black-intro suspense + tail-breath buffer, Spanish phrase respellings, subscribe button overlay, image cache content-hash sidecar, etc.).
- Catalog of shipped Shorts (with URLs, source clips, in_s/out_s, design notes) at `data/intermediate/sportstoriesanimated/SHIPPED.md`.
- Two shipped: Aguero 93:20 (https://youtu.be/D3Y5-8jExIQ) and Iniesta 2010 WC (https://youtu.be/WQep72LMSsU).
- When adding a new sports moment, follow the cookbook — don't re-derive from scratch.
- Each iteration's class-of-bug rule is captured in a `feedback_sports_*.md` memory; the index is in `MEMORY.md`. Pinned rules to read FIRST when starting a new sports session: `feedback_sports_footage_is_usp.md`, `feedback_sports_cast_locked_tokens.md`, `feedback_sports_footage_cut_at_pivot.md`, `feedback_sports_footage_dont_overshoot.md`, `feedback_sports_footage_blurred_letterbox.md`, `feedback_sports_footage_align_to_commentary.md`, `feedback_sports_footage_black_intro.md`, `feedback_sports_no_narrator_on_screen.md`, `feedback_sports_character_consistency.md`, `feedback_pronunciation_pretts.md`.


## Upload throttle
Uploads on this channel auto-enforce a ≥1h gap between consecutive public moments — see [docs/upload_throttle.md](../../docs/upload_throttle.md). Override by passing an explicit `publish_at` (dashboard "Publish at" picker).
