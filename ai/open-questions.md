# Open Questions

Things to investigate or decide. Each entry: **Question** / **Where to look** / **Who decides** / **Why it matters**.

Source for the 2026-05-22 deferred items: `ai/onboarding-qa.md` "Open threads (not yet resolved in this session)" section.

---

## Deferred from the 2026-05-22 onboarding brainstorm

### Q-001: Which layer of output quality is weakest?
**Question:** Audio, visual, script, captions, pacing, hook — which one drags the watchability the most?
**Where to look:** Once one end-to-end render ships, run `/critique-video` and `/critique-audio` on it. Watch the mp4 cold and journal first-impression complaints. Compare against the cross-channel `audit_data/*_summary.json` for historical signal (caveat: those summaries describe pre-fix state per Q53).
**Who decides:** User. Deferred in Q8 ("we will come to quality later").
**Why it matters:** Until one render ships end-to-end, there's nothing to evaluate. Reliability (Q7) blocks quality. Re-open this question after the first clean render.

### Q-002: How long has ytFactory been running?
**Question:** Calendar age of the project.
**Where to look:** `git log --reverse --max-count 1` for the first commit; cross-reference earliest YAML mtime.
**Who decides:** Informational — user.
**Why it matters:** Calibrates "how long has nothing worked" — affects priority and whether to consider rewriting subsystems.

### Q-003: Audience scale / traction
**Question:** Current subscriber count + watch-time per channel. Is there an audience to lose, or are we starting cold on every channel?
**Where to look:** YouTube Studio (out-of-band; not in repo). Possibly `<channel>/uploads/*.json` for any historical signal on what shipped.
**Who decides:** User.
**Why it matters:** Affects the "ship broken vs. hold for quality" tradeoff. A 100-subscriber channel can absorb a rough first render; a 10K channel can't.

### Q-004: GCP cost order of magnitude
**Question:** Current monthly burn on `ytfactory-prod-v3`. Is the cost guardrail set in `CLAUDE.md` (rules 1-5) holding, or are we leaking?
**Where to look:** `scripts/audit_idle_costs.py` (daily audit), GCP billing console (out-of-band). Cross-check against `docs/cost_optimized_deploy.md` "Iron Rules."
**Who decides:** User; the cost guardrails in CLAUDE.md are already locked.
**Why it matters:** Cost was Q7-eliminated as a top pain (user said quality + reliability are bigger). But sustained leakage erodes runway and changes the calculus.

### Q-005: Render duration typical / range
**Question:** Wall-clock time for a typical Shorts render vs. a long-form render, P50 and P99.
**Where to look:** Once renders actually complete, `web-next/app/app/render/[jobId]/page.tsx` shows per-stage timings; Cloud Run job execution logs in GCP.
**Who decides:** User. Deferred in Q24 ("we can look into latency later").
**Why it matters:** Currently meaningless because nothing finishes (Q26). Re-open after MVP.

### Q-006: Budget situation
**Question:** Monthly spend ceiling. What number triggers "stop and re-architect"?
**Where to look:** Out-of-band; user.
**Who decides:** User.
**Why it matters:** Informs ADR-007 (iterative-extend retry) cost — every retry is GPU time. The retry cap of 1 was chosen with an implicit budget assumption that hasn't been numerically validated.

### Q-007: Time pressure / deadline
**Question:** Is there a date that matters?
**Where to look:** Out-of-band; user.
**Who decides:** User.
**Why it matters:** Affects how aggressively to attack the 17-item CONSOLIDATED ATTACK SET in `ai/onboarding-qa.md`. No deadline → fix the root cause; deadline → ship the hack first.

### Q-008: Paid help anywhere?
**Question:** Solo developer, or contractor/agency in the loop on any subsystem (voice clones, channel branding, music)?
**Where to look:** Out-of-band; user.
**Who decides:** User.
**Why it matters:** Affects what's safe to refactor without breaking somebody else's contract.

### Q-009: Image-gen 10% threshold gate handling under new retry shape
**Question:** When ADR-008 (z-turbo refiner) + per-beat retry-with-stronger-prompt (Q66) land, does `_PER_BEAT_FAILURE_THRESHOLD = 0.10` at `pipeline/render/ai_beat_slideshow.py:90` stay at 10%, or move with the new retry semantics?
**Where to look:** `pipeline/render/ai_beat_slideshow.py:90` for the current value. ADR-003 (gates as repair triggers) for the new philosophy.
**Who decides:** User; needs an ADR.
**Why it matters:** Discussed in Q66 but not specifically locked. The threshold should become a POST-retry kill (not first-fail kill) — the number itself may stay 10% or relax to 5% post-retry.

### Q-010: Image-gen batch+select pattern for character consistency
**Question:** Should image-gen render 32+ candidates per beat and pick the best (per the [illuminatianon Z-Image-Turbo guide](https://gist.github.com/illuminatianon/c42f8e57f1e3ebf037dd58043da9de32) "shots from same photoshoot" recommendation)?
**Where to look:** Z-Image-Turbo guide; current single-shot path at `pipeline/images/`.
**Who decides:** User. Discussed in Q67 but excluded from the current attack set as a "v2 addition."
**Why it matters:** Character consistency across panels is the #1 visual quality complaint per the cosmosdecoded character_description discussion. Batch+select is more expensive but proven to work; verbatim cast spec (ADR-009) is the cheap fix being tried first.

---

## Discovered during process-doc investigation (2026-05-22)

### Q-011: scrollpulse YAML shape — clone an existing channel or design fresh?
**Question:** The 6 existing YAMLs share a common skeleton (name, source_adapter, tts_provider, image_provider, niche variants). Should scrollpulse copy from `mystoriesanimated.yaml` (Reddit source-adapter + Chatterbox TTS) and add a `compose_template: split_screen_gameplay` field, or be designed from scratch?
**Where to look:** `pipeline/channels/mystoriesanimated.yaml` (closest in source), `pipeline/render/contracts.py` (engine plugin slots — does any of compose / overlays / visualize already support split-screen?), `scrollpulse/` directory if it exists.
**Who decides:** User (channel design); agent (mechanical YAML).
**Why it matters:** ADR-018 locks the *what*, not the *how*. The split-screen compose is genuinely new and may need a new visualize plugin.

### Q-012: Five launchd daemons — which are still alive?
**Question:** `control/com.ytfactory.{cloud-critic,cloud-snapshot,critique-runner,state-sync,upload-next}.plist` exist on disk. Are all five currently loaded and running on the user's laptop, or are some legacy?
**Where to look:** `launchctl list | grep ytfactory`. Logs at `~/Library/Logs/ytfactory/*.log` and `/tmp/upload_next.log`.
**Who decides:** User.
**Why it matters:** A loaded-but-broken daemon silently eats jobs from Firestore. `cloud-critic` in particular still references `GOOGLE_CLOUD_PROJECT=ytfactory-prod-v2` (the OLD project per Q53) — verify it's been updated for v3 or it's polling the wrong project.

### Q-013: `audit_data/` source-of-truth status
**Question:** The 153 entries under `audit_data/` describe pre-fix renders (per Q53 "the data you chose earlier might be old, we have made changes in model since that"). Is this directory archived, deprecated, or still being written to?
**Where to look:** Most-recent mtime in `audit_data/`. Any writer code (`grep -r "audit_data" pipeline/ scripts/`).
**Who decides:** User.
**Why it matters:** Future agents should know whether to treat `audit_data/` as live signal or historical reference. Currently `ai/debugging-notes.md` treats it as historical only.

### Q-014: GCP project name in plist EnvironmentVariables
**Question:** `com.ytfactory.cloud-critic.plist` and `com.ytfactory.upload-next.plist` both pin `GOOGLE_CLOUD_PROJECT=ytfactory-prod-v2`. Per Q53 the production bucket migrated to `ytfactory-prod-v3`. Are the plists stale?
**Where to look:** All 5 plist files in `control/`. Cross-reference `cloud/render-worker-v2/deploy.sh` for the canonical project.
**Who decides:** User; agent can mechanically update once confirmed.
**Why it matters:** If the daemons are polling v2 and the actual jobs land in v3, the critic loop is reading an empty queue forever and renders ship without their critic gate.
