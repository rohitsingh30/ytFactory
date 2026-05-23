# Cofounder Onboarding Q&A

Log of every question I (newly-joined cofounder) have asked the user, and their answer. Append-only. The most recent question is at the bottom. This is the record of what I learned vs. what I assumed.

---

## Foundational

**Q1. What is ytFactory at a high level?**
**A:** Pipeline the user uses to make videos for their own YouTube channels (internal tool).

**Q2. Who uses ytFactory today?**
**A:** The user + AI agents (Claude / Copilot / scripts).

**Q3. Current production state?**
**A:** Live but unstable / mid-migration.

**Q4. What do you want me focused on first?**
**A:** Build mental model of the system (code-first investigation).
*(Later overridden — user told me describing-without-critique is worthless; pivoted to ask-first.)*

**Q5. What does ytFactory do, at the highest level?**
**A:** End-to-end YouTube video generator (idea → uploaded mp4, fully automated).

**Q6. What's the goal — why are you building this?**
**A:** Mix of "Grow audience (subscribers, watch time, brand)" + "Test what AI-generated content can do (experimentation / portfolio)". Monetization is not the primary driver.

---

## Pain / priorities

**Q7. What's the biggest thing holding back the goal?**
**A:** Output quality AND Reliability. (Not Volume, not Cost.)

**Q8. Output quality — which part of the video is weakest?**
**A:** *Deferred.* User said "we will come to quality later."

**Q9. Reliability — what breaks most often or eats the most babysitting time?**
**A:** Three out of four:
1. Renders failing mid-pipeline (TTS / image-gen / compose stages erroring out)
2. Silent quality failures (renders "succeed" but ship with wrong captions / frozen frames / missing audio)
3. Cloud Run cold-starts / timeouts

Upload failures are NOT a pain.

**Q10. Of those three reliability pains, which is the worst?**
**A:** Mid-pipeline failures — most time spent retrying / debugging stage errors.

**Q11. Mid-pipeline failures — which stage breaks most often?**
**A:** "This is your job" — user expects me to investigate this from code / audit data / Firestore, not ask.

---

## Channels

**Q12. How many channels does ytFactory currently operate?**
**A:** 6 channels currently. No hard limit — user adds new ones as ideas come up. **Future workflow: user will ask me to create new channels.** (Implication: channel-creation should be a low-cost, repeatable workflow.)

**Q13. How do you want me to learn the 6 channels?**
**A:** Read the channel YAMLs yourself (`pipeline/channels/*.yaml`), then ask targeted questions.

**My fresh read of the 6 channel YAMLs:**
- **Cosmos Decoded** — physics "how we knew?". Footage-only (NASA/ESA/CERN/archive). Voice-only narrator. Chatterbox TTS.
- **HindutavaAnimated** — Hindi Mahabharat stories. Amar Chitra Katha comic art via Z-Image-Turbo. IndicF5 Hindi voice.
- **History Recapped** — war history (Shorts + long-form sleep). Archival YouTube footage. Chatterbox voice.
- **MyStoriesAnimated** — Reddit/AITA stories animated. Watercolor + ink. Auto-pulls Reddit.
- **Rhyme Time Junction** — Hinglish kids rhymes. **Sung Suno song (not TTS) + image-to-video motion**. Fundamentally different pipeline.
- **SportsStoriesAnimated/sportsrecapped** — football moments + real broadcast cut-ins. Tifo-style art. Custom wiki_research step.

Two production tracks: footage-only (Cosmos, History) vs animated (others). Five of six use Chatterbox; Hindutava is IndicF5; Rhyme is Suno.

**Q14. Which of the 6 channels are actively shipping vs paused?**
**A:** **All 6 must be functional.** No paused/experimental channels — all of them need to work.

---

**Q15. How many videos ship per week?**
**A:** *Wrong question.* User redirected — wanted me to surface and challenge my channel assumptions, not jump to volume.

**Q16. I listed 8 assumptions about the channels. Which is most wrong?**
**A:** #3 — my "two production tracks (footage-only vs animated)" framing is wrong / misleading. Need to find the actual meaningful split (if any).

**Q17. What's the REAL split?**
**A:** "There is no channel-specific split. We have three visual splits for all channels." → Architecture: any channel can use any of 3 visual modes (AI image-gen / motion video / archival footage). The split is per-render, not per-channel.

**Q18. What are the 3 visual splits called?**
**A:** "Yeah I am fine with any labels, just don't get confused." → Labels don't matter; just stay consistent. Working labels: **AI image-gen / motion video / archival footage.**

**Q19. What's actively being migrated? (re: "live but unstable / mid-migration")**
**A:** "unstable" → Drop the "mid-migration" framing. The system is just **unstable**, not in the middle of a migration. Correcting my earlier interpretation of Q3.

**Q20. What's the operator workflow for one render?**
**A:** "There is a proper UI man, can't you see?" → User redirected me to investigate the UI in code (`web-next/`), not ask.

**Investigation finding (web-next/app/app/create/page.tsx, 2598 LoC):** The UI is a polished 3-step wizard at `/app/create`:
1. **Mode**: 3 cards — "Channel-focused" (works), "Clone a video" (works, at /app/create/clone), "AI-generated" (DISABLED / Coming soon).
2. **Channel**: pick from the configured channels.
3. **Customize**: pick form (short/long), niche, topic (manual or "Auto-generate" via `discoverApi`), voice (filterable by gender / tone / use-case), audio mode (voice/song), visual source, music bed, advanced fields.

Submit calls `renderApi.enqueue(...)` → routes to `/app/render/<jobId>`. So the primary path is Channel-focused wizard. AI-chat mode is disabled.

**Side finding:** UI references a 7th channel `scrollpulse` in `FALLBACK_CHANNEL_KEYS` (page.tsx:567-575), but `pipeline/channels/` only has 6 YAMLs. To verify whether scrollpulse is a 7th channel, stale code, or something else.

---

**Q21. What is `scrollpulse`?**
**A:** "It is a channel, let's add it." → It IS a 7th channel. User wants me to add the YAML.

**Q22. What's the scrollpulse niche / format?**
**A:** Auto-pull Reddit threads + split-screen gameplay overlay (Subway Surfers / Minecraft parkour style). Brain-rot Shorts format. Top of frame = Reddit thread card with TTS; bottom 40% = pre-rendered gameplay loop.

**Q23. How should I proceed on the scrollpulse YAML?**
**A:** "Do it accordingly, don't ask stupid questions, continue with other questions." → I'll draft the YAML myself based on patterns (later, as a task). Keep onboarding questions moving.

---

**Q24. How much time does a render eat?**
**A:** "It breaks currently every time so idk, I don't care about how much time, it is fine, we can look into latency later."

**Critical update:** The pipeline is **currently broken end-to-end** — renders fail effectively 100% of the time right now. This is NOT a flakiness/p99 problem; it's a "the thing doesn't work" problem. Latency / time deferred. Reliability is THE priority.

---

**Q25. What caused "breaks every time"?**
**A:** "It's never reliably worked end-to-end — always been broken or flaky."

**Major reframe:** The pipeline has NEVER worked reliably. This isn't a regression — it's "we haven't yet achieved a first stable end-to-end working state." The goal is to make it work reliably for the first time, not restore something that used to work. **MVP isn't proven yet.**

---

**Q26. Has ANY render ever shipped end-to-end cleanly without manual intervention?**
**A:** "No — the full automated pipeline has never produced a clean uploaded video without breaking somewhere."

**The defining fact:** The MVP isn't proven. The first goal isn't "fix a regression" or "improve quality" or "optimize cost" — it's **make ONE clean automated render ship end-to-end**. Everything else (scale, multi-channel, cost, latency, more channels like scrollpulse) is post-MVP. The system LOOKS comprehensive (6 channels, 7 with scrollpulse, plugin system, web UI, 12+ Cloud Run services, multiple LLM backends) but has never delivered a working render hands-off.

---

**Q27. Which channel is closest to reliably finishing a render?**
**A:** "Roughly equal — none is meaningfully ahead." → No easy MVP-candidate channel. Brokenness is shared / infrastructure-level, not channel-specific.

**Q28. What does MVP look like?**
**A:** "Everything working." → All 7 channels reliably producing finished mp4s end-to-end without intervention. Ambitious MVP — essentially "full functional spec."

**Q29. (Audio splits) How many audio modes?**
**A:** Just 2: TTS narration + sung Suno song. Confirms my assumption.

**Q30. (Output formats) How many output shapes?**
**A:** Just 2: 9:16 Shorts (≤90s) + 16:9 long-form (5-120 min).

**Q31. (Source split) How many content-source types?**
**A:** "3 looks like a good option" — current state is Reddit / Wiki+archive / manual. **User wants to expand source variety AND support multi-source mixing** in a future iteration. Tech-debt / opportunity: extend source-adapter system.

---

**Q32. Engine routing — do all 7 channels use the engine + plugin system?**
**A:** Yes, all 7 use the engine + plugin system (canonical path). Channel-specific scripts referenced in YAML comments are stale.

**Q33. Upload destinations.**
**A:** YouTube only. (X/Twitter code in `pipeline/x_upload.py` is dead or unused.)

**Q34. Render stage chain.**
**A:** "Different ordering — some stages overlap / run in parallel (not strictly linear)." → The 7 stages exist (rewrite / cast / images / tts / asr / compose / upload) but execution isn't strictly sequential. Stage-overlap is a feature; `pipeline/stage_overlap.py` is part of this.

**Q35. Niche vs channel.**
**A:** Niches are **topic-area splits within a channel** (e.g. AITA vs TIFU within a Reddit-driven channel). Same channel destination, different content categories.

**Q36. Trigger model.**
**A:** **Manual — every render is triggered from /app/create UI.** No cron/scheduler auto-fires renders. (The launchd `.plist` files I noticed earlier in `control/` may handle upload scheduling, not render initiation — to verify.)

**Q37. LLM stages per render.**
**A:** **Critic + audio_critic can be SKIPPED and done on laptop.** So the cloud worker needs only 3 LLM stages (rewrite / cast / prompts); critic + audio_critic are post-hoc, laptop-based. (Explains `scripts/cloud_critic_loop.py` + `com.ytfactory.cloud-critic.plist`.)

**Structural skeleton updated:**
- LLM stages: **3 mandatory cloud-side** (rewrite, cast, prompts) + **2 optional laptop-side** (critic, audio_critic)

**Q38. What does "new channel ready" require?**
**A:** YAML + niche variants + branding assets (avatar, banner). No upload-OAuth setup or pre-render test required for "ready".

**Q39. Where do you debug a failed render?**
**A:** **`/app/render/<jobId>` in the UI** — read the error there, retry from UI. UI is primary debug surface, not Cloud Logging / Firestore direct.

**Q40. How do new content ideas get generated?**
**A (corrected):** **"Auto-generate topic" button FETCHES from native source adapters** (Reddit threads, Wiki today-in-history, archive scans, etc.) per channel. LLM brainstorm is FALLBACK when no native source returns or none is configured. My first answer wrongly said "pure LLM brainstorm."

**Q41. Confirm fetch-primary?**
**A:** "Yes — looks like it fetches; what do you think is ideal?"

**Q42. (Cofounder design discussion) Ideal idea-to-script flow?**
**A:**
- For obvious-source channels (r/AITA → Reddit, history → today-in-history/Wiki): **fetch is obvious**, just do that.
- **Use fetched data → apply LLM to enhance / make changes for engagement.** LLM is an EDITOR on real material, NOT an author from nothing.
- For multi-source channels (history etc.): **fetch from multiple sources, then LLM synthesizes the final script.**
- User has an INTENTION here but is not sure what `rewrite` actually does today. Real gap may exist between intent and reality. → Investigation task.

**Q43. Pick-and-rewrite shape?**
**A:** **"Picking and rewriting should be ONE LLM call — give it raw fetches, get back a script."** So the ideal `rewrite` stage takes raw fetches + channel context, internally picks + synthesizes + writes the engaging script. No separate picker stage.

**Q44. Cast stage role?**
**A:** **Separate, smaller LLM call AFTER rewrite** — reads the script and assigns voices / characters per character mentioned.

**LLM stage flow (so far):**
1. `rewrite` — fetch sources → pick → synthesize → engaging script (one big call)
2. `cast` — read script → assign voices/characters (smaller call)
3. `prompts` — per-beat image-prompt authoring (smaller call)
4. (Optional, laptop) `critic` + `audio_critic`

**Q45. Stage parallelism.**
**A:** Confirmed: `rewrite → cast → prompts` sequential, then `tts || images` parallel, `asr` after `tts`, `compose` after all, `upload` last.

**Q46. Channel ceiling.**
**A:** "Leave all this, let's go deep in audio and video, and how we can improve quality. Model is fixed — don't think of changing the model." → Topic pivot for the questioning, NOT a pivot to work. Keep asking closed-form questions, now about audio + video quality. Models (TTS providers, image-gen, LLMs) are not in scope for change.

---

## Audio + video quality (questions on this topic)

**Q47. TTS architecture investigation.**
**Findings:**
- 2 TTS impls: `tts_single` (short engine, one call) + `tts_chunked` (long engine, ~380-char chunks joined with 0.4s silence).
- Both are **single-voice per render**. No per-character routing.
- Speed + atempo are global per render.
- Provider dispatch via `pipeline.audio.synthesize` with cloud→laptop fallback.

**Q48. Image architecture investigation.**
**Findings:**
- 2 main impls: `ai_beat_slideshow` (short engine: one image per ASR beat, ~1-3s each) + `longform_panels` (long engine: one panel per ~30-60s, static stills + hard cuts).
- Per-beat failure threshold: 10% (`_PER_BEAT_FAILURE_THRESHOLD = 0.10` in `ai_beat_slideshow.py:90`). Above this, raises `RenderFailedError`.
- Character consistency depends on `spec.extra["character_description"]` from `cast.json` via `spec_enrich.populate_render_extras`.

**Q49. Of architectural quality risks, which is worst?**
**A:** "No whatever it is, and whatever fucking quality gates you have established, it keeps breaking." → The quality gates themselves are part of the problem.

**Q50. Failure type — gates or code errors?**
**A:** A MIX — some renders die at gates, others crash with real errors (TypeError / kwarg mismatch / network failure). Both classes need addressing.

**Q51. Quality gate philosophy (CRITICAL architectural correction).**
**A:** *"Our philosophy is wrong. We think as if gates will fix everything. We need to think from an engineering perspective and keep gates as occasional barriers and failure points which can be individually retried for smaller pieces instead of redoing everything."*

**Implications (major):**
- The current `RenderFailedError` fail-loud model is too coarse — it terminates the whole render on any gate trigger.
- Right model: gates trigger **granular retry of the FAILING PIECE**, not full-render termination.
- If 10% beats fail image-gen → retry just those beats (stronger prompt / fallback provider). Don't kill the render.
- If TTS chunk N fails → retry chunk N. Don't re-TTS the whole script.
- If one long-form section is short → re-generate just that section. Don't re-rewrite the whole envelope.
- Gates become *repair triggers*, not termination signals.

---

## What I have now (cofounder summary)

The MVP target is **all 7 channels producing clean automated end-to-end videos.** Nothing works reliably today. The structural skeleton is comprehensive (channels / visuals / audio / outputs / sources / triggers / stages). The key design intent for the LLM authoring side is: **fetch real material, single LLM call enhances it into an engaging script.** Gap may exist between this intent and what `rewrite` actually does.

---

## Structural skeleton so far

- **7 channels** (mystories, cosmos, history, hindutava, sports, rhyme, scrollpulse-to-add)
- **3 visual modes** (AI image-gen, motion video, archival footage)
- **2 audio modes** (TTS, sung Suno song)
- **2 output formats** (9:16 Shorts, 16:9 long-form)
- **3 source types currently** (Reddit auto-pull, Wiki/archive auto-pull, manual) — expanding
- **1 upload destination** (YouTube only)
- **7 stages, parallel/overlapping** (rewrite / cast / images / tts / asr / compose / upload)
- **6 plugin slots** (audio / timeline / visualize / overlays / music / compose) — all channels routed through engine

---

## Failure-data dig

**Q52. Retry granularity — brainstorm.**
**A:** "Let's brainstorm" → user wanted me to analyse all failures to date first. Asked me to build a table with rows = renders, columns = stages, cells = what happened. → I built `/ai/render-failures-table.md` from `audit_data/*_summary.json` (122 renders catalogued; ~35% stubs, ~2% shippable). User then told me to also look at cloud data / Cloud Logging / telemetry / Firestore — not just local audit_data.

**Q53. Cloud / production data dig.**
**Findings:**
- 12 recent mp4s in `gs://ytfactory-prod-v3-artifacts/jobs/` (2026-05-16 → 2026-05-18). Most recent sampled: real h264+aac, 49s. Not stubs.
- **Zero per-channel `uploads/` records** across all 7 channel dirs. Mp4s land in GCS; nothing reaches YouTube.
- Production bucket migrated `ytfactory-prod-v2` → `ytfactory-prod-v3`.
- User said: "the data you chose earlier might be old, we have made changes in model since that" → audit_data summaries describe pre-fix state.

**Q54. Most recent actual failure data.**
- `a734babb` (2026-05-20 15:02): `LongFormContractError` at rewrite stage — section 7 contains 51 words; mean section is 363 words (14% of mean); narration delivered 3634 words (81% of 4500 target). Killed before any mp4 produced.
- `7743ca76` (2026-05-21 19:48): Writeback verification failed — mp4 duration 1232s < 1440s required (80% of 1800s target). Mp4 was real (59.7 MB, h264+aac) and uploaded to GCS, but worker exit(1)'d and marked render failed.
- Both = same root cause class: LLM rewrite produces narration shorter than target → length-gate kills render.

User pushed back when I cited commit fixes: "nothing works and it will fail again, have you seen single successful render till now?" → No, zero successful end-to-end renders confirmed.

---

## Where the reduction comes from (the user's diagnostic question)

**Q55. "Why is reduction happening? If linear, shouldn't be there."**
**A:** Pipeline isn't linear at the rewrite stage. It's fan-out:
1. Outline call (1 LLM call) allocates per-section target_words.
2. N parallel section-body calls fire in `ThreadPoolExecutor(max_workers=5)` — each only sees its own target.
3. Aggregate → validate via `critic_long_form.validate_long_form_envelope`.

Three compounding effects:
- Outline LLM can imbalance section allocations (within clamp [0.5x, 2x] of mean).
- Parallel section bodies have no cross-section awareness.
- No redistribution after fan-out.

**Q56. "Where exactly is this happening — which file / which stage?"**
**A:**
- `pipeline/llm/rewrite_long_form.py:617` — `_call_outline_with_retry` (outline call)
- `pipeline/llm/rewrite_long_form.py:1017` — `_generate_all_section_bodies` (parallel fan-out, `_SECTION_BODY_MAX_WORKERS=5`, `_SECTION_BODY_MAX_RETRIES=2`)
- `pipeline/llm/rewrite_long_form.py:880` — `_call_section_body_llm` (one section), raises `SectionTooShortError` at line 989 if `< min_words`
- `pipeline/critic_long_form.py:222` — `HARD_FLOOR_FRAC = 0.50` (total under 50% of expected)
- `pipeline/critic_long_form.py:~370` — `HARD_SECTION_FLOOR_FRAC` (per-section under % of mean) ← this fired on `a734babb`
- `cloud/render-worker-v2/entrypoint.py:~2400` — writeback verification (duration < 80% target) ← this fired on `7743ca76`

---

## The user-driven prompt engineering research

**Q57. "Instruction not following is unacceptable. You should do internet research for this."**
**A:** Researched LLM word-count instruction following.

Findings (sources: [illuminatianon Z-Image-Turbo guide](https://gist.github.com/illuminatianon/c42f8e57f1e3ebf037dd58043da9de32), [Tongyi-MAI HF discussion](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8), [readmedium word-count guide](https://readmedium.com/how-to-hit-exact-word-count-with-chatgpt-592ab179af00), [Dust.tt LLM limitations](https://docs.dust.tt/docs/understanding-llm-limitations-counting-and-parsing-structured-data), [HN word-count discussion](https://news.ycombinator.com/item?id=45149038), [MindStudio GPT-5 prompting](https://www.mindstudio.ai/blog/how-to-prompt-gpt-5-5-outcome-first-prompting)):

7 prompting gaps in `_SECTION_BODY_PROMPT_TEMPLATE` (line 275 of rewrite_long_form.py):
1. Model can't count own words while generating — force it to emit `word_count` in JSON output.
2. "Between min and max" interpreted as "just clear min" — use exact target ± tolerance.
3. No iterative lengthen step — current retry regenerates from scratch.
4. `reasoning_effort=minimal` (cli.py:318, downgraded 2026-05-13) — wrong for length-sensitive output.
5. No fairness-against-mean signal — parallel sections don't see peers.
6. No few-shot anchoring.
7. "Don't pad with filler" + min-word floor is contradictory in practice.

**Q58. Which gaps to attack?**
**A:** User picked **items 1, 2 (hard ±2% tolerance), 4, 5, 7** — and skipped 3 (iterative-extend) initially, then accepted 3 later as the retry shape, and skipped 5 (peer visibility) keeping parallel.

**Q59. Tolerance bands.**
**A:** **Per-section ±10%, total ±15%** (revised up from my initial proposal of ±3%/±10%).

**Q60. Where validator uses emitted word_count.**
**A:** My call (user said "you tell me"): **PRIMARY gate = actual count vs target ±10%; SECONDARY = log emitted-vs-actual delta as telemetry, no hard fail**. The act of emitting forces counting (anchoring); validator uses actual for truth.

**Q61. Item 7 reframe.**
**A:** REMOVE "Do NOT pad with filler" block entirely (negative framing makes LLM over-correct). REPLACE with outline-LLM-written `quality_goal` per section — what good looks like for THIS section ("escalate tension", "reveal twist"). Per-section quality expectations dynamically authored by the outline LLM.

**Q62. Retry shape per section.**
**A:** Iterative-extend (1 attempt): send the FAILED draft back + "expand to N words by adding more source detail." Don't regenerate from scratch.

**Q63. Outline sum-check retry.**
**A:** If `sum(section.target_words)` outside ±15% of user's total target → retry outline once with error in prompt → hard-fail if 2nd also wrong.

**Q64. The critical principle (locked).**
**A:** "Gates should hit and retry, that's it." Gates STAY. Gate fires → retry the failing piece. Don't loosen, don't remove. The retry IS the fix.

**Q65. Writeback verification gate.**
**A:** Becomes a sanity check only (mp4 has valid streams; not a length policer). If upstream gates work, the duration is naturally in band.

---

## Image-gen brainstorm

**Q66. Image-gen per-beat retry shape.**
**A:** 1 retry with image-quality validator (post-gen luminance/variance check); if low, retry once with stronger prompt; then hard-fail beat.

**Q67. Drill into image-gen prompting.**
**A:** I started researching FLUX.2 klein; user corrected — **production uses Z-Image-Turbo, not klein**. I re-researched.

Z-Image-Turbo facts (sources: [illuminatianon guide](https://gist.github.com/illuminatianon/c42f8e57f1e3ebf037dd58043da9de32), [HF discussion #8](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo/discussions/8), [fal.ai z-turbo guide](https://fal.ai/learn/devs/z-image-turbo-prompt-guide), [fliki guide](https://fliki.ai/blog/z-image-turbo-prompting-guide)):
- 6B S3-DiT, CFG-distilled, **negative prompts ignored** (guidance_scale=0.0)
- Optimal prompt: **80-250 words**, structured [Shot+subject] + [Age+appearance] + [Clothing+palette] + [Environment] + [Lighting] + [Mood] + [Style/medium] + [Safety]
- Lighting tokens high-impact: "soft diffused daylight", "cinematic warm key light", "noir high-contrast", "rim lighting"
- Character consistency: **batch of 32+ → pick best "shots from same photoshoot"** (no LoRA needed in code today)
- Avoidance: positive phrasing only ("correct anatomy", "no text in image")

**Q68. Architectural mismatch (verified).**
- `pipeline/images/prompt_refiner.py:1` docstring: *"LLM prompt-refiner pre-step for FLUX.2 [klein]"* — explicit single-target.
- `cloud/render-worker-v2/deploy.sh:106` sets `YTFACTORY_PROMPT_REFINER=1` (refiner ON).
- All 6 channel YAMLs use `cloudrun_z_image_turbo`. **Production never uses klein.**
- The refiner produces klein-optimized output (4-10 word `refined_visual`, BFL composition vocabulary, Qwen3-tuned) — wrong shape for z-turbo's 80-250-word sweet spot.

**Q69. Refiner rewrite.**
**A:** **Replace klein refiner with Z-Image-Turbo refiner** — longer structured prompts (100-200 words assembled), z-turbo lighting vocabulary, drop BFL composition tokens. Single-target rewrite, no klein backward-compat.

---

## Cast / TTS / Compose / Captions brainstorm

**Q70. Cast stage structural fix.**
**A:** **Output structured fields per character (age, hair, build, clothing, signature prop)** — every beat prompt literally prepends the spec verbatim, no paraphrase. **Add secondary characters** when story requires (AITA antagonist, mythology supporting cast).

**Q71. TTS attack — IndicF5 noise.**
**A:** **Fix the IndicF5 service first**: `pipeline/tts/cloudrun.py:929 _synth_cloudrun_indicf5` — verify ref_audio_text actually ships to the model. Specific targeted bug fix.

**Q72. Closer panel (dead-coded in compose.py:711-719).**
**A:** **Port to overlays plugin slot**: `pipeline/render/overlays/closer_panel.py` as OverlayProducer, toggled by `spec.chapter_cards` or new `spec.closer_panel`. Matches the new engine architecture.

**Q73. Caption layer structural fix.**
**A:** Multi-fix:
- Wire `spec.caption_style` fully through to overlay producer + compose mux
- Hard-fail (not warn) when caption import breaks
- Add Devanagari fonts to worker Dockerfile
- **Add caption quality gate**: post-render verify caption density (% audio time with caption overlay); hard-fail if <X%.

---

## Source / Audio / Observability

**Q74. Multi-source mixing.**
**A:** **Per-niche multi-source config** — each variant YAML at `pipeline/variants/<channel>/<niche>.yaml` declares which adapters apply. E.g., `aita_animated → reddit_aita`; `krishna_leela → wiki_mahabharat + scripture_text`.

**Q75. Audio mix.**
**A:** "It's fine, just make sure nothing is worsening." → **No active improvement. Regression guard only.**

**Q76. Observability gap.**
**A:** "Make sure you have access to everything, you are the one who is going to fix." → Operational: the AI agent (Claude) needs read access to logs / Firestore / GCS / metrics for diagnosis. Not a code-level observability feature for the operator UI.

---

## Final brainstorm direction

**Q77. Wrap-up — what's next?**
**A:** "No no, these are things we need to work on. But mainly this exercise was for you to understand what's in my mind. First update the onboarding qa properly, then we will talk." → The brainstorm's PURPOSE was to transfer the user's mental model to me. The attack set isn't immediate work; it's a future plan. Right now: keep the doc current.

---

## CONSOLIDATED ATTACK SET (for future work, not immediate)

Things to work on, decided in this session. Ordering not yet locked.

### Long-form rewrite
1. **Add `word_count` field** to section-body JSON output. Model self-counts (anchoring). Validator uses ACTUAL count for the hard gate; emitted-vs-actual delta logged as telemetry only.
2. **Tighten gates** — per-section ±10% of section's target; total ±15% of total target.
3. **Outline sum-check** — if `sum(section.target_words)` is outside ±15% of user's total target, retry outline once with error in prompt; hard-fail if 2nd also wrong.
4. **Flip `reasoning_effort` back to `medium`** for `rewrite_long_form` (revert the 2026-05-13 `minimal` calibration in `pipeline/llm/cli.py:318`).
5. **Remove "Do NOT pad with filler" block** from `_SECTION_BODY_PROMPT_TEMPLATE`. **Add outline-LLM-written `quality_goal`** per section to the outline schema; section-body prompt receives it.
6. **Iterative-extend retry** — when a section fails its gate, send the failed draft back + "expand to N words by adding more source detail." 1 attempt; hard-fail if 2nd also wrong.
7. **Writeback verification** — drop the 80% duration floor; keep only sanity checks (mp4 has valid streams).

### Image-gen
8. **Replace klein refiner with Z-Image-Turbo refiner.** Longer structured prompts (100-200 words assembled), z-turbo lighting vocabulary, drop BFL composition tokens. Single-target rewrite.
9. **Per-beat retry with image-quality validator** — post-gen luminance/variance check; if low, retry once with stronger prompt; then hard-fail the beat. The 10% threshold becomes a post-retry kill (not first-fail kill).

### Cast
10. **Structured fields per character** (age, hair, build, clothing, signature prop). Beat prompts literally prepend the spec verbatim. **Add secondary characters** when story requires.

### TTS
11. **Fix `_synth_cloudrun_indicf5`** at `pipeline/tts/cloudrun.py:929` — verify `ref_audio_text` actually ships to the IndicF5 model (root cause of the Hindi gibberish noise).

### Compose
12. **Port closer panel to overlays plugin slot** — new `pipeline/render/overlays/closer_panel.py` as OverlayProducer; toggled by `spec.chapter_cards` or new `spec.closer_panel`. Delete the legacy dead code in `compose.py:711-719`.

### Captions
13. **Wire `spec.caption_style` fully** through to overlay producer + compose mux. **Hard-fail** (not warn) when caption import breaks. **Add Devanagari fonts** to worker Dockerfile. **Add caption density gate** post-render.

### Source fetching
14. **Per-niche multi-source config** — variant YAMLs declare their source adapters. LLM rewrite synthesizes across N raw materials.

### Audio mix
15. **Regression guard only** — no active improvement; add tests so no code change worsens current audio quality.

### Observability
16. **Ensure agent (Claude) has read access** to Cloud Logging, Firestore, GCS, metrics — operational, not code.

### Channel onboarding
17. **Add `scrollpulse` channel YAML** (auto-pull Reddit + split-screen gameplay overlay format) + niche variants + branding assets.

---

## Operating principles (locked from this session)

1. **Gates STAY. They are repair triggers, not termination signals.** Gate fires → retry the failing piece (granular). Don't loosen, don't remove. The retry IS the fix.
2. **LLM as editor on real material, not author from nothing.** Fetch sources first; LLM enhances them into engaging scripts.
3. **One LLM call combines picking + synthesizing + writing** (no separate picker stage).
4. **All gates and validators ultimately serve: ship videos.** Not "ensure perfection." Watchable + acceptable = ship; broken = retry the broken piece.
5. **Negative-framing in prompts ("Do NOT X") often amplifies X.** Use positive specifications ("aim for Y", "include Z").
6. **Z-Image-Turbo is the production image model (not FLUX.2 klein).** All image-side prompting, refiner, and gates must be calibrated to z-turbo, not klein.
7. **Model changes are out of scope** — don't propose switching providers; work within current models' behavior.
8. **All 7 channels must be functional.** No hierarchy of importance. Channel-creation is a repeatable workflow.

---

## Open threads (not yet resolved in this session)

- Quality weakest layer (Q8) — deferred. User said "we'll come to quality later."
- How long has this been running?
- Audience scale / traction.
- GCP cost order-of-magnitude.
- Render duration typical / range (deferred — "we can look into latency later").
- What's the budget situation?
- Time pressure / deadline.
- Solo, or with paid/contractor help anywhere?
- Image-gen 10% threshold gate handling — discussed but not specifically locked for the per-beat retry path.
- Image-gen batch+select pattern (research-recommended for character consistency) — not in current attack set, could be a v2 addition.
