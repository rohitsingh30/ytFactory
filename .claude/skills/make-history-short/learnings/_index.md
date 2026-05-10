# /make-history-short — learnings

Append one line per regression. Classify each as ONE-OFF (this script)
or CLASS-OF-BUG (extend §6 quality gates + mirror to
`historyrecapped/learnings/<topic>.md`). See §8 of `SKILL.md` for the
full self-learning loop.

<!-- entries appended below this line -->

## 2026-05-08 — pompeii-79 (animated path), 10 renders to ship

| # | Surprise | Class | Fix |
|---|---|---|---|
| 1 | WW1 soldier-on-horseback in Roman Short — no era enforcement at all | CLASS-OF-BUG | era_lock injection from `narration.metadata.era_lock` shipped at `pipeline/render/shorts.py:842-866`. **Add to skill §6**: skill MUST author `metadata.era_lock` per `docs/era_lock_goldilocks.md` Goldilocks rules. |
| 2 | Heuristic prompt prefix "a single character" forced figures into object-only beats | PIPELINE-BUG | `pipeline/images.py:beat_to_prompt` dropped the prefix. Cast-router decides character vs object beats. |
| 3 | Cloud Chatterbox cold-start exceeded `CLOUDRUN_TTS_TIMEOUT` → fell back to F5 → 33s truncated mp4 | PIPELINE-BUG | Pre-warm hook ported from `render/footage_only.py` to `render/shorts.py:1238-1264`. **Defensive habit**: every pipeline patch needs sibling-mirror review across `render/*.py`. |
| 4 | era_lock too long ("togas + harbor + ships + oil lamps") → 80% of frames identical "Roman harbor + togas" | CLASS-OF-BUG | Goldilocks rule documented in `docs/era_lock_goldilocks.md`. Drop composition tokens (harbor, ships, oil lamps); keep only era anchor + clothing + roof material + specific negatives. |
| 5 | era_lock too short ("period-accurate") → fell back to WW1 soldiers prior | CLASS-OF-BUG | Same doc. Min affirmative tokens needed; "period-accurate" is meaningless to diffusion. |
| 6 | LIKE+SUBSCRIBE icons firing on "falls **like** snow" mid-Short | CLASS-OF-BUG | `_CLOSER_KEYWORDS` UPPERCASE + last-25% positional gate. `docs/closer_overlay_design.md`. |
| 7 | Per-word captions ran through 2-beat closer (split closer detection failed) | CLASS-OF-BUG | Span-based detection in `compose.py`. Mirror across `compose()` AND `compose_clips()`. |
| 8 | peter_drury voice ref overemphasized "MOUUUNT" + room-tone bleed | ONE-OFF for this slug; CLASS-OF-BUG that voice-refs need hygiene gate (denoise + trim) | Reverted to sarah.wav. Voice-ref hygiene rule pending. |
| 9 | "ad 79" pronounced as the word "add" by Cloud Chatterbox | CLASS-OF-BUG (PRONUNCIATION) | `_RE_AD_BC_YEAR` + `_RE_AD_BC_POST_YEAR` in `pipeline/tts/text_normalize.py`. |
| 10 | Caption "Aug 24" then split across "A" "D" "79" word PNGs | CLASS-OF-BUG | After audio letter-spell, whisper produces 7 tokens not 6. Caption pattern in `pipeline/captions.py` extended to 7-token form. **Rule: audio normalize ↔ caption numericalize MUST stay in sync token-for-token.** |
| 11 | prompts.json had 12 entries for 19-beat narration → 7 beats fell to heuristic → era_lock dominated | CLASS-OF-BUG / SKILL-IMPROVEMENT | Skill §6 should compute expected beat count and require N prompts. **Skill §6 update**: every animated Short must author one prompt per expected beat (estimate via `narration_words / 145 * 60 / beat_target_s`). |
| 12 | Compositions naturally collapse to "alleyway with toga crowd" without explicit guidance | CLASS-OF-BUG / SKILL-IMPROVEMENT | Per-beat scenes must START with COMPOSITION TYPE in caps (EXTREME WIDE / VERTICAL LOW-ANGLE / MACRO CLOSE-UP / REAR-VIEW / STILL-LIFE / etc.). Cycle through types — never repeat adjacent. See `feedback_prompts_must_cover_all_beats.md`. |
| 13 | Background mp4 corruption (moov atom missing) when ffmpeg killed mid-mux by session reset | WORKFLOW | Recovery: keep narration + image cache; rerun compose only (`make_shorts.py` will TTS-cache-hit + image-cache-hit). |

