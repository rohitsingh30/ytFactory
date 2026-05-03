# SportsStoriesAnimated — shipped Shorts

Catalog of every short rendered + uploaded. Each entry captures the
specific recipe (source clip, in/out timestamps, design notes) so we
can reproduce or iterate without re-deriving.

For the channel's full pipeline + cookbook, see [`PIPELINE.md`](../../../PIPELINE.md)
"SportsStoriesAnimated — end-to-end cookbook".

---

## 1. Aguero 93:20 — Manchester City win the Premier League

- **Slug**: `aguero-9320`
- **YouTube**: https://youtu.be/D3Y5-8jExIQ
- **Duration**: 30.7s
- **Final iteration**: v16 (uploaded 2026-05-02)
- **Match**: Manchester City 3-2 QPR · Etihad Stadium · 13 May 2012
- **Narrator script**: Final-day-of-season buildup → Last-kick pivot → footage → "Forty-four years of pain. Ended by one touch." → closer

**Footage source**:
- URL: https://www.youtube.com/watch?v=Tg_QMqEmQ-c (78s "All Angles & All Commentary")
- Window: `in_s=0.0, out_s=10.5` (4s window via "Balotelli, Aguerooo!" + "I swear you'll never see anything like this ever again!")
- `black_intro: true`, `audio_mix: 0.75`
- We initially tried `sNnHXLb05lw` (58s with desat replay graphic baked in) — rejected because the broadcast's stylized post-call sequence dropped energy mid-window. The Tg_ source opens directly on Tyler's call without graphic.

**Notable iteration milestones** (every render number ≈ a fix):
- v0–v4: AnimateDiff motion path → user rejected, switched to z_image_turbo slideshow
- v5: critique loop established; identified channel-locked seeds, kit accuracy, hook problem
- v6–v7: dossier-driven cast.json with per-character seeds; pronunciation overrides; first footage cut wired
- v8: pivot moved from "Aguero scores" to "Last kick" (buildup phrase)
- v9: footage `out_s` tightened from 8s → 4s (post-call replay was dragging energy)
- v10: blurred-letterbox replaced center-crop (action at edges was being chopped)
- v11: switched to Tg_QMqEmQ-c source (cleaner live broadcast, no desat replay)
- v12: ASR-aligned cut points via whisper transcription of the source
- v13: black-intro suspense beat for "Last kick"; closer panel
- v14: tail-breath buffer (300ms) so word-final consonants don't get chopped by ASR underestimation
- v15: silence insertion moved to `beats[i+1].start` (was cutting "season" tail mid-phoneme)
- v16: numeric-display lint patched the gibberish "88th minute" clock

---

## 2. Iniesta 2010 World Cup Final winner

- **Slug**: `iniesta-2010-wc`
- **YouTube**: https://youtu.be/WQep72LMSsU
- **Duration**: 50.7s
- **Final iteration**: v3c (uploaded 2026-05-03)
- **Match**: Spain 1-0 Netherlands (a.e.t.) · Soccer City, Johannesburg · 11 July 2010
- **Narrator script**: 14 yellow cards / brutal match → 90 minutes 0-0 → extra time → "Last touch of the World Cup" pivot → footage → Iniesta lifts shirt → Dani Jarque siempre con nosotros → Spain are World Champions → closer

**Footage source**:
- URL: https://www.youtube.com/watch?v=3pCPQDxZzfY (52s "All Angles" — 1080p AV1 HD)
- Window: `in_s=0.0, out_s=14.5` (full English Sky Sports call: "Iniesta's in the middle all alone, if Fernando Torres can find him, it's stabbed away uncomfortably to Fabregas, surely now, surely now, Spain have won the World Cup for the first time in history.")
- `black_intro: true`, `audio_mix: 0.75`

**Notable iteration milestones**:
- v1: ran into the FPS-mismatch bug (source 25fps + black-intro 30fps → concat dropped 2.7s of video at the end → last 2.7s was black while audio continued). Fixed by forcing 30fps on both passes in `footage.py`.
- v1: Spanish phrase "siempre con nosotros" came out as German-ish gibberish; patched dossier `pronunciation_dict` with phonetic respellings ("see-EM-pray con no-SO-tross").
- v1: Subscribe button rendered as bottom-left thumbs-up + bare YouTube icon by diffusion; replaced with PIL-rendered centered "SUBSCRIBE" button overlay via `captions.render_subscribe_button` + `compose_clips(subscribe_button=True)`.
- v2: AV-sync drift in beats 1-3 (image cache reused img_NN.png by index even though prompts.json shifted between v1's 16-beat split and v2's 14-beat split). Fixed with content-hash sidecar (`img_NN.prompt.sha256`) — `make_shorts.py` skips regen only when the prompt hash matches.
- v3a-c: targeted regen of just the misaligned beats using the hash-cache.

---

## Recipe template for the next moment

When adding a new sports moment, copy this block and fill in:

```
- slug: <kebab-case>
- match: <Team A 1-0 Team B · Venue · Date>
- moment-defining commentary line: <"And there it is!", "AGUEROOOOO!", etc.>
- candidate YouTube URLs:
    1. <ALL ANGLES variant — 60-90s preferred>
    2. <fallback HD highlights, 4-5 min>
- whisper transcript anchors:
    IN word:    <buildup phrase, t=?.??>
    CALL word:  <player surname shouted, t=?.??>
    OUT word:   <post-call payoff, t=?.??>
- pronunciation respellings to add to dossier:
    <name>: <kokoro-friendly form>
```

Then run the cookbook in [`PIPELINE.md`](../../../PIPELINE.md) Steps
1-9.

---

## Iteration cost as of 2026-05-03

- 2 channels (MyStoriesAnimated + SportsStoriesAnimated) actively shipping.
- 2 sports shorts uploaded; 16+3 total renders to ship them = ~19
  failed iterations turned into 25+ class-of-bug rules in
  `.claude/projects/<hash>/memory/feedback_*.md`.
- Single biggest time sink: image regeneration on M-series under
  thermal throttling (~5-10 min/beat on hot GPU). Cache invalidation
  bugs (voice fingerprint, image content-hash) cost 2-3 entire
  re-renders before the sidecar fix landed.
