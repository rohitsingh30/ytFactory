# /make-reddit-thread learnings

## 2026-05-08 — askreddit-genuinely-unintelligent (first non-AITA ship)

- **CLASS-OF-BUG** — link-post-only subs (TIL / news / pics / etc.) fail `is_self`; pivot to AskReddit / TIFU. Full list + fallbacks in [`docs/reddit_scraping.md` § 1](/Users/rohit/ytFactory/docs/reddit_scraping.md).
- **PIPELINE-BUG** — `pipeline/reddit_card.py::main()` hardcoded `verdict="NTA"`. Added `--verdict` / `--verdict-caption` CLI flags. Per-sub label table at [`scrollpulse/learnings/non_aita_verdict_override.md`](/Users/rohit/ytFactory/scrollpulse/learnings/non_aita_verdict_override.md).
- **PIPELINE-BUG** — `render_post_card` showed barren empty band on no-selftext posts. Engagement footer now collapses under the title. Doc: [`scrollpulse/learnings/post_card_empty_selftext.md`](/Users/rohit/ytFactory/scrollpulse/learnings/post_card_empty_selftext.md).
- **WORKFLOW-IMPROVEMENT** — Stage 0 preflight gates added: OAuth token check + link-post-only sub guard + verdict-label pick. Saves ~90 s of wasted render when token is missing.

## 2026-05-08 — `/critique-video` debrief (score 3/10 — "looks so shitty mg")

- **PIPELINE-BUG** — `render_split_screen.py` ships a placeholder-gradient render when no gameplay mp4 is present; must hard-fail. [`scrollpulse/learnings/gameplay_required_hard_fail.md`](/Users/rohit/ytFactory/scrollpulse/learnings/gameplay_required_hard_fail.md).
- **PIPELINE-BUG** — `render_comment_card` rendered raw URLs that overflowed the right edge of the card. Strip `https?://\S+`. [`scrollpulse/learnings/url_strip_in_card_bodies.md`](/Users/rohit/ytFactory/scrollpulse/learnings/url_strip_in_card_bodies.md).
- **CLASS-OF-BUG** (×5) — comment-card overstuff, single-word floating captions, static cards no motion, `card_full` letterbox bars, card-cache not invalidated. Consolidated in [`scrollpulse/learnings/split_screen_polish.md`](/Users/rohit/ytFactory/scrollpulse/learnings/split_screen_polish.md).
- **WORKFLOW-IMPROVEMENT** — Stage 0 preflight gates extended with a 4th gameplay-mp4 check + Stage 2 now `rm -rf scrollpulse/cache/<slug>/` before re-rendering cards.

## 2026-05-08 — askreddit v3→v6 iteration arc (final architecture locked)

Six rounds of fixes after the first critique. Sections 6–15 of [`split_screen_polish.md`](/Users/rohit/ytFactory/scrollpulse/learnings/split_screen_polish.md) cover everything; memory entry: [`feedback_scrollpulse_brainrot_v6.md`](~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_scrollpulse_brainrot_v6.md).

- **CLASS-OF-BUG** — card-on-gameplay architecture: gameplay fills 1080×1920, RGBA static PNG with card at `CARD_TOP_OFFSET=220` overlaid on top. (Replaces "card on top, gameplay in lower band".)
- **CLASS-OF-BUG** — `render()` picks `random.uniform(0, gp_dur - 60)` start offset per render via `-ss` before `-stream_loop -1`. Without this, every Short opens with frame 0.
- **PIPELINE-BUG** — 9:16 portrait gameplay crashed `crop=1080:768` because `scale=-1:768` produced 432×768. Fix: `scale=1080:1920:force_original_aspect_ratio=decrease` (fg) + `=increase` + crop (bg blur).
- **CLASS-OF-BUG** — engagement counters + score footer removed entirely (user explicit "remove the upvote thing completely"). `_draw_engagement` no longer called from `render_post_card`; comment cards no longer render `▲ score`.
- **CLASS-OF-BUG** — captions Alignment 5 (middle-center) + MarginV=0 → frame center y=960. (Was Alignment 2 + MarginV=500.)
- **WORKFLOW-IMPROVEMENT** — `tts_voice_override` + `tts_ref_text_override` fields in narration JSON for A/B voice tests. Used during female-vs-male compare; female `sarah.wav` won.
- **WORKFLOW NOTE** — yt-dlp default filename can include `[...]` (e.g. *"... [4K 9x16] No Copy.mp4"*) → ffmpeg crashes (treats brackets as filter labels). Always rename to a slug before consumption.
- **WORKFLOW NOTE** — two `render_split_screen.py` processes writing the same `--out` mp4 leaves a 1.5 MB stub with no `moov` atom. Always use distinct `--out` paths for parallel A/B variants.
- **STILL OPEN** — Whisper hallucinates caption text ("SWEARS"/"PEEING" instead of "Virgo"). Port `historyrecapped/learnings/long_form_captions.md` rule: anchor caption text to authored narration, use Whisper for timing only.
- **STILL OPEN** — Per-beat `max_body_lines` override for punchline-at-end comments (FlungerD frontal-lobe joke past the 3-line cap).
