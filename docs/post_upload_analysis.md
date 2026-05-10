# Post-upload analysis (cross-channel rule)

> **2026-05-08 update — operationalized as `/update-docs`.** This doc
> remains the **taxonomy source of truth** (the 5 classifications and
> their save destinations). The **executable workflow** that applies
> the taxonomy is now the `/update-docs` skill at
> `.claude/skills/update-docs/SKILL.md`. Triggers have been broadened
> from post-upload-only to "every meaningful unit of work" (any render
> ship, any `/critique-*` regression, any durable user correction,
> any explicit ask, end-of-session). See CLAUDE.md "Auto-invocation
> rule" for the canonical trigger list. The skill auto-invokes —
> Claude should fire it without being asked.

**Established 2026-05-08** after the LIGO Short shipped through 6
visible render iterations + 2 uploads, surfacing learnings that
existed only in conversation memory until the user explicitly asked
for them to be persisted (UTC pronunciation, photo aspect
inconsistency, image-narration drift, button end-screen, etc.).

**Rule:** every time a `make-*` skill completes a successful YouTube
upload (or the user manually uploads), the agent MUST run a
conversational debrief BEFORE the session ends. The debrief reviews
the iteration history that produced this video, classifies each
mistake / surprise / late-discovery, and updates the relevant notes
so the same problem doesn't repeat in the next render.

## When to trigger

Trigger conditions (any of):

1. `cosmosdecoded/scripts/upload.py` (or sibling channel uploaders)
   prints `UPLOAD OK` with a video_id.
2. The user runs `/critique-video` after upload and surfaces new
   issues.
3. The user instructs "analyze this conversation" or "save what we
   learned" or "update the notes".
4. End of session if at least one render finished.

## What the debrief covers

For every visible iteration in the conversation (every `vN` mp4
build, every render-failure-and-retry, every script rewrite, every
prompt-tuning round), classify:

| classification | meaning | action |
|---|---|---|
| **ONE-OFF** | typo / wrong date / single bad pronunciation in this slug only | fix in the JSON output for this slug; one-line note in `.claude/skills/<skill>/learnings/_index.md` |
| **CLASS-OF-BUG** | recurring pattern that will hit the next 100 renders | dedicated learning file: project-doc + skill-side mirror + memory entry; SKILL.md prompt update if the rule is authorial |
| **PIPELINE-BUG** | the rendering / TTS / image-prep / upload code itself was wrong | fix the pipeline code; learning file documents the bug + fix; reference from SKILL.md |
| **WORKFLOW-IMPROVEMENT** | the order/cadence of steps could be shortened | update CLAUDE.md if cross-channel; update SKILL.md if skill-specific |
| **PRONUNCIATION** | a TTS provider mispronounced a token | extend `pipeline/tts/text_normalize.py::_ACRONYM_PHRASES` (cross-channel) and/or per-slug `<slug>.pronounce.json` (skill-specific) |

For each row above:
- **Project-doc location:** `<channel>/learnings/<topic>.md` for channel-specific, `docs/<topic>.md` for cross-channel
- **Skill-doc location:** `.claude/skills/<skill>/learnings/<topic>.md`
- **Memory location:** `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_<topic>.md`
- **Per CLAUDE.md dual-save rule:** never save to memory only; always mirror to project doc

## What the debrief does NOT do

- It does NOT touch the rendered mp4 or upload record (read-only on
  the deliverable).
- It does NOT write feature requests or wishlist items — only items
  the agent can verify from this conversation's evidence.
- It does NOT save trivia ("today's render took 45 minutes") unless
  it's actionable (a pipeline timeout that surfaced needs documenting).

## Workflow (script the agent follows)

1. **Open the existing `_index.md`** for the running skill at
   `.claude/skills/<skill>/learnings/_index.md`.
2. **Walk the conversation backward** from the upload event to the
   skill invocation. For each visible iteration:
   - What was the visible problem?
   - Why did it happen (pipeline bug? skill-prompt gap? TTS quirk?)
   - Was it captured in a learning file already?
3. **Identify gaps** — iterations that produced a fix but no
   learning file got written. Default-to-write rather than
   default-to-skip; small learnings compound.
4. **Write or update** the relevant files per the table above.
5. **Append a one-line entry** to the skill's `_index.md` for each
   class-of-bug captured.
6. **Print a summary** to the user: "Debrief saved N learnings. Future
   `/make-<skill>` runs will avoid: …"

## What the LIGO short would have surfaced

(Worked example — items I should have written WITHOUT being asked, based
on the 6-iteration LIGO Short session 2026-05-08:)

1. **Chatterbox cloud silently truncates at ~40s** — saved (post-hoc)
2. **WPM was 148, not 231** — saved (post-hoc)
3. **`_flagged_words/` cache-invalidation gap** — saved
4. **`clip_NN.mp4` cache reused stale shotlist sources** — saved
5. **SVG sources crash ffmpeg in render path** — saved
6. **`.webm` Wikimedia URLs return JPEG thumbnails** — saved
7. **Match_text alignment for image swaps** — fix shipped, learning unsaved
8. **Numerical captions for dates / years / numbers** — saved
9. **LIKE+SUBSCRIBE button end-screen overlay** — saved
10. **UTC + science-acronym letter-spelling** — saved
11. **Uniform 1080x1349 inner box for portrait/landscape consistency** — fix shipped, learning unsaved
12. **Wikimedia URL guess miss-rate (~60%)** — saved earlier this session

Items 7 + 11 should have been written when their fixes shipped — that's
exactly what the post-upload debrief catches.

## Channel applicability

| channel | post-upload debrief required |
|---|---|
| Cosmos Decoded (long + short) | ✓ |
| HistoryRecapped (long + short) | ✓ |
| MyStoriesAnimated (Shorts) | ✓ |
| SportsRecapped (rivalry + ranking + sports-doc) | ✓ |
| HindutavaAnimated (Hindi mythology / kathaa) | ✓ |
| Cosmos Decoded katha / sleep-history | ✓ |
| Rhyme Time Junction | ✓ (when Suno render uploads) |

Effectively: every channel that publishes to YouTube gets the
debrief on every render.

## Skill SKILL.md integration

Every `make-*` skill's SKILL.md MUST include a Section 11 "Post-upload
analysis" pointing to this doc. The skill-specific section names which
learning topics are most likely to surface for that skill (e.g.
cosmos-short → TTS truncation, photo aspect, match_text alignment;
make-mystories-short → AITA closer phrasing, character continuity).

## Reference

- This doc: `docs/post_upload_analysis.md` (cross-channel rule of
  record)
- CLAUDE.md: persistent rule statement
- Memory: `feedback_post_upload_analysis.md` (terse pointer)
