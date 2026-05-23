# Decision Log

ADR-style architectural decisions. Each entry: Date / Context / Options considered / Chosen approach / Rationale / Consequences / Future risks. Append-only; most recent at the bottom.

Source for the 2026-05-22 entries: `ai/onboarding-qa.md` (cofounder onboarding brainstorm, Q1-Q77 + the CONSOLIDATED ATTACK SET + locked operating principles).

---

## ADR-001: Lean documentation surface (18 files) — keep the full set

**Date:** 2026-05-22 (resolved earlier in session)
**Context:** The charter at `ai/engineering-principles.md` mandates 18 ai/*.md files (current-system-map, architecture, runtime-flows, source-of-truth, state-management, api-contracts, data-models, external-integrations, decision-log, known-fragility, tech-debt, open-questions, improvement-opportunities, performance-concerns, security-observations, product-observations, developer-experience, debugging-notes). The "Lean Engineering Principles" section in the same file warns against "unnecessary documentation" and "abstractions for hypothetical future needs."
**Options:**
1. Lean — collapse to ~5 docs (system-map + decision-log + debugging-notes + product-observations + tech-debt).
2. Full 18 — write all per the charter.
3. Hybrid — write the 18 but keep each one small and tied to actual code references.
**Chosen:** Option 3.
**Rationale:** The 18 docs each cover a non-overlapping concern (state-management ≠ data-models ≠ api-contracts). Collapsing creates god-files that get stale faster. The lean principle applies to *code abstractions*, not *understanding artefacts*. The risk that motivates the lean clause (docs drifting) is mitigated by tying every doc to a file:line citation so re-verification is mechanical.
**Consequences:** 18 files exist. Maintenance burden is one update per change; offset by the file:line discipline (a `grep -r` finds stale references).
**Future risks:** If the team stays solo, three or four of the 18 (api-contracts, data-models, external-integrations) may end up under-used and rot. Quarterly prune: if a doc has not been opened/edited in 90 days, demote into the nearest neighbour.

---

## ADR-002: Verbatim preservation of user prompts

**Date:** 2026-05-22 (resolved earlier in session)
**Context:** The user supplied a multi-section prompt in `ai/engineering-principles.md` (the "critique-first technical co-founder" mode, the "system steward" mode, and the "Lean Engineering Principles" trailer). Past habit had been to "tighten" or "synthesize" such input.
**Options:**
1. Compress into a single distilled prompt.
2. Preserve every section verbatim, append-only.
**Chosen:** Option 2.
**Rationale:** The user's exact wording is the system contract. Compression silently changes meaning ("challenge me" → "ask clarifying questions"), and the user has no easy way to spot the drift. Verbatim preservation makes intent diffable.
**Consequences:** `ai/engineering-principles.md` is long and has some repetition between sections; that is by design.
**Future risks:** Two sections contradict (e.g. one says "do not generate excessive tests", another implicitly endorses test-coverage gates). When that fires, log it here as a new ADR rather than silently editing one section.

---

## ADR-003: Gates are repair triggers, not termination signals

**Date:** 2026-05-22 (locked in Q51 + Q64)
**Context:** Current `RenderFailedError` and `LongFormContractError` paths kill the entire render on any gate trigger — verified at `pipeline/critic_long_form.py:222` (`HARD_FLOOR_FRAC = 0.50`), `pipeline/render/ai_beat_slideshow.py:90` (`_PER_BEAT_FAILURE_THRESHOLD = 0.10`). When a single section is short or 10% of beats fail image-gen, the worker discards everything and exits. 2026-05-20 render `a734babb` died because section 7 was 51 words against a 363-word mean; the other 11 sections were fine.
**Options:**
1. Loosen the gate thresholds (treat 30% short as acceptable).
2. Remove the gates entirely.
3. Keep the gates, change the action — fire-then-retry the failing piece, not the whole render.
**Chosen:** Option 3 ("Gates STAY. They are repair triggers, not termination signals" — Q64).
**Rationale:** Gates exist because silent fallback produced unshippable mp4s (see MEMORY.md "Silent-fallback unshippable output"). The bug is the *action on fire*, not the firing itself. Loosening (option 1) re-admits the unshippable output; removing (option 2) takes us back to silent fallback. Granular retry preserves the gate-as-quality-floor while not destroying 11 good sections.
**Consequences:** Every gate site needs a granular retry shape: per-section iterative-extend (ADR-007), per-beat image retry with stronger prompt (ADR-008), per-chunk TTS retry. Writeback verification stops being a length policer and becomes a sanity check (ADR-010).
**Future risks:** Retry-on-fire creates unbounded loops if not capped. Every retry site must have a hard attempt ceiling (1 or 2) after which it raises and the render dies — same end state as today, just only after the granular retry actually tried.

---

## ADR-004: Per-section ±10%, total ±15% word-count tolerances

**Date:** 2026-05-22 (Q59)
**Context:** Current long-form length validation at `pipeline/critic_long_form.py` uses hard floors (50% of expected total; per-section floor against section mean). The 2026-05-20 a734babb failure tripped the per-section floor at 14% of mean. Initial proposal during Q57-Q59 was ±3% per-section / ±10% total based on the OpenAI/Anthropic length-instruction-following literature. User raised these to ±10% / ±15%.
**Options:**
1. ±3% per-section / ±10% total (literature-default).
2. ±10% per-section / ±15% total (user-chosen).
3. Soft warning only, no hard band.
**Chosen:** Option 2.
**Rationale:** Tight bands (option 1) trigger the retry path on every render; the retry cost dominates and we never converge. ±10% is wide enough that a well-behaved section LLM hits it first-shot most of the time; ±15% total absorbs the residual per-section variance after aggregation. Option 3 is the pre-gate world that produced silent-fallback unshippable outputs.
**Consequences:** New validator logic at `pipeline/critic_long_form.py`. The outline-sum gate (ADR-006) uses the same ±15% number.
**Future risks:** ±10% is generous. A consistently-low LLM (always lands at -9%) ships 90%-length narrations indefinitely. Mitigation: log emitted-vs-actual delta as telemetry (ADR-005) so a sustained skew is visible before it becomes a quality complaint.

---

## ADR-005: Section LLM emits `word_count`; validator uses actual

**Date:** 2026-05-22 (Q57 item 1, Q60)
**Context:** Research from Q57 (Dust.tt, HN, MindStudio sources) shows LLMs cannot count their own output mid-generation, but emitting an explicit count field forces self-anchoring and significantly tightens length following. Question was: which is the source of truth for the gate — the LLM-emitted count, or the post-hoc actual count?
**Options:**
1. Trust the LLM's emitted count; gate on it.
2. Recount externally; gate on actual; ignore the emitted field.
3. Recount externally; gate on actual; log the emitted-vs-actual delta as telemetry.
**Chosen:** Option 3.
**Rationale:** Emitting forces counting (the anchoring benefit). External recount is the truth (LLMs lie). Logging the delta surfaces "the LLM thinks it wrote 500 words but actually wrote 380" as a learnable signal — sustained drift means the prompt needs work, not the model.
**Consequences:** `_SECTION_BODY_PROMPT_TEMPLATE` at `pipeline/llm/rewrite_long_form.py:275` adds a `word_count` field to the JSON schema. The validator computes actual via `len(text.split())`. The delta lands in render telemetry, not in the gate decision.
**Future risks:** If the LLM cheats by inflating `word_count`, the gate still fires on actual — there is no exploit surface. If the actual count is computed differently from the LLM's internal count (token-splitting vs whitespace-splitting), the delta will show a constant offset and that's fine — what matters is the change in delta over time.

---

## ADR-006: Outline sum-check with single retry

**Date:** 2026-05-22 (Q63)
**Context:** The outline LLM at `pipeline/llm/rewrite_long_form.py:617` (`_call_outline_with_retry`) allocates `target_words` per section. The clamp at `[0.5x, 2x] of mean` does NOT enforce that `sum(target_words) ≈ total target`. If the outline allocates 4800 words across sections when the user asked for 4500, the section LLMs honor their allocations and the total ships at 4800 — which then trips the 80% writeback gate the other direction.
**Options:**
1. Hard-fail the render on outline sum mismatch.
2. Rescale the allocations post-hoc.
3. Retry the outline LLM once with the error fed back in; hard-fail on second mismatch.
**Chosen:** Option 3.
**Rationale:** Rescaling (option 2) reintroduces silent correction — the LLM authored a section assuming 600 words and we just told it "actually do 480"; the section content has already been outlined for the larger budget. Hard-fail (option 1) is wasteful for a recoverable miss. Retry-with-error is the gate-as-repair-trigger pattern (ADR-003) applied to the outline call.
**Consequences:** New retry loop around `_call_outline_with_retry`. Tolerance ±15% (same as ADR-004 total band).
**Future risks:** Two-retry outlines double the rewrite latency on a bad day. The retry cap of 1 keeps the worst case bounded.

---

## ADR-007: Iterative-extend section retry (1 attempt)

**Date:** 2026-05-22 (Q62)
**Context:** Current `_SECTION_BODY_MAX_RETRIES=2` at `pipeline/llm/rewrite_long_form.py:1017` retries by regenerating the section from scratch. Each retry starts cold, has no context from the failed draft, and often produces similar-length output (the same LLM, same prompt, similar tokens).
**Options:**
1. Keep cold-retry, raise the count.
2. Iterative-extend: send the failed draft back with "expand to N words by adding more source detail."
3. Switch model for the retry.
**Chosen:** Option 2 (1 retry attempt).
**Rationale:** Cold retry of a length-failed section usually fails the same way. Extending the existing draft preserves the LLM's already-committed structure and only adds material — much higher recovery probability per attempt. Model switching (option 3) is out of scope per Q46 ("Model is fixed").
**Consequences:** New prompt template for the extend path; the failed draft is appended to the context. Retry budget drops from 2 to 1 (one extend attempt, then hard-fail).
**Future risks:** Iterative-extend can pad with filler if the LLM has nothing more to say from the source. Mitigation: the extend prompt explicitly directs "add more source detail" — when sources are exhausted, the retry fails cleanly and the gate kills the render. That's the intended outcome for thin source material.

---

## ADR-008: Z-Image-Turbo refiner replaces FLUX.2 klein refiner

**Date:** 2026-05-22 (Q67-Q69)
**Context:** All 6 channel YAMLs set `image_provider: cloudrun_z_image_turbo`. The refiner at `pipeline/images/prompt_refiner.py:1` docstring reads "LLM prompt-refiner pre-step for FLUX.2 [klein]" — it's still calibrated for klein (4-10 word `refined_visual`, BFL composition vocabulary, Qwen3-tuned). Production never uses klein. Per the [illuminatianon Z-Image-Turbo guide](https://gist.github.com/illuminatianon/c42f8e57f1e3ebf037dd58043da9de32) and [fal.ai guide](https://fal.ai/learn/devs/z-image-turbo-prompt-guide), z-turbo wants 80-250-word structured prompts: [Shot+subject] + [Age+appearance] + [Clothing+palette] + [Environment] + [Lighting] + [Mood] + [Style/medium] + [Safety]. Negative prompts are silently ignored (`guidance_scale=0.0` — CFG-distilled).
**Options:**
1. Keep klein refiner; add a z-turbo-aware adapter.
2. Replace the refiner with a z-turbo-native one. Single-target, no klein backward-compat.
3. Remove the refiner entirely (let raw beat prompts go to z-turbo).
**Chosen:** Option 2.
**Rationale:** Adapter (option 1) carries klein-specific assumptions (terse output) through to the new path — actively harmful. No-refiner (option 3) loses the prompt-quality lift the refiner gives on raw LLM-authored beat prompts. Single-target rewrite matches the locked operating principle "Z-Image-Turbo is the production image model."
**Consequences:** `pipeline/images/prompt_refiner.py` rewrite. New per-channel lighting-token vocabulary. Drop BFL composition tokens. Drop negative-prompt blocks (silently ignored anyway).
**Future risks:** If a future channel needs klein (cost, latency, style fit), we re-introduce a per-channel refiner switch then. Don't pre-build for it.

---

## ADR-009: Cast stage outputs structured character fields, beat prompts prepend verbatim

**Date:** 2026-05-22 (Q70)
**Context:** Cast.json today is a loose blob — character description varies in shape per render, and `spec.extra["character_description"]` at `pipeline/render/spec_enrich.py` passes whatever string the LLM emitted. Beat prompts then paraphrase the description, drifting the character spec from beat to beat. Result: same "character" looks different in panel 1 vs panel 7 of the same render.
**Options:**
1. Tighter free-form prompt instruction ("be consistent across beats").
2. Structured cast fields (age, hair, build, clothing, signature prop) + verbatim prepend in every beat prompt.
3. Reference-image / IP-Adapter / LoRA per character.
**Chosen:** Option 2.
**Rationale:** Free-form (option 1) is what we have; doesn't work. Reference-image (option 3) is a model-architecture change — out of scope per Q46. Structured fields + verbatim prepend is a deterministic textual fix: the same description token-for-token in every prompt, so the model sees the same character.
**Consequences:** Cast schema changes; beat-prompt assembly changes; secondary-character support added when stories require (AITA antagonist, mythology supporting cast). `pipeline/cast.py` + `pipeline/prompts.py`.
**Future risks:** Verbatim prepend lengthens prompts toward the z-turbo 250-word ceiling (ADR-008). If a render has 3 named characters, the prepended spec eats budget the per-beat scene description needs. Budget-share rule: cast spec ≤ 40% of total prompt length.

---

## ADR-010: Writeback duration gate becomes sanity check only

**Date:** 2026-05-22 (Q65)
**Context:** `cloud/render-worker-v2/entrypoint.py:~2400` writeback verification hard-fails if mp4 duration is under 80% of target. 2026-05-21 render 7743ca76 produced a real 59.7 MB h264+aac mp4 of 1232s against a 1440s floor (1800s target) — perfectly watchable, killed at the last gate.
**Options:**
1. Keep the 80% floor.
2. Lower the floor (60%).
3. Drop the duration floor; keep mp4-stream-validity checks only.
**Chosen:** Option 3.
**Rationale:** If upstream length gates (ADR-004 + ADR-006 + ADR-007) work, the duration is naturally in band — the writeback gate is redundant and only fires when upstream over-corrected. If upstream gates *don't* work, the writeback gate is killing real renders for a 14% miss while the actual fix belongs upstream.
**Consequences:** Writeback checks reduce to: file exists, h264+aac streams present, duration > 0, no truncation. Length policing moves entirely to rewrite gates.
**Future risks:** If the rewrite gates are loosened (ADR-004 widened beyond ±15%), short mp4s can ship. The mitigation is that ADR-004's ±15% is already generous; any further widening needs a new ADR.

---

## ADR-011: Closer panel ported to overlays plugin slot

**Date:** 2026-05-22 (Q72)
**Context:** Closer-panel code lives at `pipeline/render/compose/compose.py:711-719` as dead code. The plugin engine has an `overlays/` slot designed for exactly this. Pre-engine code was retained at the compose layer because the original short pipeline composed everything in one ffmpeg pass.
**Options:**
1. Delete the closer-panel code (drop the feature).
2. Leave it in compose.py.
3. Port to `pipeline/render/overlays/closer_panel.py` as an `OverlayProducer`.
**Chosen:** Option 3.
**Rationale:** Closer panel is a real product surface (the YouTube-style LIKE+SUBSCRIBE end-card). Compose-layer code mixes concerns with the timeline mux; overlays plugin slot is the right architectural home; the dead-code path is a known landmine for the next refactor.
**Consequences:** New plugin file; `RenderSpec` gains `closer_panel` field (or reuses `chapter_cards`); compose.py lines 711-719 deleted.
**Future risks:** None obvious — straightforward migration to existing architecture.

---

## ADR-012: Caption density quality gate

**Date:** 2026-05-22 (Q73)
**Context:** Captions are produced post-hoc from Whisper alignment and overlaid in the compose stage. Silent failures (missing captions, frozen frames, Devanagari rendered as boxes) are the #2 reliability pain (Q9). No gate currently verifies that captions actually overlay the audio.
**Options:**
1. Hard-fail when caption import fails; trust the rest.
2. Add a post-render caption density check (% of audio time with caption overlay); hard-fail below threshold.
3. Both.
**Chosen:** Option 3.
**Rationale:** Caption import failures are the loud bug; density failures are the silent one. Both are needed. The density gate is a granular check — the threshold (e.g. 80% of audio covered) is per-channel tunable.
**Consequences:** New gate in compose post-step. Devanagari font installation moves into the worker Dockerfile (was a runtime miss). `spec.caption_style` is wired end-to-end (currently lossy through compose).
**Future risks:** Density gate could mis-fire on intentional silent gaps (b-roll, music-only sections). Mitigation: the gate compares to *audio time*, not *render time*; silent gaps don't count.

---

## ADR-013: Per-niche multi-source config in variant YAMLs

**Date:** 2026-05-22 (Q31, Q42, Q74)
**Context:** Today, source adapters are channel-level (`source_adapter: reddit_video` in `mystoriesanimated.yaml`). Multi-source mixing within a channel (e.g. `krishna_leela` wants both `wiki_mahabharat` and `scripture_text`; `aita_animated` wants just `reddit_aita`) isn't expressible. The user wants the rewrite stage to consume multiple raw sources and synthesize one engaging script.
**Options:**
1. Channel-level only (status quo).
2. Niche-level (per-variant YAML) declares its source adapters.
3. Per-render UI selection.
**Chosen:** Option 2.
**Rationale:** Channel-level is too coarse for the AITA-vs-TIFU-vs-Wiki-oddities split inside MyStoriesAnimated. Per-render UI selection (option 3) puts cognitive load on the operator every render. Niche-level config moves the decision to authoring-time and gets reused across N renders of that niche.
**Consequences:** `pipeline/variants/<channel>/<niche>.yaml` gains a `source_adapters: [list]` field. `rewrite` stage receives `raw_sources: [{adapter, payload}, ...]` and synthesizes across them in one LLM call (per ADR-014).
**Future risks:** Variant proliferation. Each new niche adds a YAML. Convention: only create a variant when the niche meaningfully differs in source mix, not just topic.

---

## ADR-014: Single LLM call for rewrite (pick + synthesize + write)

**Date:** 2026-05-22 (Q42, Q43, locked operating principle 3)
**Context:** The current shape mixes "picker LLM call" + "rewrite LLM call" + ad-hoc enhancement. Boundary is unclear. User's design intent: feed raw fetches in, get a finished engaging script out, one call.
**Options:**
1. Two-stage: picker LLM picks one of N raw items, rewrite LLM scripts it.
2. One-stage: rewrite LLM receives all N raw fetches + channel context + niche quality goals, internally picks + synthesizes + writes.
**Chosen:** Option 2.
**Rationale:** Two-stage hides the picker's quality bar from the rewriter — the picker may pick a thin source the rewriter then has to pad. One-stage gives the LLM full visibility: it sees what's on the menu AND knows what kind of script it needs to ship, so it can pick the source most likely to support that script. Aligns with the locked principle "LLM as editor on real material, not author from nothing."
**Consequences:** `pipeline/llm/rewrite.py` + `rewrite_long_form.py` accept `raw_sources: list` instead of `raw_source: dict`. Single LLM call. Internal selection logic moves into the prompt.
**Future risks:** Large source bundles bloat the input context. Cap raw source count at ~5 per render; if the adapter returns more, pre-truncate by recency or upvote score.

---

## ADR-015: Negative-framing avoidance in prompts

**Date:** 2026-05-22 (Q61, locked operating principle 5)
**Context:** "Do NOT pad with filler" + min-word floor in `_SECTION_BODY_PROMPT_TEMPLATE` interact poorly — LLMs read negative instructions weakly, the min-word floor pulls them toward padding, and the result is filler that violates the "no filler" line. Same pattern in other prompts.
**Options:**
1. Keep negative framing but emphasize it more ("CRITICAL: Do NOT pad").
2. Replace negative framing with positive specs of what good output looks like.
**Chosen:** Option 2.
**Rationale:** Verified in multiple LLM prompting literature ([MindStudio GPT-5 prompting](https://www.mindstudio.ai/blog/how-to-prompt-gpt-5-5-outcome-first-prompting), [readmedium word-count guide](https://readmedium.com/how-to-hit-exact-word-count-with-chatgpt-592ab179af00)) — positive framing outperforms negative for instruction following. Emphasizing the negative (option 1) often amplifies the unwanted behavior because the model anchors on the forbidden token.
**Consequences:** Outline LLM authors a per-section `quality_goal` ("escalate tension", "reveal twist") which the section-body prompt receives as positive guidance. "Do NOT pad with filler" block is removed.
**Future risks:** Quality goals as free text are an extra LLM-authored field that itself could be noisy. If a render's quality_goal field comes back generic ("write good content"), it adds no signal. Mitigation: track quality_goal diversity across renders; if every section gets the same string, flag the outline prompt.

---

## ADR-016: IndicF5 ref_audio_text bug — targeted fix

**Date:** 2026-05-22 (Q71)
**Context:** Hindi renders ship gibberish noise. Root cause located: `pipeline/tts/cloudrun.py:929` `_synth_cloudrun_indicf5` does not actually forward `ref_audio_text` to the IndicF5 model — the field is read from config but dropped before the request payload is assembled.
**Options:**
1. Switch Hindi to a different TTS provider.
2. Fix the targeted bug.
**Chosen:** Option 2.
**Rationale:** Model changes are out of scope per Q46. The bug is a one-line wiring miss, not a model limitation.
**Consequences:** Fix at `pipeline/tts/cloudrun.py:929`. Regression test that asserts `ref_audio_text` is in the request payload.
**Future risks:** None.

---

## ADR-017: All 7 channels must be functional (no hierarchy)

**Date:** 2026-05-22 (Q14, Q28, locked operating principle 8)
**Context:** Tempting MVP cut: pick one strong channel, ship it end-to-end, then expand. User explicitly rejects: every render must work for every channel; channel-creation must be a low-cost repeatable workflow because new channels will be added.
**Options:**
1. MVP = one channel reliably shipping; expand later.
2. MVP = all 7 channels reliably shipping.
**Chosen:** Option 2.
**Rationale:** User's stated product surface. The infrastructure differences between channels (TTS provider, source adapter, visual mode) are exactly the surface the system needs to *prove* it handles. Cutting to one channel hides the cross-channel bugs that are the actual problem.
**Consequences:** Ambitious MVP scope. Every reliability fix must clear the cross-channel test, not just one.
**Future risks:** Scope creep — adding a channel before the existing 7 work hides which fix broke which channel. Convention: scrollpulse YAML (ADR-018) is the cap; no new channels until the 7 ship cleanly.

---

## ADR-018: Add scrollpulse as the 7th channel

**Date:** 2026-05-22 (Q21-Q23)
**Context:** UI references `scrollpulse` in `web-next/app/app/create/page.tsx:567-575` (`FALLBACK_CHANNEL_KEYS`) but no YAML exists. User confirmed it IS a 7th channel: auto-pull Reddit threads + bottom-40% split-screen gameplay overlay (Subway Surfers / Minecraft parkour); top-40% is the Reddit thread card with TTS.
**Options:**
1. Leave it as a UI fallback; don't add the YAML.
2. Remove the UI reference.
3. Add the YAML, niche variants, branding.
**Chosen:** Option 3.
**Rationale:** User explicitly wants the channel.
**Consequences:** New `pipeline/channels/scrollpulse.yaml`. Niche variants (subreddit splits). Branding assets. A gameplay-loop asset library (not auto-generated).
**Future risks:** Gameplay overlays are a new compose-layer pattern (vertical split-screen) — distinct enough from the existing slideshow path that a new visualize plugin may be needed.

---

## ADR-019: Z-Image-Turbo verb-led prompt principle

**Date:** Prior session (carried into 2026-05-22 from MEMORY.md "Z-Image-Turbo verb-led prompts")
**Context:** Floating-object product photos appeared when prompts were noun-led ("a car, a tree, a person"). Z-turbo interprets these as still-life compositions.
**Options:**
1. Add post-hoc filters to discard product-photo outputs.
2. Lead with a verb ("a car driving through a forest"); structure as `[verb-led key_visual] + [named environment] + [lighting]`.
**Chosen:** Option 2.
**Rationale:** Output filter (option 1) is reactive; verb-led prompt fixes the input. Verbs imply action which implies camera + scene which implies environment.
**Consequences:** All beat prompts must start with a verb. Verifier in `pipeline/llm/critic.py`.
**Future risks:** Some legitimate static shots are noun-led ("a portrait of …"). Carve-out: allow noun-led when an explicit `style: portrait` is set.

---

## ADR-020: Channel richness gate

**Date:** Prior session (carried into 2026-05-22 from MEMORY.md "Channel richness gate")
**Context:** Some channel YAMLs shipped with thin style/character blocks; the rewriter and prompter under-extracted because the channel context was empty calories.
**Options:**
1. Hand-review per channel.
2. Hard gate: every channel YAML needs ≥60w style + ≥50w character with required token classes; enforced at `author_beat_prompts` entry.
**Chosen:** Option 2.
**Rationale:** Gate makes the bar enforceable and mechanical.
**Consequences:** Validator at channel-load time. Existing YAMLs sized accordingly.
**Future risks:** Token-class requirements (lighting, mood, palette, ...) can drift from current model preferences as we move to z-turbo (ADR-008); gate vocabulary needs a quarterly review against the chosen image model.

---

## ADR-021: Shape-C Azure structured-output array bug — wrap in object

**Date:** Prior session (carried into 2026-05-22 from MEMORY.md "Shape-C Azure structured-output array bug")
**Context:** Azure OpenAI rejects root-array `json_schema` (the schema must have `type: object` at the root). The cast stage was emitting a root-array schema.
**Options:**
1. Switch all schemas off `json_schema`.
2. Wrap arrays in an object: `{ items: [...] }`.
**Chosen:** Option 2.
**Rationale:** Targeted fix; preserves structured-output benefits.
**Consequences:** All schemas across `pipeline/llm/cli.py` shaped as object root. Test for the wrap.
**Future risks:** None.

---

## ADR-022: Render-worker env truth lives in deploy.sh

**Date:** Prior session (carried into 2026-05-22 from MEMORY.md "Render-worker env truth")
**Context:** `gcloud run jobs update --update-env-vars` writes succeed but are wiped on the next `deploy.sh` run because `deploy.sh` re-applies the full env set from its own `--set-env-vars` block.
**Options:**
1. Make `deploy.sh` source the live env (read-then-merge).
2. Treat `cloud/render-worker-v2/deploy.sh:90` as the only source of truth; never `gcloud run jobs update` env vars directly.
**Chosen:** Option 2.
**Rationale:** Read-then-merge introduces a state machine that drifts; deploy-time-truth is a simple, diffable, code-reviewable contract.
**Consequences:** Every env var change is a deploy.sh edit + redeploy. Documented.
**Future risks:** Operators reflexively use `gcloud run jobs update` and lose their change on the next deploy. Mitigation: a comment block at the top of deploy.sh + this ADR.

---

## ADR-023: Gates are repair triggers, not termination signals (operating principle)

**Date:** 2026-05-23 (Q51 + Q64 lock; promoted to a top-level operating-principle ADR)
**Context:** ADR-003 captured this for the specific case of long-form length gates. The 2026-05-22/05-23 session generalized the principle to every gate in the system: per-beat image-gen failure (`_PER_BEAT_FAILURE_THRESHOLD=0.10`), TTS chunk failure, writeback duration mismatch, caption density, outline sum-check, post-image-quality validator. Q64 lock: "Gates should hit and retry, that's it."
**Options:**
1. Keep ADR-003 scoped to long-form rewrite only; treat each gate's policy independently.
2. Promote to a system-wide operating principle: every gate in the pipeline follows the granular-retry-then-raise shape.
**Chosen:** Option 2.
**Rationale:** The session brainstorm (Q49–Q73) repeatedly hit the same shape — every gate site is currently a whole-render terminator; every fix moves it to a piece-level retry. Naming the principle once at the ADR layer prevents each future gate from having to re-derive it. It also locks the contract: any new gate must declare its smallest retriable unit AND its retry cap before it can land.
**Consequences:** Every gate site needs three properties: smallest retriable unit (section / beat / chunk), retry cap (1 or 2), terminal action (raise + propagate). Cross-cutting framework opportunity (see improvement-opportunities O22) but ADR-level commitment is independent of whether the framework lands.
**Future risks:** "Granular retry" can spread to legitimate kill cases (truly unrecoverable errors), masking them as retry-exhausted gate fires. Mitigation: terminal action MUST be `raise`, not silent fallback — the gate still kills the render after retry exhaustion (same end state as today, just only after the retry actually tried).

---

## ADR-024: Z-Image-Turbo is the canonical production image model

**Date:** 2026-05-23 (Q67-Q68 lock)
**Context:** ADR-008 captures the refiner replacement decision. This ADR captures the underlying canonical-model decision the refiner replacement flows from. All 6 (now 7) channel YAMLs set `image_provider: cloudrun_z_image_turbo`. FLUX.2 klein has no live `cloud/image-flux2-klein/` deploy.sh; the env URL in `cloud/render-worker-v2/deploy.sh:106` references a dead service. Multiple sibling artifacts (refiner module docstring, channel YAML comments, README, CLAUDE.md fragments) still reference klein as if it were a live option.
**Options:**
1. Keep klein as a "supported alternate" — preserve refiner backward-compat, keep env URLs, keep docs ambiguous.
2. Declare Z-Image-Turbo the canonical model. Delete klein artifacts (refiner backward-compat, dead env URLs, docstring/README references). Any future alternate model gets re-introduced via a deliberate per-channel switch when that need arises.
**Chosen:** Option 2.
**Rationale:** Klein is not deployed and not used by any channel. Carrying klein-shaped artifacts (4-10 word refiner output, BFL composition vocabulary, negative-prompt blocks that z-turbo silently ignores) is actively harmful — it shapes every adjacent decision around a model that isn't running. Single-target is also the locked operating principle (onboarding-qa principle 6: "Z-Image-Turbo is the production image model (not FLUX.2 klein)").
**Consequences:** Refiner rewrite (ADR-008). 4 dead env URLs removed from `deploy.sh:106` (flux2-klein, flux2-dev, qwen, indicparler — Phase 3 of refactor-plan.md). Channel YAML comments + docs reflect single target. Any new image-gen channel defaults to z-turbo unless explicitly justified.
**Future risks:** A future channel may need a different model (cost / style / latency). Re-introduce it via a deliberate per-channel `image_provider` switch + its own ADR. Don't pre-build a multi-model abstraction for hypothetical future use.

---

## ADR-025: LLM as editor on real material, not author from nothing

**Date:** 2026-05-23 (Q42 lock; locked operating principle 2)
**Context:** Multiple session entries (Q40–Q42) revealed the user's intent for the `rewrite` stage: ingest real fetched material (Reddit thread, Wiki article, archive entry) and produce an engaging script by editing / synthesizing / enhancing — NOT by hallucinating a script from a topic prompt. Current `rewrite_long_form` is at risk of drifting toward author-from-nothing for thin source material because the prompt allows it.
**Options:**
1. Allow the LLM to "fill in" when sources are thin (author-from-nothing fallback).
2. Make the editor-on-real-material contract explicit and load-bearing: rewrite always receives raw sources; thin sources → retry adapter / fetch more / raise; never hallucinate.
**Chosen:** Option 2.
**Rationale:** Author-from-nothing produces stories that don't exist (factually wrong) and stories that aren't grounded (boring). Editor-on-real-material aligns LLM strengths (synthesis, voice, pacing) with adapter strengths (real-world facts, freshness). Locked operating principle.
**Consequences:** Rewrite prompts require raw source payloads. Adapters must surface "no usable source" as a hard error, not as empty input. ADR-026 (one LLM call) is the implementation shape; ADR-013 (per-niche multi-source config) is the source-side extension; ADR-014 (single rewrite call shape) is the call-side commitment.
**Future risks:** Hard-error on no-source can make a channel unrenderable during source outages (Reddit down, Wiki rate-limited). Mitigation: the adapter contract allows fallback adapters per niche (per ADR-013), not LLM hallucination. If all adapters fail, the render fails — that's correct; channel-creation workflow can pre-stage a manual-source path for emergency renders.

---

## ADR-026: One LLM call combines pick + synthesize + write (no separate picker stage)

**Date:** 2026-05-23 (Q43 lock; locked operating principle 3)
**Context:** ADR-014 already covered this for the multi-source case. This ADR generalizes: even for single-source channels, no separate "picker LLM" stage exists. The rewrite LLM receives raw_source(s) + channel + niche context + quality goals and produces the finished script in one call.
**Options:**
1. Two-stage: picker LLM picks one of N raw items, rewrite LLM scripts the chosen item.
2. One-stage: rewrite LLM sees the raw material + script-quality goals in one call; picks + synthesizes + writes internally.
**Chosen:** Option 2.
**Rationale:** Same as ADR-014's logic, lifted to a system-wide rule. A separate picker is a separate cost + latency + failure surface; it also hides the script-quality bar from the picker, leading to "I picked the wrong source for what I needed to write." One-stage is cheaper, fewer-failure-modes, and gives the LLM full visibility.
**Consequences:** `pipeline/llm/rewrite.py` + `rewrite_long_form.py` accept `raw_sources: list[{adapter, payload}]`. Selection logic lives in the prompt. No `picker.py` stage exists or should exist. Test rule: any new content channel routes through the same one-call shape.
**Future risks:** Large source bundles bloat the input context (multiple Reddit threads + multiple Wiki articles can blow past sane context windows). Mitigation: cap raw source count at ~5 per render; adapters pre-truncate by recency / upvote / relevance.

---

## ADR-027: `render_via_engines` is the canonical render entry; legacy `render()` will be deleted

**Date:** 2026-05-23 (Phase 2 of refactor-plan.md)
**Context:** `pipeline/render/video.py:81` exports a legacy `render()` function that raises `NotImplementedError` for `kind=short` (line 116) and dispatches to `render_long_form()` for `kind=long_form`. The bigbang absorbed everything into `render_via_engines()` at line 128. The cloud worker uses `render_via_engines` directly. The legacy `render()` is dead code with an active footgun: module docstring (lines 1-43) still describes the pre-bigbang Slice 2 state. Known-fragility F1, F15 and tech-debt D4, D7 reference this.
**Options:**
1. Keep both entry points indefinitely (back-compat hedge).
2. Mark `render()` deprecated, delete in a future cycle.
3. Migrate any remaining callers + delete legacy `render()` this refactor.
**Chosen:** Option 3 (Phase 2 + Phase 3 of refactor-plan.md).
**Rationale:** Two entry points violate source-of-truth (charter principle 2). The legacy path has a misleading docstring and a NotImplementedError for the most-used kind. Deprecation-without-deletion (option 2) is the worst outcome — it preserves the footgun while pretending it's not a footgun.
**Consequences:** Phase 2 migrates callers (mostly already done — worker already uses `render_via_engines`). Phase 3 deletes the legacy function + the supporting `_extract_last_traceback` machinery (D5) + the subprocess plumbing (D15). Module docstring rewritten (D7) to describe the engine dispatch shape.
**Future risks:** A test or script we didn't grep still imports `render()`. Mitigation: Phase 2 grep is exhaustive (`pipeline.render.video.render(` across the repo + tests/); Phase 5 smoke test catches any miss.

---

## ADR-028: `control/core/*` is the canonical state-module location

**Date:** 2026-05-23 (Phase 2 of refactor-plan.md)
**Context:** 7 file pairs exist with diverged logic: `control/{jobs,queue,scheduler,rate_limit,storage,schema,auth}.py` vs `control/core/*.py`. 5 route-file pairs also exist: `control/{agent,dashboard,niche,render,scheduler}_routes.py` vs `control/routes/*`. Production `web/server.py:1792-1813` imports ONLY from `control.routes.*` and `control.core.*`. Laptop dev `control/server_dev.py:24-39` imports BOTH. Known-fragility F10, tech-debt D1.
**Options:**
1. Pick `control/*` (flat) as canonical; delete `control/core/*` + `control/routes/*`.
2. Pick `control/core/*` + `control/routes/*` (nested) as canonical; delete flat siblings.
3. Keep both; accept fork drift.
**Chosen:** Option 2.
**Rationale:** Production already imports the nested path (`web/server.py`). The migration is half-done in that direction. Reversing would mean re-migrating production. The nested layout also matches the engine module organization (`pipeline/render/{audio,timeline,visualize,overlays,music,compose}/`) — consistency.
**Consequences:** Phase 2 migrates `control/server_dev.py` to import only from nested paths. Phase 3 deletes the 5 flat route files + 7 flat state modules. Test imports update accordingly. Diverged logic is merged into nested (the diverged half is reviewed line-by-line; whichever side is correct wins).
**Future risks:** Diverged logic between flat and nested may include bug fixes only on one side. Phase 2's "diff `include_router(...)` sets" check catches surface-level drift. Logic-level drift (a fix in `control/jobs.py` not in `control/core/jobs.py`) requires line-level diff review during the merge.

---

## ADR-029: All 7 channels MVP target; scrollpulse added as the 7th

**Date:** 2026-05-23 (Q14 + Q21-Q23 + Q28; locked operating principle 8)
**Context:** ADR-017 covered "all 7 channels must be functional, no hierarchy." ADR-018 covered "add scrollpulse as the 7th channel." This ADR consolidates them into the locked MVP shape: MVP = all 7 channels (mystoriesanimated, cosmosdecoded, historyrecapped, hindutavaanimated, sportsrecapped, rhymetimejunction, scrollpulse-NEW) reliably producing finished mp4s end-to-end without manual intervention.
**Options:**
1. MVP = one channel reliably shipping; expand later.
2. MVP = subset (e.g., the 6 existing) shipping; scrollpulse added post-MVP.
3. MVP = all 7 channels (the 6 + scrollpulse) shipping.
**Chosen:** Option 3.
**Rationale:** Q14 + Q28 locked. The cross-channel infrastructure (TTS provider routing, source adapter routing, visual mode routing) is exactly the surface the system needs to prove it handles. Cutting MVP scope hides cross-channel bugs that are the actual reliability problem. scrollpulse is referenced in the UI's `FALLBACK_CHANNEL_KEYS` (page.tsx:567-575) — partial rollout is worse than no rollout.
**Consequences:** Every reliability fix in Phase 1 of refactor-plan.md must clear the cross-channel test, not just one channel. Phase 6 documents `make-channel` as a repeatable workflow because new channels will be added. Phase 5 smoke = 7 Shorts + 4 long-forms = 11 successful renders before MVP is declared.
**Future risks:** New channels added before the 7 ship cleanly hide which fix broke which channel. Convention: scrollpulse is the cap; no new channels until the 7 ship.

---

## ADR-030: `reasoning_effort=medium` for length-sensitive rewrites in `rewrite_long_form`

**Date:** 2026-05-23 (Q57.4 lock; reverts 2026-05-13 minimal calibration)
**Context:** On 2026-05-13, `_DEFAULT_REASONING_EFFORT_BY_STAGE["rewrite_long_form"]` at `pipeline/llm/cli.py:318` was downgraded to `"minimal"` to save tokens. Q57 research surfaced that minimal-effort models can't count their own words while emitting — which is exactly the failure mode that produced jobs `a734babb` (section 7 = 14% of mean) and `7743ca76` (mp4 1232s < 1440s required). The token-budget headroom is already at 64k (line 220), so token savings from minimal-effort don't bind.
**Options:**
1. Keep `minimal` (preserve the 2026-05-13 cost optimization).
2. Flip back to `medium`.
3. Flip to `high` (max length-following at max cost).
**Chosen:** Option 2.
**Rationale:** Cost savings from `minimal` don't bind (64k headroom). The cost is shipping shorter narrations than instructed, which trips downstream gates and burns a whole render's worth of TTS / image-gen / compose spend on a re-run. `medium` is the canonical default; `high` is overkill for the instruction-following gap we observed (Q57 research did not show `high` materially outperforming `medium` on length following).
**Consequences:** One-constant change at `pipeline/llm/cli.py:318`. Verify max_tokens budget at line 220 still has headroom (yes — 64k). Add a test that pins `_DEFAULT_REASONING_EFFORT_BY_STAGE["rewrite_long_form"] == "medium"` so a future cost-pressure flip back to minimal lands as a deliberate revert with its own ADR, not a quiet edit.
**Future risks:** Future cost pressure may push to revert. Mitigation: the pinning test makes the revert visible; the ADR makes the rationale recoverable.

---

## ADR-031: `anthropic_sdk` LLM backend is a documented escape hatch, not vestigial

**Date:** 2026-05-23 (R7 audit, /ai/prd.md post-discovery walk)
**Context:** The 2026-05-23 vestigial audit recommended collapsing `pipeline/llm/cli.py` from 3 backends to 2 by removing the `anthropic_sdk` branch — under the charter's "fewer concepts" / "abstractions without need" rule. R7 of the post-discovery R-task sweep re-audited the claim before deletion. Evidence found: the backend has 5 dedicated unit tests in `test_pipeline_llm.py`, the dispatch branch in `_choose_backend` (`pipeline/llm/cli.py:67`) is reachable, AND the operator runbook in `cloud/render-worker-v2/deploy.sh:134-137` documents how to switch a live worker to it (`YTFACTORY_LLM_BACKEND=anthropic_sdk` + `--update-secrets ANTHROPIC_API_KEY=...`). Cost-fallback semantic: when Azure OpenAI is unavailable / quota-exhausted, the operator can flip the worker to pay-per-token Anthropic on a separate billing line without redeploying.
**Options:**
1. Delete `_call_anthropic_sdk` + the backend dispatch branch (vestigial-audit recommendation).
2. Keep the backend as a documented escape hatch. Mark it in the source so it's not re-audited as vestigial.
3. Keep the backend AND promote it to a routinely-exercised path (e.g. swap one channel onto it).
**Chosen:** Option 2.
**Rationale:** The backend has all three properties the charter requires for keeping an abstraction: a current consumer (the runbook), a documented purpose (cost fallback / Azure outage), and tests proving it works. Deleting it loses optionality the system pays nothing to keep — the dispatch branch is ~50 lines and the tests are owned. Promoting it (option 3) introduces a parallel billing surface for no current benefit. The right shape is "tested, runbook-documented fallback" — code stays, default in prod stays `azure_openai`, anthropic_sdk fires only when the operator explicitly toggles.
**Consequences:** No code change. The vestigial audit's recommendation is overruled with this ADR as the rationale. Source-of-truth doc reflects all 3 backends are intentional. Mental-model docs no longer call the dispatcher "potentially vestigial."
**Future risks:** Maintenance drift: the anthropic_sdk path may rot if no one exercises it for months. Mitigation: the 5 unit tests catch surface-level rot; quarterly the operator should run a smoke render with the flag flipped to confirm end-to-end works. Tracked as a recurring chore, not part of MVP.

---

## ADR-032: Plugin system 6-slot abstraction stays

**Date:** 2026-05-23 (R8 audit, /ai/prd.md post-discovery walk)
**Context:** The 2026-05-23 vestigial audit recommended collapsing the 6-slot plugin abstraction (`pipeline/render/contracts.py`) — specifically the three "kind-discriminated" slots (audio, timeline, compose) where each slot has exactly 2 impls (one for short, one for long). The audit argued: if a slot is just "pick by `spec.kind`", replace `get_plugin(slot, name)` with `if spec.kind == SHORT: ... else: ...` and inline the calls. R8 of the post-discovery R-task sweep re-audited the claim.
**Options:**
1. Collapse the 3 kind-discriminated slots (audio/timeline/compose) to direct imports + if/else discriminators in each engine.
2. Collapse all 6 slots — including the spec-driven ones (visualize/overlays/music).
3. Keep all 6 slots. Document the per-slot abstraction earns-its-keep evidence.
**Chosen:** Option 3.
**Rationale:** Re-audit found: every slot has 2+ impls (audio: tts_single + tts_chunked + fixture; timeline: asr_beats + asr_anchors + fixture; compose: beat_slideshow_mux + section_video_mux). The slot is a discriminator on `spec.kind`, not a runtime user choice — but the abstraction lets the long-engine's `tts_chunked` ride the same `AudioSynthesizer` Protocol as the short-engine's `tts_single`, and the same for timeline + compose. Collapsing to direct imports would re-implement the `if spec.kind == SHORT: ... else: ...` discriminator at each engine site (3 if/else blocks per engine = 6 total). Net LoC: zero. Coupling: higher (engine modules now hold the dispatch logic rather than reading it from the registry). Testability: lower (can't swap impl by name in tests; have to monkey-patch the import). The other three slots (visualize / overlays / music) are spec-driven multi-impl and obviously need the registry. The 6-slot abstraction is justified per the charter's "every abstraction must solve a real problem" — the problem solved is uniform engine dispatch and testable fixture-impl substitution.
**Consequences:** No code change. The vestigial audit's recommendation is overruled with this ADR as the rationale. Source-of-truth doc reflects all 6 slots are intentional. Future ADRs may revisit if any slot collapses to exactly one impl AND fixture impls become un-needed.
**Future risks:** Adding new engines (e.g. a `realtime_stream` engine) would multiply impls per slot — the abstraction scales with new render kinds. Removing engines (collapsing long+short into one) would invert the calculus — re-audit when that happens.

