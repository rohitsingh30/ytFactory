---
name: Sports cast-locked tokens are server-side enforced, not LLM-suggested
description: Class-of-bug rule — kit + shirt number + era for any named supporting character in a sports beat must be injected programmatically post-LLM, never trusted to be copied verbatim
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
For SportsStoriesAnimated, the prompt-author LLM cannot be trusted to faithfully copy `kit + shirt_number + era_notes` from the dossier into per-beat scene strings. Caught on the v5 Aguero 93:20 render: cartoon Dzeko shipped with shorts #4 (real: #10), and cartoon Kompany shipped with a teal kit + #17 (real: 2011-12 City sky-blue + #4) — both because the LLM paraphrased the dossier instead of copying.

**Why:** The LLM has the dossier in its prompt context but its scene-authoring is creative-rewriting, so kit specifics drift. The dossier-cast pipeline already has the canonical values; we just need to enforce them after the LLM returns rather than inside the LLM call.

**How to apply:**
- After `pipeline/prompts.author_beat_prompts` returns, do a server-side post-processing pass: for each beat whose `scene` matches a supporting[] character (by name or alias), append the canonical kit string + `#<shirt_number>` + era_notes verbatim from cast.json. Don't ask the LLM to do this — enforce it in code.
- Same enforcement pattern applies to other "factual hard tokens" we might add later: stadium name, match date, scoreline overlay text. Anything that has a CORRECT value in the dossier should be copy-pasted, not re-described.
- Lint catches: pipeline/prompts.py should warn (or fail-loud) if a scene names a supporting character but the kit/number tokens DON'T appear after post-processing — that means the injector matched against the wrong character.
- This is part of the broader "Compose is the contract" / "Cast is the contract" principle: the dossier and cast files are the truth, the LLM is allowed to riff on tone but not on facts.