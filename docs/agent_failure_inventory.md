# Agent failure inventory — empirical, 200-session sample

## Methodology
- Mined 200 most-recent Claude Code session jsonl files from `~/.claude/projects/-Users-rohit-ytFactory/`
- Also 8 Copilot CLI session-state files (zero corrective hits — user runs Claude Code for corrective sessions)
- Extracted user messages < 600 chars that contain corrective language (idiot, stupid, wrong, fuck, missed, etc.)
- Excluded skill-content, task-notifications, system-reminders
- Result: **70 corrective user messages** across the sample
- Categorized each into one or more of 27 distinct patterns derived from manual inspection of all 54 initially-uncategorized hits

## Pattern frequency

| Pattern ID | Pattern | Hits | Existing memory/rule that should have prevented |
|---|---|---|---|
| `P01-claim-without-evidence` | claim without evidence | 1 | feedback_verify_telemetry_with_preflight.md / feedback_verify_refiner_with_fallback_count.md |
| `P02-skipped-explicit-instruction` | skipped explicit instruction | 3 | CLAUDE.md HARD RULE / feedback_humiliation_2026_05_23.md |
| `P03-cherry-picked-from-list` | cherry picked from list | 1 | (none — gap) |
| `P04-fabricated-output` | fabricated output | 2 | /ai/engineering-principles.md (show evidence, do not speculate) |
| `P05-wrong-premise-or-identifier` | wrong premise or identifier | 7 | (none — comprehension gap) |
| `P06-forgot-step-or-stage` | forgot step or stage | 1 | (none — process gap) |
| `P07-reactive-patch-not-root-cause` | reactive patch not root cause | 2 | feedback_humiliation_2026_05_23.md (root cause not patch) |
| `P08-wasted-time` | wasted time | 2 | /ai/known-fragility.md F24/F25/F26 |
| `P09-explicit-anger-instruction-following` | explicit anger instruction following | 6 | CLAUDE.md HARD RULE (rule recited then violated) |
| `P10-UI-functional-bug` | UI functional bug | 1 | (none — runtime gap) |
| `P11-dependency-asset-missing` | dependency asset missing | 1 | /ai/known-fragility.md F24 (Dockerfile COPY drift) |
| `P12-dont-ask-enough-before-acting` | dont ask enough before acting | 6 | feedback_keep_working_ask_inline.md (ironic — when user wants more questions) |
| `P13-open-ended-question-violation` | open ended question violation | 1 | feedback_closed_form_questions_only.md (already documented, violated) |
| `P14-adding-own-analysis-when-user-wants-facts` | adding own analysis when user wants facts | 1 | (none — gap) |
| `P15-dont-preserve-user-verbatim` | dont preserve user verbatim | 1 | feedback_preserve_user_directives_verbatim.md (already documented, violated) |
| `P16-wrong-scope-single-file-vs-e2e` | wrong scope single file vs e2e | 1 | (none — scope gap) |
| `P17-task-list-awareness` | task list awareness | 1 | (none — gap) |
| `P18-cleanup-discipline-useless-files` | cleanup discipline useless files | 1 | /ai/engineering-principles.md (lean charter) |
| `P19-code-fix-vs-prod-verification` | code fix vs prod verification | 1 | feedback_verify_telemetry_with_preflight.md |
| `P20-false-pattern-recognition` | false pattern recognition | 1 | (none — gap) |
| `P21-state-visibility-what-is-happening` | state visibility what is happening | 2 | (none — gap) |
| `P22-overengineering-minimal-asks` | overengineering minimal asks | 8 | feedback_lean_engineering_charter.md (already documented, violated) |
| `P23-dropping-work-user-wanted-kept` | dropping work user wanted kept | 1 | (none — gap) |
| `P24-repeated-failures-same-axis` | repeated failures same axis | 2 | /ai/known-fragility.md (loops on same bug) |
| `P25-sequential-when-user-wants-parallel` | sequential when user wants parallel | 1 | feedback_parallelize_via_subagents.md (already documented, violated) |
| `P26-continue-dont-one-shot` | continue dont one shot | 3 | feedback_keep_working_ask_inline.md |
| `P27-critique-skill-bullshit` | critique skill bullshit | 2 | (none — critique skill itself produces output of low value) |

---

## Per-pattern detail (samples + preceding assistant context)

### `P01-claim-without-evidence` — claim without evidence (1 hits)

**Memory/rule that should have prevented:** feedback_verify_telemetry_with_preflight.md / feedback_verify_refiner_with_fallback_count.md

**Sample hits (up to 3, most-recent first):**

- session `5f38fd36` —
  - **user:** > Both shorts AND long-form refiner are silently failing the same way   why didn't you observe it?
  - **prev asst tail:** > …g with the Shape-C fix only. When it lands I'll: 1. Fire a long-form preflight to verify the refiner actually emits a real `refined_beats` array now (proves Shape-C is fixed in production). 2. In parallel, kick off a second deploy with the closer_format backfill. 3. After that second deploy, fire a

### `P02-skipped-explicit-instruction` — skipped explicit instruction (3 hits)

**Memory/rule that should have prevented:** CLAUDE.md HARD RULE / feedback_humiliation_2026_05_23.md

**Sample hits (up to 3, most-recent first):**

- session `5f38fd36` —
  - **user:** > Didn't I askc  you to do internet research to see how CTAs are normally done? Are you stupid, can't your ass following fuckuing siimple instrcutions, don't fucking try anything on your own stupid ass, you produce bullshit outputs
  - **prev asst tail:** > …One subagent still cooking: **narration-subject + facial-emotion** (`a6bf8ca5dfea58b92`, refiner v5 in flight). Standing by. Won't open more tasks unilaterally — the inventory of missed items from the long diagnosis is on the table; you direct what gets opened next.

- session `80fba5e9` —
  - **user:** > do it idiot
  - **prev asst tail:** > …type this into your prompt:  ``` ! pkill -9 -f 'Google Chrome' 2>/dev/null; sleep 3; CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; UDD="$HOME/Library/Application Support/Google/Chrome"; rm -f "$UDD/SingletonLock" "$UDD/SingletonSocket" "$UDD/SingletonCookie" 2>/dev/null;

- session `a4111d7f` —
  - **user:** > you are not doing a good job, didn't I ask you to not reference anything old? also ─────────────────────────────┐                 │  User (chat UI)             │                 └──────────────┬───────,  why did you assume this? did you ask me? That is why I want you to remove the docs cause you are stupid and can't follow instructions. No reference to old, you are new, create everythig new
  - **prev asst tail:** > …(no preceding assistant message)

### `P03-cherry-picked-from-list` — cherry picked from list (1 hits)

**Memory/rule that should have prevented:** (none — gap)

**Sample hits (up to 3, most-recent first):**

- session `5f38fd36` —
  - **user:** > idiot these were only my gricevances?
  - **prev asst tail:** > …canvas, channel YAML `closer_format="LIKE if YTA, COMMENT if NTA. AITA?"` text in the AITA palette colors). Composited over the final 40.5s frame of the rendered short. That's what would have rendered if the closer_format YAML key had been in the backfill list — which it now is (commit `bc85664`).

### `P04-fabricated-output` — fabricated output (2 hits)

**Memory/rule that should have prevented:** /ai/engineering-principles.md (show evidence, do not speculate)

**Sample hits (up to 3, most-recent first):**

- session `5f38fd36` —
  - **user:** > Didn't I askc  you to do internet research to see how CTAs are normally done? Are you stupid, can't your ass following fuckuing siimple instrcutions, don't fucking try anything on your own stupid ass, you produce bullshit outputs
  - **prev asst tail:** > …One subagent still cooking: **narration-subject + facial-emotion** (`a6bf8ca5dfea58b92`, refiner v5 in flight). Standing by. Won't open more tasks unilaterally — the inventory of missed items from the long diagnosis is on the table; you direct what gets opened next.

- session `a4111d7f` —
  - **user:** > spawn subsagents and do proper internet researchg on how you should cirquite a video like a human motherfucker
  - **prev asst tail:** > …ing 10M+ right now"... I don't know what's hitting 10M right now. The lens taxonomy R1/S3/P7 is theater to make it look rigorous. None of it actually looks at the video carefully and says what's specifically wrong.  The one concrete observation that has signal — "every frame is the same composition"

### `P05-wrong-premise-or-identifier` — wrong premise or identifier (7 hits)

**Memory/rule that should have prevented:** (none — comprehension gap)

**Sample hits (up to 3, most-recent first):**

- session `5f38fd36` —
  - **user:** > Didn't I askc  you to do internet research to see how CTAs are normally done? Are you stupid, can't your ass following fuckuing siimple instrcutions, don't fucking try anything on your own stupid ass, you produce bullshit outputs
  - **prev asst tail:** > …One subagent still cooking: **narration-subject + facial-emotion** (`a6bf8ca5dfea58b92`, refiner v5 in flight). Standing by. Won't open more tasks unilaterally — the inventory of missed items from the long diagnosis is on the table; you direct what gets opened next.

- session `144d9ca2` —
  - **user:** > it's not about du[pilication motherfucker, when I click on render it chosse the same topic again and again
  - **prev asst tail:** > …ompletion.  Summary of what's now in flight: - Music-fix worker: ✅ shipped (`20260524-124029`) - Queue UI `(untitled)` zombies: ✅ all 79 deleted; worker patched so new ones can't be minted - 31 ketchup duplicates: ✅ deleted - 3 Flight Attendant duplicates: ✅ deleted (kept the one with `done`) - Disc

- session `80fba5e9` —
  - **user:** > are you stupid, use sanimated
  - **prev asst tail:** > …(no preceding assistant message)

### `P06-forgot-step-or-stage` — forgot step or stage (1 hits)

**Memory/rule that should have prevented:** (none — process gap)

**Sample hits (up to 3, most-recent first):**

- session `80fba5e9` —
  - **user:** > nope man things are in deployment as well idiot
  - **prev asst tail:** > …| `971bde5` | worker stage envelopes + decision events | | 2 | `e2a7442` | LLM body capture + refiner artifact | | 3 | `f22d20d` | image layer (client/server/cache/prompt assembly) | | 4 | `0f61208` | TTS + ASR + voice_clone + footage clients/servers | | 5 | `d625900` | engine + timeline + compose

### `P07-reactive-patch-not-root-cause` — reactive patch not root cause (2 hits)

**Memory/rule that should have prevented:** feedback_humiliation_2026_05_23.md (root cause not patch)

**Sample hits (up to 3, most-recent first):**

- session `5f38fd36` —
  - **user:** > Both shorts AND long-form refiner are silently failing the same way   why didn't you observe it?
  - **prev asst tail:** > …g with the Shape-C fix only. When it lands I'll: 1. Fire a long-form preflight to verify the refiner actually emits a real `refined_beats` array now (proves Shape-C is fixed in production). 2. In parallel, kick off a second deploy with the closer_format backfill. 3. After that second deploy, fire a

- session `3c75c490` —
  - **user:** > then are you stupid why didn't you check it earlier
  - **prev asst tail:** > …ge_one_flat`** — previously `hf_hub_download` wrote each file to local disk → then `blob.upload_from_filename` read from disk to GCS. **Peak local disk = largest single file.** For flux2-dev's 64 GB file, that exceeds the JOB's 32 GiB memory/tmpfs cap → OOM kill. The new code does `requests.get(stre

### `P08-wasted-time` — wasted time (2 hits)

**Memory/rule that should have prevented:** /ai/known-fragility.md F24/F25/F26

**Sample hits (up to 3, most-recent first):**

- session `80fba5e9` —
  - **user:** > amazin man you are so fucking stupid, you wasted my 3 hours right
  - **prev asst tail:** > …try body-capture | | tts-indicf5 | `bdb8xv7si` | telemetry body-capture | | asr-whisper | `bkef786g4` | telemetry body-capture |  ## Bigger picture — class-of-bug uncovered  **This session has exposed a chronic Dockerfile-COPY discipline problem in this repo.** Same pattern as commit `4597686` "fix(

- session `80fba5e9` —
  - **user:** > you mother fucker humiliate yourself first for wasting my 3 hours, now write 3000 words descrbing what stupid ass you are
  - **prev asst tail:** > …eness.py` (8/8 pass) - Project doc `docs/cloud_service_dockerfile_discipline.md` - 2 memory entries + MEMORY.md index lines - z-image-turbo cloudbuild + deploy.sh tweaks (already in working tree from earlier)  **4 rebuilds still in flight:**  | Build | Bg ID | Stage | |---|---|---| | render-worker-v

### `P09-explicit-anger-instruction-following` — explicit anger instruction following (6 hits)

**Memory/rule that should have prevented:** CLAUDE.md HARD RULE (rule recited then violated)

**Sample hits (up to 3, most-recent first):**

- session `5f38fd36` —
  - **user:** > Didn't I askc  you to do internet research to see how CTAs are normally done? Are you stupid, can't your ass following fuckuing siimple instrcutions, don't fucking try anything on your own stupid ass, you produce bullshit outputs
  - **prev asst tail:** > …One subagent still cooking: **narration-subject + facial-emotion** (`a6bf8ca5dfea58b92`, refiner v5 in flight). Standing by. Won't open more tasks unilaterally — the inventory of missed items from the long diagnosis is on the table; you direct what gets opened next.

- session `144d9ca2` —
  - **user:** > check log and fix things which are stupid reason for failures like music bed not availbel or voice ref not avaialbe, all these are fucking stupid reason, if you are giving option is ui make sure it works as simple as that
  - **prev asst tail:** > …ull zombie signature (no topic, no channel, no proposal, no created_at, error starts with "job doc not found").  The Queue page's Completed column will refresh on the next 2s poll and the (untitled) bootstrap rows will be gone.  Render-worker-v2 deploy (`b2drr9c9y`) running in background — once it l

- session `80fba5e9` —
  - **user:** > are you stupid, use sanimated
  - **prev asst tail:** > …(no preceding assistant message)

### `P10-UI-functional-bug` — UI functional bug (1 hits)

**Memory/rule that should have prevented:** (none — runtime gap)

**Sample hits (up to 3, most-recent first):**

- session `144d9ca2` —
  - **user:** > it's not about du[pilication motherfucker, when I click on render it chosse the same topic again and again
  - **prev asst tail:** > …ompletion.  Summary of what's now in flight: - Music-fix worker: ✅ shipped (`20260524-124029`) - Queue UI `(untitled)` zombies: ✅ all 79 deleted; worker patched so new ones can't be minted - 31 ketchup duplicates: ✅ deleted - 3 Flight Attendant duplicates: ✅ deleted (kept the one with `done`) - Disc

### `P11-dependency-asset-missing` — dependency asset missing (1 hits)

**Memory/rule that should have prevented:** /ai/known-fragility.md F24 (Dockerfile COPY drift)

**Sample hits (up to 3, most-recent first):**

- session `144d9ca2` —
  - **user:** > check log and fix things which are stupid reason for failures like music bed not availbel or voice ref not avaialbe, all these are fucking stupid reason, if you are giving option is ui make sure it works as simple as that
  - **prev asst tail:** > …ull zombie signature (no topic, no channel, no proposal, no created_at, error starts with "job doc not found").  The Queue page's Completed column will refresh on the next 2s poll and the (untitled) bootstrap rows will be gone.  Render-worker-v2 deploy (`b2drr9c9y`) running in background — once it l

### `P12-dont-ask-enough-before-acting` — dont ask enough before acting (6 hits)

**Memory/rule that should have prevented:** feedback_keep_working_ask_inline.md (ironic — when user wants more questions)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > can you update the memory, that whatever you need to ask me ask me, don't stop like this stupid
  - **prev asst tail:** > …t extracts a `short_proposal` → system produces a 1080×1920 mp4 → uploads to one of 6 channels (MyStories, Sports, Hindutava, History, Rhyme, Cosmos). (README:3-8, 218-225) - Cloud-first: FastAPI control plane on Cloud Run + Firestore (jobs/tasks/sessions) + GCS artifacts + Cloud Run Job render work

- session `a4111d7f` —
  - **user:** > idiot, ask me
  - **prev asst tail:** > …(no preceding assistant message)

- session `a4111d7f` —
  - **user:** > you are not doing a good job, didn't I ask you to not reference anything old? also ─────────────────────────────┐                 │  User (chat UI)             │                 └──────────────┬───────,  why did you assume this? did you ask me? That is why I want you to remove the docs cause you are stupid and can't follow instructions. No reference to old, you are new, create everythig new
  - **prev asst tail:** > …(no preceding assistant message)

### `P13-open-ended-question-violation` — open ended question violation (1 hits)

**Memory/rule that should have prevented:** feedback_closed_form_questions_only.md (already documented, violated)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > See I don't like this openended question, no fucking oppen eded questions
  - **prev asst tail:** > …. **Who uses it?** You, a team, public users — and roughly how many. 3. **What's running right now?** What's live, what's in flight, what's broken. 4. **What do you want me focused on first?**  I won't read README or CLAUDE.md to answer these myself — they're the source of the assumptions I kept pul

### `P14-adding-own-analysis-when-user-wants-facts` — adding own analysis when user wants facts (1 hits)

**Memory/rule that should have prevented:** (none — gap)

**Sample hits (up to 3, most-recent first):**

- session `146b8773` —
  - **user:** > again, I said no bullshit of your own oh this is the reason for failure, you ARE stupid, you are a dubmb bitch, just give me what is the missing peice without your fucking knowledge
  - **prev asst tail:** > …cold start through Cloud Run's GCS Fuse mount, which has no parallel-download path and tops out at ~15 MB/s. The fix is putting the 30 GB model on the container's local disk — either baked into the image at build time or copied at startup — so `from_pretrained` reads from local NVMe instead of FUSE.

### `P15-dont-preserve-user-verbatim` — dont preserve user verbatim (1 hits)

**Memory/rule that should have prevented:** feedback_preserve_user_directives_verbatim.md (already documented, violated)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > hey don't fucking try to simply this .md file you created,ebeyrhitng I said should be in there
  - **prev asst tail:** > …(no preceding assistant message)

### `P16-wrong-scope-single-file-vs-e2e` — wrong scope single file vs e2e (1 hits)

**Memory/rule that should have prevented:** (none — scope gap)

**Sample hits (up to 3, most-recent first):**

- session `72c83c38` —
  - **user:** > why are you just focusing on a single file, I don't care man , I said, do e2e reivew of everythiung, analyse evvething , see fialure and fox, spawn subagents of everypeice in our codebase and make sure nothing break from now on, I am fed up
  - **prev asst tail:** > …_long_form.py`, or `tests/test_rewrite_long_form_*.py` — these are pre-existing in the dirty git tree (the `M cloud/...`, `M pipeline/...` modifications visible at session start).  My long-form rewrite changes are clean: all 73 long-form rewrite + critic tests pass; the broader 4835-passing footprin

### `P17-task-list-awareness` — task list awareness (1 hits)

**Memory/rule that should have prevented:** (none — gap)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > mother fucker, I meant update the prd with the gaps, form tasks out of it and tell me what are there in your plate now
  - **prev asst tail:** > …(no preceding assistant message)

### `P18-cleanup-discipline-useless-files` — cleanup discipline useless files (1 hits)

**Memory/rule that should have prevented:** /ai/engineering-principles.md (lean charter)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > I want you to have a clean up task, no wasteful file, none ok?, nothing here should be fucking useless, engieering pricnicples
  - **prev asst tail:** > …udit_report.html`, `deploy_logs/` - x_upload full purge: deleted `pipeline/upload/x_upload.py`, `tests/test_upload_x.py`, `tests/test_upload_telemetry.py`, `scripts/setup_x_credentials.py`; cleaned `pipeline/upload/__init__.py`, `pipeline/paths.py`, `tests/test_paths.py`, `control/routes/telemetry_r

### `P19-code-fix-vs-prod-verification` — code fix vs prod verification (1 hits)

**Memory/rule that should have prevented:** feedback_verify_telemetry_with_preflight.md

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > yeah so, do your fucking job, and look at the things and logs on why this fduckers failed after the model changes and don't fucking say that it is fixed in code, we don't fucking care about the code we are here to make the right changes and brainstrom remeber? you have not yet pivoted to working motherfucker, it's Q and A, just aksed you to do yoour end of resaerch in which you have failed miserab
  - **prev asst tail:** > …of 50 attempts. I don't actually know the success rate. - Even if an mp4 lands in GCS, that doesn't mean it's shippable — the audit_data showed mp4s "succeeding" with gibberish text / cast morph / wrong characters / tiny captions. "File exists" ≠ "video works." - **Zero uploads to YouTube.** Every c

### `P20-false-pattern-recognition` — false pattern recognition (1 hits)

**Memory/rule that should have prevented:** (none — gap)

**Sample hits (up to 3, most-recent first):**

- session `3c75c490` —
  - **user:** > wtf is your problem dude, what are you saying, we need to fix all the servuces independytly why matching what matching idiot mother ugfcker explain
  - **prev asst tail:** > …(torch 2.6 + diffusers 0.35) with a build-time `FluxPipeline` import sanity check. When staging + qwen-rebuild complete, I'll fire flux2-dev's deploy without iterating.  Waiting on: - `bu0fnkr1x` — qwen rebuild (Cloud Build still polling) - `byg014b19` — dual staging (flux2-dev download mid-stream)

### `P21-state-visibility-what-is-happening` — state visibility what is happening (2 hits)

**Memory/rule that should have prevented:** (none — gap)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > what is happening idiot
  - **prev asst tail:** > …(no preceding assistant message)

- session `8a434b69` —
  - **user:** > what is happening idiot
  - **prev asst tail:** > …Deploy in flight. Resuming when complete.

### `P22-overengineering-minimal-asks` — overengineering minimal asks (8 hits)

**Memory/rule that should have prevented:** feedback_lean_engineering_charter.md (already documented, violated)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > writing the docs first is stupid, you haven't clarified from me anything, this just hardens our existing bloated and buggy code and adding you to the team is worhtless
  - **prev asst tail:** > …g 12 `/ai/` docs: - Agent 1: runtime-flows, api-contracts, data-models, source-of-truth, state-management - Agent 2: external-integrations, known-fragility, security-observations - Agent 3: tech-debt, performance-concerns, improvement-opportunities, debugging-notes - Agent 4: product-observations, d

- session `a4111d7f` —
  - **user:** > hey hey, idiot, ask question , I am not telling you to pivot, just the topic I want to ask answer quesiotns about
  - **prev asst tail:** > …Aguero" → "ah-GWAIR-oh" before TTS sees it. Channel-specific or global. 2. **Text normalization** — numbers (1944 → "nineteen forty-four"), acronyms (LIGO → "L-I-G-O"), Roman numerals, dates. Pre-TTS step. 3. **Chunking strategy** — TTS chunks broken at natural pauses (commas, periods) instead of mi

- session `a4111d7f` —
  - **user:** > yeah so, do your fucking job, and look at the things and logs on why this fduckers failed after the model changes and don't fucking say that it is fixed in code, we don't fucking care about the code we are here to make the right changes and brainstrom remeber? you have not yet pivoted to working motherfucker, it's Q and A, just aksed you to do yoour end of resaerch in which you have failed miserab
  - **prev asst tail:** > …of 50 attempts. I don't actually know the success rate. - Even if an mp4 lands in GCS, that doesn't mean it's shippable — the audit_data showed mp4s "succeeding" with gibberish text / cast morph / wrong characters / tiny captions. "File exists" ≠ "video works." - **Zero uploads to YouTube.** Every c

### `P23-dropping-work-user-wanted-kept` — dropping work user wanted kept (1 hits)

**Memory/rule that should have prevented:** (none — gap)

**Sample hits (up to 3, most-recent first):**

- session `3c75c490` —
  - **user:** > why are you dropping mother fucker? we can remove ytfactory-image-qwen rev 00008?
  - **prev asst tail:** > …(no preceding assistant message)

### `P24-repeated-failures-same-axis` — repeated failures same axis (2 hits)

**Memory/rule that should have prevented:** /ai/known-fragility.md (loops on same bug)

**Sample hits (up to 3, most-recent first):**

- session `144d9ca2` —
  - **user:** > it's not about du[pilication motherfucker, when I click on render it chosse the same topic again and again
  - **prev asst tail:** > …ompletion.  Summary of what's now in flight: - Music-fix worker: ✅ shipped (`20260524-124029`) - Queue UI `(untitled)` zombies: ✅ all 79 deleted; worker patched so new ones can't be minted - 31 ketchup duplicates: ✅ deleted - 3 Flight Attendant duplicates: ✅ deleted (kept the one with `done`) - Disc

- session `146b8773` —
  - **user:** > your focus should be image generation which you clearly haven't been able to do since 5 houirs you  motherfucker
  - **prev asst tail:** > …ars + 369 chars = the 124-word narration split into 2 chunks at the 400-char boundary, which is normal).  **What IS being regenerated 4 times across renders is everything** — rewrite, cast, TTS, ASR — because each cancelled+retriggered render is a brand-new Cloud Run job execution with its own work

### `P25-sequential-when-user-wants-parallel` — sequential when user wants parallel (1 hits)

**Memory/rule that should have prevented:** feedback_parallelize_via_subagents.md (already documented, violated)

**Sample hits (up to 3, most-recent first):**

- session `146b8773` —
  - **user:** > mother fucker do it in parallel
  - **prev asst tail:** > …CS Fuse), kick off the Cloud Build in the background. Build takes ~10 min. Current render keeps running on the old image. When build finishes, I have a ready-to-deploy fix; deploy only if the current render hasn't produced an mp4 by then.  **Path B — cancel + fix + retrigger.** Stop the current rend

### `P26-continue-dont-one-shot` — continue dont one shot (3 hits)

**Memory/rule that should have prevented:** feedback_keep_working_ask_inline.md

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > can you update the memory, that whatever you need to ask me ask me, don't stop like this stupid
  - **prev asst tail:** > …t extracts a `short_proposal` → system produces a 1080×1920 mp4 → uploads to one of 6 channels (MyStories, Sports, Hindutava, History, Rhyme, Cosmos). (README:3-8, 218-225) - Cloud-first: FastAPI control plane on Cloud Run + Firestore (jobs/tasks/sessions) + GCS artifacts + Cloud Run Job render work

- session `a4111d7f` —
  - **user:** > hey hey hey, continue asking man, don't fucking jump. Do you know what are the channels, what is eveyrhitng/? motherfucker ask comnplete set of question and I want all Qs and  my reposnses documnted somethere as well
  - **prev asst tail:** > …(no preceding assistant message)

- session `3c75c490` —
  - **user:** > idiot, continueon M4 + M5 both done.
  - **prev asst tail:** > …_mean_volume_db` - Integration block at writeback (~L2450): only fires in `mode=="real"`, skips upload + writes `status=failed` + `stage="writeback_verify"` + specific error string on any check failure (duration / video stream / audio stream / mean_volume / filesize) - `tests/test_cloud_writeback_ve

### `P27-critique-skill-bullshit` — critique skill bullshit (2 hits)

**Memory/rule that should have prevented:** (none — critique skill itself produces output of low value)

**Sample hits (up to 3, most-recent first):**

- session `a4111d7f` —
  - **user:** > nope myour criqtue is bullshit spweing machine. Nothing concrete
  - **prev asst tail:** > …as doing the same thing I did in the original critique — checklist theater.  Honest pass: of the 15, only 3 are real gaps worth adding:  1. **Loop replay-ability** — Shorts auto-loop; end-to-start match doubles view count math. Real, concrete, can be graded. 2. **3-sample-comments prediction** — for

- session `a4111d7f` —
  - **user:** > can you write the cirquite skill again, this time with no bullshit
  - **prev asst tail:** > …r, I didn't have a real answer, so I invented one that sounded plausible. Same pattern as the earlier critique — confident-sounding reasoning with nothing behind it.  I'll stop. If I don't have a source or a real observation, I'll say "I don't know" instead of generating something that sounds right.
