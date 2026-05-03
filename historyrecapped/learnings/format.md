---
name: History Recapped is 100% archival footage, not sports-style cut-in
description: Documentary war-history channels need a fundamentally different recipe than SportsStoriesAnimated — pure archival footage timeline + voice-over narrator + captions, no animations, no suspense pause.
type: feedback
originSessionId: d8b4c272-4f1a-4370-83ff-3d002b51108d
---
History Recapped does NOT use the SportsStoriesAnimated format (illustrated beats + 1 footage cut at the narrative climax). The channel is **100% real archival footage** with our narrator on top.

**Why:** Tried sports-style on the first render (1 footage cut + 18 illustrated war-history-book plates, 63s). User rejected: "no suspense format in this type of shorts". The illustrated plates read as cheap when the channel premise is "real footage of real history". Sports works because the goal/celebration animation is the artistic interpretation of a moment we then cut to the real broadcast — but for a 60s war recap, the illustrations are filler between the only thing that matters: the actual archive.

**How to apply:**
- For History Recapped (and likely any documentary-archival channel): pick 5-7 footage windows from HD source(s), concat with blurred-letterbox 9:16, mute source audio, overlay our Cartesia narration + word captions + inline national-flag emojis. NO animations, NO `black_intro: true` suspense pads.
- Bypass `make_shorts.py` for footage-only renders — image-gen wastes ~30 min on a M2 Max for beats whose images are never used. Drive `pipeline.audio.synthesize` + `pipeline.beats.transcribe_words` + `pipeline.beats.split_into_beats` + `pipeline.compose.prerender_word_captions` directly. Reproducible scripts at `scripts/warhistory/{build_100footage.sh,regen_audio_caps.py,final_v2.py}` (~25s end-to-end vs 30min).
- Cut-aware caption clamping is mandatory: when stitching N footage windows into one timeline, word captions whose display would naturally extend past a scene cut must be clamped to end 100ms BEFORE the cut. Otherwise a caption from the previous scene visibly persists into the next, which reads as a glitch.
- Source-audio mix should be 0.0 (mute source). Unlike sports — where the broadcast commentator's call IS the payoff — documentary sources have their own competing narration that conflicts with ours.
- Inline emoji flag composite: pull twemoji PNGs (`cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/<codepoint>.png`, CC-BY-4.0) and side-paste them onto each `word_NNNN.png` BEFORE burning. Apple Color Emoji.ttc renders blank through Pillow's freetype on macOS (sbix bitmap incompatibility) — don't waste time trying to make it work.
- First upload to a brand-new YouTube channel takes 30-60min to second-pass-process even for a 50s Short. Not a bug, just YouTube's anti-spam pass on accounts with no upload history. Don't delete + reupload (deletion penalises channel reputation).
