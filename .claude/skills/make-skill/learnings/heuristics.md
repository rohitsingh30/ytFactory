# /make-skill — the 51 heuristics

Source of truth for what every generated ytFactory skill must enforce.
Updated 2026-05-04. When a heuristic gets violated in the wild, append
the regression note inline (don't fork a new file) and update the
SKILL.md cross-reference if the wording changes.

---

## A. Intent classification (1–7)

1. Classify into `/make-*` (authoring), `/critique-*` (post-render
   review), ops/research, or harness automation. Refuse to proceed
   until the bucket is locked.
2. Detect format: Shorts (50–60s) / long-form sleep (60–120m) / sports
   doc (20–30m) / ranking / rivalry-recap / rhyme / movie. Wrong
   bucket = wrong renderer.
3. Pin to exactly one channel. "Works for all channels" is almost
   always wrong (closers, voices, footage policies differ).
4. If the request is "every time X happens, do Y", redirect to
   `/update-config` — it's a hook, not a skill.
5. Reject duplicates: grep `.claude/skills/` and `~/.claude/skills/`.
   If overlap ≥60%, extend the existing skill instead of forking.
6. Reserve a unique trigger phrase. Refuse on collision.
7. Force the user to state success criteria in one sentence before
   generation.

## B. Channel context awareness (8–14)

8. Load `<channel>/learnings/channel.md` and `<channel>/config.yaml`
   before authoring. Bake their constraints in.
9. Inherit banned phrasings literally (verdict acronyms, "smash
   subscribe", "vote in comments", "COMMENT which battle next").
10. Inherit the channel's closer pattern verbatim.
11. Inherit TTS provider + voice ID from `config.yaml`.
12. Inherit aspect ratio + duration band; reject outputs outside.
13. Inherit footage policy (sports = real broadcast cut-in mandatory;
    HR Shorts = 100% archival; HR long-form = archive.org PD only).
14. Refuse if the channel dir doesn't exist; suggest
    `/create-youtube-channel` first.

## C. Pipeline integration (15–22)

15. Map output to existing stages: audio → images/footage → compose →
    captions → upload. Justify any new stage.
16. Reuse `pipeline/audio.py`, `pipeline/captions.py`,
    `pipeline/compose.py`, `pipeline/footage.py`. No re-implementation.
17. Output JSON lands at the channel's standard path
    (`<channel>/narrations/<slug>.json`, `footage_plan/<slug>.json`).
18. Renderer named `scripts/<channel>/render_<format>.py` consistently.
19. Reuse the F5-TTS-MLX singleton; never call `generate()` in a loop
    (memory: `feedback_f5_tts_mlx_singleton.md`).
20. Content-hash all caches (`img_NN.prompt.sha256`); never index-key
    (memory: `feedback_image_cache_content_hash.md`).
21. Force `-r 30` on any new footage clip path
    (memory: `feedback_footage_unify_fps.md`).
22. LLM calls go through `pipeline/llm.py` (`claude -p`), not the
    Anthropic SDK (memory: `feedback_llm_via_claude_cli.md`).

## D. Output schema / contract (23–30)

23. Explicit JSON schema with required keys; reject free-form output.
24. Beat shape matches the channel's existing schema.
25. Every narration includes a closer OR an explicit
    `closer: "embedded_in_last_beat"` flag.
26. Word budget per beat respects TTS pace (~165 wpm Kokoro,
    ~135 wpm F5-TTS sleep-cadence long-form).
27. Numbered shot list (camera/framing/lighting/blocking) for
    movie + doc formats.
28. Chapter structure mandatory for runtime ≥10 min.
29. SEO bundle (title, description, tags, hashtags) for every
    `/make-*` skill.
30. Source/citation list mandatory for any factual format
    (history, sports doc, AI-recap).
30b. **SKILL.md frontmatter `description:` MUST be ≤1024 chars.**
    The Claude Skills loader rejects the SKILL outright on overflow
    (silent failure at session start: `✖ .claude/skills/<name>/SKILL.md:
    description: Skill d…`). Hard-cap at ~980 chars to leave margin.
    Trigger phrases + routing hints to sibling skills are mandatory and
    must survive any trim — drop adjectives and parenthetical asides
    first, never the trigger keywords. Regression: 2026-05-05 —
    `make-cosmos-decoder` (1159) and `make-sleep-history` (1033) both
    failed to load on session start; trimmed to 981 each.

## E. Pre-render quality gates (31–38)

31. Run `/critique-audio` before image gen. TTS bugs invalidate
    downstream (memory:
    `feedback_critique_audio_before_image_gen.md`).
32. Pronunciation pre-pass (phonetic respelling) for foreign /
    proper nouns before TTS.
33. Banned-phrase scan against the channel's learnings dir; block
    emit on hit.
34. AITA-style profanity sanitizer (`_RE_PROFANITY_ASSHOLE`, verdict
    acronyms) for Reddit-derived narrations.
35. Source-fidelity check: every named claim traces to the dossier.
36. Length budget check: total runtime fits the channel's band;
    over/under both block.
37. Aspect-ratio match: 9:16 Shorts / 16:9 long-form; no in-between.
38. ContentID risk check: archive.org PD or paid stock for HR
    long-form; never YouTube re-uploaders.

## F. Self-learning / regression prevention (39–44)

39. Generated skill writes regressions back to
    `<channel>/learnings/<topic>.md` after any failed `/critique-*`.
40. Skill frontmatter carries `learnings_consulted: [paths]` so we
    can see which prior lessons loaded.
41. Class-of-bug vs one-off classification on every failure
    (memory: `feedback_engineer_class_of_bug.md`); class fixes go to
    pipeline code, one-offs go to skill prompt.
42. After each run, append a one-line postmortem to the skill's
    learnings index; update `MEMORY.md` pointer.
43. Skill loads its own `learnings/` dir at start and prepends new
    entries to its system prompt.
44. Dual-save enforced (CLAUDE.md): every learning lands in BOTH
    `~/.claude/projects/.../memory/` AND `<channel>/learnings/` or
    `docs/`.

## G. UX & ergonomics (45–48)

45. ≤4 stages of input. Never a 20-question wall.
46. Defaults from `config.yaml` first, channel learnings second; ask
    only when no default exists.
47. Confirm intent in one sentence before any generation.
48. Always echo the absolute output path.

## H. Anti-patterns (49–50)

49. Refuse skills that bypass `web/server.py` at `:8765` for
    production runs (memory: `project_workflow_website.md`).
50. Refuse skills that swallow credit / quota errors silently
    (memory: `youtube_upload_quota.md`).

## I. Engineering efficiency (51)

51. **Scout the current process for efficiency wins on every run
    BEFORE adding code.** Audit:
    - Pipeline reuse — would this duplicate `pipeline/<x>.py`?
    - Existing-skill reuse — can `/make-script` / `/make-ranking` /
      `/make-sports-doc` be extended instead?
    - Existing helper-script reuse —
      `scripts/<channel>/`, `scripts/historyrecapped/`,
      `scripts/sportstoriesanimated/` already cover the step?
    - Cache reuse — content-hashed image cache, F5-TTS-MLX singleton,
      whisper-mlx cache, vidlens collections.
    - Redundancy across skills — if ≥2 skills do the same step,
      propose extracting it (`pipeline/` or `scripts/_shared/`) as a
      separate refactor.
    - Schema extension over schema fork — extend an existing JSON
      schema with optional fields rather than forking a parallel one.
    - Dead paths — flag dead code noticed in passing for cleanup.

    **Surface every finding in the stage-4 handoff.** Don't silently
    duplicate. Don't silently fork. Don't silently leave dead code.

---

## Regression log (append-only)

<!-- format: YYYY-MM-DD — heuristic # — what fired — fix -->
- 2026-05-05 — 30b — `.claude/skills/make-cosmos-decoder/SKILL.md`
  (1159 chars) and `.claude/skills/make-sleep-history/SKILL.md`
  (1033 chars) failed to load at session start because frontmatter
  `description:` exceeded the Claude Skills 1024-char cap. Loader
  reported `✖ .claude/skills/<name>/SKILL.md: description: Skill d…`.
  Fix: trimmed both to 981 chars by collapsing duplicated context
  ("Cosmos Decoded channel" → "Cosmos Decoded", merging sibling-route
  hints) while preserving every trigger phrase and the routing pointers
  to /make-script, /make-top10, /make-katha, /make-cosmos-decoder,
  /make-sports-doc. New rule: hard-cap descriptions at ~980 chars in
  /make-skill stage-3 emit.
