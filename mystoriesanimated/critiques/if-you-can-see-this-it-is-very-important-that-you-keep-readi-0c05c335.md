# Watching `if-you-can-see-this-it-is-very-important-that-you-keep-readi-0c05c335.mp4`

- **Format**: long-form, 16:01 @ 1920×1080, 83.4 MB
- **Channel**: mystoriesanimated
- **Source**: https://reddit.com/r/nosleep/comments/9qzt9j/if_you_can_see_this_it_is_very_important_that_you/  ← **r/nosleep** (creepypasta — horror)
- **Voice**: Alex Foster (web/lv-alex-foster) — voice override **landed correctly**
- **Title (selected)**: "If You Can See This… Keep Reading"
- **Job**: `0c05c335ff7e47ddaeaf8ba9136e75c4` — first end-to-end successful cloud render in 10 days

---

**One-line gut take**: The **pipeline shipped its first complete video — and the first complete video is the wrong video.** A r/nosleep creepypasta about cosmic horror was rewritten into a 16-minute self-help essay about scrolling habits. Every mechanical thing works (voice, audio, captions, compose, upload-ready mp4). Every editorial thing is wrong (source betrayal, motivational tone on a horror channel, 30-second-per-image deadness, garbled AI text on phone screens, no closer/CTA).

**Score (1–10, 10 = I'd ship to YouTube)**: **2/10** — would not publish; mp4 is technically valid but content is brand-misaligned.

**Verdict**: **FIX** — pipeline mechanics are now real (don't undo any 2026-05-13 commits), but rewrite_long_form for r/nosleep niche is not honoring the subreddit's tonal contract and panel hold defaults are anti-watchable.

---

## What actually happened (the viewer)

I'm watching MyStoriesAnimated. The thumbnail says "If You Can See This… Keep Reading" with a kawaii crayon-drawn girl on a couch holding a phone. **Source URL is literally `r/nosleep`** — I'm expecting some kind of creepy presence in the room, an entity behind the couch, a phone showing something it shouldn't. Maybe blood under the door.

Instead:

- **0–30s** — Girl on a couch holding a phone. The narrator (Alex Foster, voice is great) calmly opens with *"Imagine a completely ordinary afternoon. A person sits somewhere — maybe on a couch, maybe at a desk, maybe on a bus rolling through traffic. Nothing dramatic is happening."* The image **does not move for the entire 30 seconds**. Frame at t=0.3s, t=8s, t=25s are pixel-identical (verified by hash). I assume it's a meditation app ad. I would scroll.
- **30–60s** — Cut to a close-up of the phone screen. The "social feed" on the screen is **clearly hallucinated text**: "Hosbb / Schtoot / Sebaab / Ronpkbi" with garbled letterforms. The narrator is now talking about *"researchers estimating you scroll the height of the Statue of Liberty every day."* Wait, this is a TED Talk?
- **60s–4 min** — Narrator pivots into "attention as currency", "86 billion neurons", "British Cycling team marginal-gains story". Each section is illustrated by **one** static image of the same kawaii character (wandering a path, holding a compass, walking through hills). Nothing in the narration ever references the original Reddit story. **This is not the story I was promised** — this is generic productivity content lifted from a self-help podcast.
- **4–14 min** — Long, calm, well-narrated essay about choosing your attention. Beautifully voiced. Captions readable, yellow italic, well-positioned bottom-third. Mute mode works — you can follow the gist from captions alone. But there is no *story*. There is no character with a name, no tension, no incident, no twist. The hook never lands because there is no hook.
- **Closer (15:30–16:01)** — Girl walks down a path between hills. Caption: *"Because the purpose of a message like this isn't to change your life in one dramatic moment."* Narrator closes with *"Now that you've noticed your attention… where will you point it next?"* **No LIKE/COMMENT split. No subscribe pitch. No reference to source. No anchor for what to engage with in the comments.** I would not comment. I would not share. I would not subscribe.

Watermark "MYSTORIESANIMATED" is visible upper-right (small but present — good).

---

## Section-by-section reaction

| section | wall-clock | what plays | viewer reaction |
|---|---|---|---|
| 0 "The Smallest Decision" | 0:00–1:36 | Girl on couch, then close-up of phone | "Wait, where's the horror?" → checked out by 0:30 |
| 1 "The Currency of Attention" | 1:36–3:12 | Same girl with compass | Now it's a TED Talk about currency-of-attention |
| 2 "Invisible Turning Points" | 3:12–4:48 | Walking through hills | This is a different video than what I clicked on |
| 3 "How Thoughts Become Paths" | 4:48–6:24 | Static neuron-network image (probably) | Mid-form motivational filler |
| 4 "The Choice Most People Miss" | 6:24–8:00 | Girl + abstract attention metaphor | Sound is calm, image static — nearly lulled to sleep |
| 5 "The Power of Slow Change" | 8:00–9:36 | British Cycling 1% better story | This is a **stock self-help anecdote**, not the source story at all |
| 6 "Right Now" | 9:36–11:12 | Open landscape | Nothing has happened |
| 7 "Why This Message Exists" | 11:12–12:48 | Girl walking | Now self-aware: *"You might wonder why a message like this exists at all."* — even the narrator is asking |
| 8 "The Quiet Realization" | 12:48–14:24 | Path | Wraps up |
| 9 "One Last Thought" | 14:24–16:01 | Girl walking into the distance | Closer: *"where will you point it next?"* No CTA, no source reference |

---

## The killer evidence

**Source URL is `r/nosleep/9qzt9j` — a real published creepypasta from 2018.** That story is a malevolent-entity-trapped-in-text horror piece. Our pipeline saw the title pattern ("If you can see this it is very important that you keep reading") and the LLM pattern-matched it to **chain-letter / motivational content**, completely sanding off the horror genre and never once referencing the actual source narrative.

Verified by:
- `envelope.source_url` = the r/nosleep URL
- `script.narration[:600]` opens with "Imagine a completely ordinary afternoon" — pure self-help framing
- `script.sections[5]` is **literally the British Cycling marginal-gains story** — a stock TED-style anecdote that has nothing to do with the source
- `script.title_options` are all motivational variations: "If You Can See This… Keep Reading" / "If You're Seeing This, Something Important Is Happening" / "The Strange Power of the Moments You Don't Notice"

The rewriter never saw the subreddit. Or saw it and ignored it. Or saw it and was instructed to be brand-safe so it sanitised it. Either way: **a horror channel published a meditation video.**

---

## Per-frame fix table (the engineer)

| beat | timestamp | lens(es) | what's wrong (concrete) | classification | the fix |
|---|---|---|---|---|---|
| 0 (panel 0) | 0:00–0:30 | L1, L4, L6, L11 | First 30 s is **one static image** of girl on couch + phone. Frames at t=0.3, t=8, t=25 are pixel-identical. No Ken-Burns motion visible. No hook. No tension. Source URL says r/nosleep but the framing is calm/cute. | **CLASS-OF-BUG** | `script.panels[*].hold_s` defaults to **30.0** for all 24 panels in this script. `pipeline/llm/rewrite_long_form.py` (the prompt that fills `panels`) needs a hard cap of `hold_s ≤ 8` and an explicit instruction to emit ≥3 panels per section. Validator in `pipeline/critic.py` should fail any long-form script where `max(hold_s) > 12`. |
| 0 (panel 1) | 0:30–1:00 | L6 | Phone screen close-up shows **garbled fake text**: "Hosbb / Schtoot / Sebaab / Ronpkbi". Image model hallucinated UI text. Looks unprofessional in close-up. | **CLASS-OF-BUG** | `pipeline/prompts.py` long-form panel-prompt builder must inject `"Avoid all readable text, captions, UI labels, signs, posters, or letters in the image."` for any panel mentioning a screen / phone / sign / book. (Same rule that already exists for shorts panel prompts — appears to not be applied on long-form path.) |
| Hook (0–10 s) | 0:00–0:10 | L1, L10 | Voice opens with "Imagine a completely ordinary afternoon." For r/nosleep, the genre contract demands an unease / dread anchor in the first 10 seconds. Instead it's TED-Talk soothing. | **CLASS-OF-BUG** | `pipeline/llm/rewrite_long_form.py` needs a per-niche hook-template lookup. For `r/nosleep`, the hook MUST contain at least one dread/anomaly token (e.g. "they were never meant to find it", "the message was already inside the house", "by the time she realised, it was too late"). Add a `pipeline/critic_long_form.py` validator that fails the rewrite if the first 200 words contain zero entries from a niche-specific dread lexicon when `niche=r/nosleep`. |
| Section 5 | 8:00–9:36 | L13 | Renderer imports the **British Cycling marginal-gains anecdote** — a stock TED talking point — into a video that is supposed to be sourced from r/nosleep. Source fidelity = 0%. | **CLASS-OF-BUG** | `pipeline/llm/rewrite_long_form.py` system prompt must include `"Do NOT introduce stock anecdotes (British Cycling, Steve Jobs, Roger Bannister, marshmallow test, 10000-hour rule). Every concrete story must be traceable to the source URL."` plus a post-rewrite validator that greps for stock-anecdote keywords and fails the script. |
| Title | thumbnail | L10, L11 | "If You Can See This… Keep Reading" reads like chain-letter spam, not horror-channel branding. None of the 3 title_options have a dread anchor. | **ONE-OFF + CLASS-OF-BUG** | Per-niche title-options template in `pipeline/llm/rewrite_long_form.py`. For r/nosleep, at least one of the 3 options must contain a noun like "the room / the door / the call / the message / the hallway / the figure" + a present-tense dread verb. |
| Section breaks | every ~96 s | L4 | Each section is one panel for ~96 s (24 panels / 10 sections). No mid-section image swap → eyes glaze over. | **CLASS-OF-BUG** | `pipeline/llm/rewrite_long_form.py` should emit ~6–10 panels per section (every 10–15 s), not the current 2-3. Tie to `hold_s ≤ 8` rule above. |
| Closer | 15:30–16:01 | L12, L15 | No LIKE/COMMENT split, no subscribe ask, no source reference. Closing question ("where will you point it next?") is too abstract to drive a comment. | **CLASS-OF-BUG** | `pipeline/llm/rewrite_long_form.py` closer template must end with a **specific binary opinion bait** ("Was the entity real or was she imagining it? — comment YES if you think it was real, NO if you think it was her") AND an explicit subscribe pitch. Same rule as shorts closer template — appears to not be applied on long-form path. |
| Watermark | every frame | L8 | Watermark "MYSTORIESANIMATED" upper-right is **so faint** (~15% opacity, light-grey on light-bg) you have to squint. | **CLASS-OF-BUG** | `pipeline/render/long_form.py` watermark default opacity is too low for the channel's pastel-bg aesthetic. Bump to 30% or use a contrasting color depending on luminance of the underlying frame region. |
| Character continuity | t=0:30 vs t=8:30 | L3 | Character body proportions drift between panels. t_30s_001 = chubby, t_30s_017 = taller / slimmer. Hair length consistent. | **CLASS-OF-BUG** (low priority) | Channel character lock in `mystoriesanimated/cast/` should pin body proportions, not just hair/clothing. Or run an IP-Adapter pass with the canonical character image as conditioning per panel. (Lower priority than the others — viewers will tolerate mild drift if the *story* lands.) |

---

## Class-of-bug fixes for the next 100 long-form videos

For every CLASS-OF-BUG row above, here is the system correction. **Do these IN ORDER** — the first three are the difference between "shippable" and "scroll-past":

1. **Niche-tonal contract on rewrite** — `pipeline/llm/rewrite_long_form.py`
   - Add `NICHE_TONAL_CONTRACT` dict keyed by subreddit/niche. For `r/nosleep`: minimum-required dread lexicon, banned soft-self-help vocabulary, mandatory hook structure, mandatory closer structure (binary opinion bait).
   - Add `pipeline/critic_long_form.py::check_niche_contract(script, niche)` that hard-fails the rewrite if the contract is violated. Wire into the worker so a contract failure triggers ONE retry with the violation surfaced in the prompt before falling back.
   - **Without this, every r/nosleep render will keep producing meditation videos.** This is the single most important fix.

2. **Panel hold ceiling + density** — `pipeline/llm/rewrite_long_form.py` + `pipeline/critic_long_form.py`
   - Cap `hold_s` at 8 (currently defaults to 30.0 across all panels).
   - Require ≥6 panels per section.
   - Validator fails the rewrite if `max(p.hold_s for p in panels) > 12` or `min(panels per section) < 4`.
   - **Without this, every long-form video has 30-second image dead-zones that will tank watch-time.**

3. **No-text rule on phone/sign/book panels** — `pipeline/prompts.py`
   - When the panel `scene` mentions phone / sign / book / poster / chalkboard / screen / label, append: *"AVOID all readable text, UI labels, captions, words, or letters in the image. Use abstract shapes or icons only."*
   - **Without this, every social-media or message-screen close-up will show garbled fake text and look amateur.**

4. **Stock-anecdote ban on rewrite** — `pipeline/llm/rewrite_long_form.py`
   - System prompt ban list: British Cycling, Steve Jobs Stanford speech, Roger Bannister, marshmallow test, 10,000-hour rule, Sara Blakely, "boiling frog", Kobe Bryant work ethic, Michael Jordan cut from team, etc.
   - Validator that greps the rewritten narration for any banned phrase and hard-fails the rewrite.
   - **Without this, the rewriter will drift into TED-Talk filler whenever the source story isn't long enough.**

5. **Per-niche title_options template** — `pipeline/llm/rewrite_long_form.py`
   - For `r/nosleep`: ≥1 of 3 title options must contain a noun-anchor (room/door/hallway/call/figure/message/window/photo) + present-tense dread verb.
   - For `r/AmItheAsshole`: ≥1 must contain "AITA for" prefix.
   - For each declared niche.

6. **Long-form closer template parity with shorts** — `pipeline/llm/rewrite_long_form.py`
   - Closer must end with: (a) specific binary opinion question, (b) explicit "drop a YES or NO in the comments", (c) explicit "subscribe to MyStoriesAnimated for more".
   - The shorts closer template already does this; long-form doesn't — copy it across.

7. **Watermark opacity contrast-aware** — `pipeline/render/long_form.py`
   - When channel aesthetic is pastel/light-bg, bump watermark opacity floor to 30% and use a darker color. Sample a 100×30 pixel region under the watermark, check luminance, pick contrast-maximising color.

---

## What pulled me in

- **Voice quality (Alex Foster)** — warm, calm, well-paced. Audio chain is genuinely production-quality. **Do not regress this.**
- **Caption legibility** — yellow italic, bottom-third, well-positioned, readable on a phone. Word-level sync looks tight.
- **Channel character lock** — the kawaii crayon-drawn girl is recognisably the same character across most panels (with the body-proportion drift caveat noted above). The pastel/cream palette is consistent.
- **Mute mode** — captions alone tell roughly the same story. Works for muted viewers.

---

## What pulled me out

- **0:00–0:30** — One image, no movement, no hook, soothing TED-talk voiceover when I expected dread. Lost me at 8 seconds.
- **0:30–1:00** — Garbled phone-screen text in close-up. "Hosbb / Schtoot" — visible AI hallucination kills credibility.
- **8:00–9:36** — British Cycling anecdote. This is when I realised the video has nothing to do with what I clicked on.
- **15:30–16:01** — Closer with no CTA, no opinion-bait, no subscribe ask. I have no reason to engage and no reason to remember the video tomorrow.

---

## If I were the creator, the single highest-leverage change is:

**Wire `pipeline/critic_long_form.py::check_niche_contract` into the rewrite loop — and make it hard-fail (not soft-warn) when the rewritten script's first 200 words contain zero dread lexicon for r/nosleep niche.** That single validator would have killed this rewrite at the gate and forced ONE retry with the violation surfaced. Without it, every r/nosleep render will keep producing motivational essays because the LLM's natural prior is "give the user something safe, calm, generic". The genre contract has to be enforced at the edge — the LLM will not enforce it on its own.

Adopting this fix lifts the next 100 r/nosleep long-form renders, not just this one. It also generalises: write the contract once per niche, every channel benefits.

---

## What worked at the pipeline level (don't undo)

The 2026-05-13 commits that landed today turned the laptop-only pipeline into a working cloud pipeline. Specifically:
- `7202928 fix(render+input): cloud TTS unknown channel-layout + voice-id silent drop` — voice override now flows wizard → worker. Verified live: TTS used `web/lv-alex-foster/ref.wav`. Silence-concat now handles WAVEFORMATEXT-less WAVs (201 chunks → 996.1 s narration).
- `ea3c8cf fix(input_registry): long-form overlay must dispatch apply_handlers` — long-form overlay now walks both `cfg_targets` AND `apply_handler` descriptors, so wizard form inputs survive into the renderer.

These are **not** the source of any issue surfaced above. The issues above are content-quality / prompt-quality, not pipeline-mechanics.

---

## ⚠️ Length disrespect (added on user's catch)

**You asked for 30 minutes in the wizard. The final mp4 is 16:01 (961 s). That is 53% of what you asked for.**

Verified by re-reading the script + envelope from GCS:

```
target_duration_s passed to rewriter:  1800 s   (30 min)  ← spec carried it correctly
sum of section.target_s the LLM emitted: 1800 s          ← LLM CLAIMS 30 min
                                                            (each of 10 sections = 180 s)
actual narration words written:        2550 words
expected narration duration @ 150 wpm: 1020 s   (17 min)  ← what 2550 words actually plays as
final mp4 duration:                     961 s   (16 min 1 s)
```

The rewriter **lied in its `section.target_s` field**. Each section was tagged `target_s=180.0` (the prompt's instruction was 3 minutes per section), but each section actually contains only ~106 s of words on average — so the LLM under-delivered by 43% per section while still labeling them 180 s. Then the renderer faithfully synthesises whatever audio the words give it, hits 961 s, and ships.

**Per-section breakdown** (all claimed 180 s; actual at 150 wpm):

| section | target_s | words | actual @150wpm | delivery |
|---|---|---|---|---|
| 0 | 180 | 321 | 128 s | 71% |
| 1 | 180 | 306 | 122 s | 68% |
| 2 | 180 | 276 | 110 s | 61% |
| 3 | 180 | 269 | 108 s | 60% |
| 4 | 180 | 247 |  99 s | 55% |
| 5 | 180 | 264 | 106 s | 59% |
| 6 | 180 | 222 |  89 s | 49% |
| 7 | 180 | 218 |  87 s | 48% |
| 8 | 180 | 212 |  85 s | 47% |
| 9 | 180 | 215 |  86 s | 48% |

Notice the **degradation pattern** — sections 0–3 hit ~65% of target, sections 6–9 hit only ~48%. Classic LLM "tired-by-the-end" degradation: the model started strong, ran out of patience, and shortened every later section.

### Class-of-bug fix (length disrespect) — **CRITICAL**

**`pipeline/llm/rewrite_long_form.py` has no post-rewrite word-count validator.** The prompt asks for `~{words_target}` words (4500 for 30 min), the LLM returns whatever it wants, and the renderer accepts it. There is also no `pipeline/critic_long_form.py` validator that checks the realised script against the target duration before kicking off the (expensive) image + TTS stages.

**The fix is a 3-part contract:**

1. **`pipeline/llm/rewrite_long_form.py` — strict word-count instruction:**
   - Replace `~{words_target} total words` (soft hint) with `MINIMUM {words_floor} words and MAXIMUM {words_ceiling} words. The script will be REJECTED if shorter than {words_floor}.` where `words_floor = int(words_target * 0.92)` and `words_ceiling = int(words_target * 1.10)`.
   - Add per-section minimum: `Each section must contain at least {section_words_floor} words. Do NOT trail off in later sections — sustain density end-to-end.`

2. **`pipeline/critic_long_form.py` (NEW) — post-rewrite hard validator:**
   - `assert sum(words_per_section) >= 0.92 * target_words, "rewriter under-delivered by N%"`
   - `assert min(words_per_section) >= 0.7 * mean(words_per_section), "section degradation pattern detected"` (catches the tired-by-the-end LLM)
   - `assert all(s.get('target_s') is None or abs(words_at_150wpm(s.text) - s['target_s']) <= 0.3 * s['target_s'] for s in sections), "section.target_s lies"` (catches mislabelled targets)
   - On any assertion failure: ONE retry with the violation surfaced in the prompt before giving up.

3. **`cloud/render-worker-v2/entrypoint.py` — pre-render gate:**
   - Before dispatching to image/TTS, call `validate_long_form_envelope(env, target_duration_s)`. If it fails, mark the job FAILED with a clear "rewrite under-delivered" error and refuse to spend ~$1 of TTS + image budget on a script that's already wrong.

**Without this, every long-form render will under-deliver by 30-50% and you'll never get the 30 / 60 / 90-minute videos the wizard promises.**

---

## ⚠️ Audio preview disrespect (added on user's catch)

**You asked: "can you hear video in preview as well?"**

Short answer: **No, not really.** Two related bugs in the dashboard.

### Bug 1 — Final video preview is forced-muted

`web-next/app/app/render/[jobId]/page.tsx:211-220`:

```tsx
<video
  src={src}
  controls
  autoPlay
  loop
  muted          ← FORCED MUTED on every video preview, every render
  playsInline
/>
```

The `muted` attribute is hard-coded so the browser allows autoplay. **You can't hear your own video in the preview unless you manually click the unmute button on the player controls.** Most users won't notice the unmute button is there.

### Bug 2 — Long-form NEVER emits the live `narration` artifact

The Live Previews card has an `<audio controls>` block that fires when a `narration` artifact lands during a render (`page.tsx:623-630`). For **shorts**, the worker emits this artifact post-TTS in `cloud/render-worker-v2/entrypoint.py:1198-1201`. **For long-form, no equivalent emit exists.** The long-form path (`pipeline/render/video.py`) only emits `envelope` (post-rewrite), `script` (legacy alias for the same path), and `video` (post-mp4). No `narration`, no `beats`, no per-image `images[i]`.

That means for the entire 24-minute long-form render that just completed, the Live Previews card said "Waiting for the first artifact (script lands ~5 s into the render)…" for 3 minutes (until the rewrite landed) and then showed only `Sectioned envelope` + `Script` until the mp4 landed at 53 minutes in. **No audio preview ever appeared.**

### Class-of-bug fixes

1. **Drop the forced `muted` on the video preview** (or replace with `muted` only when `status === "rendering"`, then `unmuted` on `status === "ready"`):
   - `web-next/app/app/render/[jobId]/page.tsx:217` — change `muted` to `muted={!ready}` so a finished render auto-plays with sound.
   - Keep `autoPlay` but accept the trade-off that browsers may pause autoplay if `muted=false` (users will see the play button — that's fine for a finished video).

2. **Wire long-form artifact emission for narration + beats + images:**
   - `pipeline/render/video.py` after the long_form subprocess returns mp4 — the cache dir at `<channel>/<slug>/cache/` contains `narration.wav`, `beats.json`, and `img_NN.png`. Sweep them and call `emit_artifact` for each, mirroring the SHORT path at `entrypoint.py:1188-1240`.
   - Better: emit them **incrementally as each stage of long_form.py completes**, not all at the end. Add a callback hook in `pipeline/render/long_form.py::main()` for stage transitions (`narration_done`, `beats_done`, `images_done`, `compose_done`) that calls back into the worker's `emit_artifact`.

**Without these, the Live Previews card is useless on long-form renders and the final video preview never plays sound.**

---

## Verdict

**FIX** — render again with the class-of-bug fixes above. **Skip upload until at minimum the niche-tonal contract + length-floor validator + panel hold_s cap are wired in.** Re-critique after.

**Do NOT ship this mp4 to YouTube.** Three independent reasons, any one of which is fatal:
1. **Brand mismatch** — a horror channel publishing a meditation video trains the algorithm against you for weeks.
2. **Length deception** — a 16-minute video metadata-tagged as a 30-minute video tanks audience-retention curves.
3. **Audio quality** — the narration is fine but the dashboard never let you preview it before publishing, so you'd be flying blind.
