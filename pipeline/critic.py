"""Stage 7.5 — auto-critique a finished Short and (optionally) drive
a regenerate pass on weak beats.

Spawns the `claude` CLI with the /critique-video skill's prompt body
inlined, plus access to the rendered mp4 frames (sampled to PNGs)
and the cached beats.json. Returns:

    {
      "score": 1..10,
      "top_issues": ["short text", ...],
      "beat_corrections": {beat_idx: "fix prompt to ...", ...},
      "critique_md": "<markdown body>"
    }

If ``score < channel_cfg["min_critic_score"]``, the orchestrator
patches the affected beats' prompts.json entries and regenerates
just those images, then recomposes once.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import llm


# Patterns the critic occasionally emits that are NOT visual scene
# descriptions and would corrupt the image-gen prompt if concatenated.
# When a beat correction contains these, we drop the offending span and
# keep only the visual remainder. If nothing visual is left, the patch
# is rejected entirely (better to leave the original scene untouched
# than to render a hallucinated text overlay).
_META_INSTRUCTION_PATTERNS = (
    # "Spoken closer rewrite to: '...'", "Audio:", "Narration:", "TTS:",
    # "Panel render unchanged" — these are author-voice meta directives
    # that the image model interprets as literal scene content.
    re.compile(r"\bSpoken\s+\w+\s+rewrite\s+to:.*?(?=\.|$)", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:Audio|Narration|TTS|Voice)\s*:.*?(?=\.|$)", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bPanel\s+render\s+(?:unchanged|update|patch).*?(?=\.|$)", re.IGNORECASE),
    re.compile(r"\bClass[- ]of[- ]bug\b.*?(?=\.|$)", re.IGNORECASE),
    re.compile(r"\bONE[- ]OFF\b.*?(?=\.|$)", re.IGNORECASE),
    # Quoted full sentences. Image models render quoted text verbatim
    # when given quoted strings (this is the actual bug — "AITA for
    # kicking them out..." in quotes became a banner in the image).
    # Drop the quoted span; keep surrounding context.
    re.compile(r"['\"][^'\"]{15,}['\"]"),
)


def _sanitise_scene_patch(fix: str) -> str:
    """Strip critic-introduced meta-instructions from a beat-correction
    fix string. Returns the cleaned visual description, or '' if nothing
    visual remains.

    Critic finding 2026-05: a closer-beat correction included a quoted
    spoken-CTA rewrite ("AITA for kicking them out — or did Amelia
    have it coming?") which the image model rendered as a literal text
    banner in the image, double-stamping the closer panel. The fix
    must only describe what is VISIBLE, never what is SPOKEN or how
    the text panel renders.
    """
    if not fix or not fix.strip():
        return ""
    cleaned = fix
    for pat in _META_INSTRUCTION_PATTERNS:
        cleaned = pat.sub(" ", cleaned)
    # Collapse double-spaces left by the substitutions.
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:")
    # Require a minimum substantive remainder — a stray adjective or
    # connective alone isn't worth patching, and silently appending it
    # would only add noise to the scene.
    if len(cleaned) < 8:
        return ""
    return cleaned


SAMPLE_FPS = 1  # one frame per second is enough density for critique.


# JSON schema for the critic's response. Caps every free-form string
# with maxLength so the model can't ramble — schema-constrained output
# both shrinks token spend and tightens the per-finding signal-to-noise.
# Telemetry on a recent run: critic emitted 13.4k output tokens at $0.30
# in 4m21s; the bulk was redundant prose in per_frame_findings. Bounding
# maxLength + maxItems trims padding without losing the actionable items.
_CRITIC_SCHEMA = {
    "type": "object",
    "required": [
        "score", "one_line_take", "top_issues",
        "beat_corrections", "system_corrections", "highest_leverage_change",
    ],
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "one_line_take": {"type": "string", "maxLength": 400},
        "top_issues": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 500},
        },
        "per_frame_findings": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": [
                    "beat", "timestamp_s", "what_is_wrong",
                    "classification", "fix",
                ],
                "properties": {
                    "beat": {"type": "integer"},
                    "timestamp_s": {"type": "string", "maxLength": 32},
                    "lenses": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 8},
                    },
                    "what_is_wrong": {"type": "string", "maxLength": 800},
                    "classification": {
                        "type": "string",
                        "enum": ["one-off", "class-of-bug"],
                    },
                    "fix": {"type": "string", "maxLength": 600},
                },
            },
        },
        "beat_corrections": {
            "type": "object",
            "additionalProperties": {"type": "string", "maxLength": 600},
        },
        "system_corrections": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["issue_class", "where", "fix", "principle"],
                "properties": {
                    "issue_class": {"type": "string", "maxLength": 80},
                    "where": {"type": "string", "maxLength": 200},
                    "fix": {"type": "string", "maxLength": 800},
                    "principle": {"type": "string", "maxLength": 200},
                },
            },
        },
        "highest_leverage_change": {"type": "string", "maxLength": 600},
    },
}


def _sample_frames(mp4_path: Path, frames_dir: Path) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(mp4_path),
        "-vf", f"fps={SAMPLE_FPS}",
        "-q:v", "2",
        str(frames_dir / "t_%02d.png"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg frame-sampling failed: {result.stderr[-500:]}"
        )
    return sorted(frames_dir.glob("t_*.png"))


_CRITIC_PROMPT = """\
You are a YouTube Shorts viewer AND a pipeline engineer. The video
at the path below just auto-played in your feed. You have to decide
in 1.5 seconds whether to keep watching or flick up. Then if you
stayed, react to what happens next, beat by beat, the way a real
viewer would — attention drifting, getting hooked, rolling your eyes,
etc. THEN switch hats and engineer the fixes.

Read every PNG frame under {frames_dir} in order (they are named
t_00.png, t_01.png, ..., one per second). The audio timeline is in
{beats_path} as word-level timestamps — read it once and use that
as your "audio track" since you can't actually listen.

EVERY-LENS PASS — non-negotiable:
You must examine every frame through ALL of these lenses. Most
lenses will be silent on most frames; that's fine. The point of
listing them is so you don't MISS what you'd otherwise overlook.
Speak up only when a lens flags something concrete + timestamped.

  L1  Hook (0.0-1.5s) — am I scrolling past this in 1.5s? why exactly?
  L2  Audio-visual sync — does the image match the spoken word RIGHT
      THEN, or is there lag (steak shown while wine is spoken)?
  L3  Character continuity — is the character the same person
      frame-to-frame? same hair, age, outfit, body shape? or do they
      morph? (channel-locked narrator AND per-story cast must hold)
  L4  Pacing / retention — where am I bored? where do I lean in?
  L5  Caption legibility — readable on a phone (contrast, size,
      position, line wrap)? covered by anything? matches spoken word?
  L6  AI-glitch — wrong fingers, melting faces, ghost-doubled bodies,
      gibberish text, wrong number of people, bad anatomy
  L7  Composition — rule of thirds, headroom, leadroom, negative
      space; subject stuck dead-center for 16s? cropped at a joint?
  L8  Palette / channel aesthetic — does the locked look hold every
      frame? any frame drifting into a different style?
  L9  Mute mode — 80% of Shorts viewers mute. Does the video tell
      the story without sound — captions + visuals alone? Where does
      mute mode break?
  L10 Thumbnail (frame 0) — would frame 0 stop a thumb-scroll as a
      static thumbnail? clear punchline visual or generic?
  L11 Story arc / escalation — does the narrative escalate? is there
      a "wait what" beat? or does energy stay flat?
  L12 Closer / CTA — does the closer panel render legibly and hold
      long enough to read? spoken closer match the panel? actually
      drives action (LIKE/COMMENT split present)?
  L13 Source fidelity — vs the raw story, did the narration miss
      the punchline? sand off a juicy detail? soften a contentious
      word? (skip if no raw story is on disk for this slug)
  L14 Re-watchability — would I tap replay? a detail in the hook
      I'd notice on a second pass?
  L15 Comments-bait — will the closer drive specific opinions, or
      vague "what do you think" mush? deliberate ambiguity that
      makes commenting irresistible?

Cite the timestamp on every complaint. "It's confusing" is useless;
"at 7s I'm hearing 'three bottles of wine' but the image is still
showing the salad from beat 3" is useful.

ENGINEERING THINKING — non-negotiable:
For EVERY issue you raise, classify it:

  • ONE-OFF — this is unique to this Short's prompt for this beat.
    Fix lives in the per-beat prompts.json patch (`beat_corrections`).

  • CLASS-OF-BUG — the pipeline could let this slip through on
    any future Short. Fix lives at the SCHEMA / CODE / VALIDATOR
    level. Example: "the character smiles through every emotional
    beat" is a class bug — the cast.py author isn't propagating
    emotional tone, and prompts.py isn't driving per-beat emotion
    from beat-text sentiment. Fix it ONCE in the pipeline; don't
    patch the smile on every video.

If you can't decide, default to CLASS-OF-BUG — being wrong towards
system fixes is cheaper than being wrong towards whack-a-mole.

You must propose system-level fixes (`system_corrections`) for every
class-of-bug issue. Don't just complain about this video — engineer
the next 100 videos to not have the same problem. Reference
specific files when you can: `pipeline/cast.py`, `pipeline/prompts.py`,
`pipeline/compose.py`, `pipeline/script_check.py`, `channels/<channel>.yaml`,
`pipeline/images.py:lint_prompt` (the linter rule list), DESIGN.md §14
(the principles list).

After watching, return ONLY a JSON object with this exact shape:

{{
  "score": <integer 1-10, where 10 is "I would share this Short">,
  "one_line_take": "<single sentence, would-I-keep-watching>",
  "top_issues": [
    "<concrete issue 1 with timestamp + classification (one-off|class-of-bug)>",
    "<concrete issue 2>",
    "<...>"
  ],
  "per_frame_findings": [
    {{
      "beat": <integer beat index, matching beats.json order>,
      "timestamp_s": "<e.g. '7.0-9.6s'>",
      "lenses": ["<L1|L2|...>", ...],
      "what_is_wrong": "<concrete, specific>",
      "classification": "<one-off|class-of-bug>",
      "fix": "<actionable one-liner — concrete prompt diff OR file:function change>"
    }}
  ],
  "beat_corrections": {{
    "<beat_index_as_string>": "<what to change about this beat's prompt to fix it (ONE-OFF only — this is the string fed into the regenerate loop)>"
  }},
  "system_corrections": [
    {{
      "issue_class": "<short label, e.g. 'narrator-emotion-mismatch'>",
      "where": "<file:function or schema field, e.g. 'pipeline/cast.py:author_cast — narrator.default_emotion not propagated to prompts.py'>",
      "fix": "<the engineering change that prevents this class on all future Shorts>",
      "principle": "<which DESIGN.md §14 principle this is or 'NEW' if it should be added as #15+>"
    }}
  ],
  "highest_leverage_change": "<one thing — prefer system_corrections over beat_corrections when both apply>"
}}

per_frame_findings is the detailed multi-lens record — one entry per
flagged frame, may include multiple lenses per entry. Lenses that
were silent are simply omitted; you do NOT need to list "L7: ok" for
every frame.

beat_corrections keys must be string-form beat indices ("0", "1", ...)
matching the order in beats.json. Only include beats that genuinely
have a unique problem ON TOP OF a class-of-bug fix; if a class fix
covers it, don't double-patch with a beat correction. Empty dict if
everything's fine or if all issues are class-of-bug.
"""


def critique_short(
    *,
    slug: str,
    mp4_path: Path,
    cache_dir: Path,
    out_dir: Path,
) -> dict:
    """Critique a rendered Short. Returns the parsed dict (see header).

    Side effects:
    - Writes sampled frames to ``out_dir/frames/``.
    - Writes the parsed JSON to ``out_dir/<slug>.score.json``.
    """
    if not mp4_path.exists():
        raise FileNotFoundError(f"mp4 missing: {mp4_path}")
    beats_path = cache_dir / "beats.json"
    if not beats_path.exists():
        raise FileNotFoundError(f"beats.json missing: {beats_path}")

    frames_dir = out_dir / "frames"
    print(f"[critic] sampling frames from {mp4_path.name}…")
    frames = _sample_frames(mp4_path, frames_dir)
    print(f"[critic] {len(frames)} frames at {frames_dir}")

    prompt = _CRITIC_PROMPT.format(
        frames_dir=frames_dir.resolve(),
        beats_path=beats_path.resolve(),
    )

    critic_model = llm.model_for("critic")
    print(f"[critic] sending to claude CLI for critique ({critic_model} — vision quality matters)…")
    raw = llm.call_claude_cli(
        prompt,
        output_json=True,
        # Vision + judgment quality is the product here — opus by default.
        # Override with YTFACTORY_MODEL_CRITIC=sonnet for fast iteration.
        model=critic_model,
        # Schema-constrained output: caps free-form prose so the model
        # emits only actionable findings instead of padding. Cuts output
        # tokens (~30% on observed runs) without losing per-frame detail.
        json_schema=_CRITIC_SCHEMA,
        # Critic needs Read access to frames + beats.json.
        allowed_tools=["Read", "Bash"],
        add_dirs=[frames_dir.resolve(), beats_path.parent.resolve()],
        timeout_s=600,
        budget_usd=1.5,
    )

    if not isinstance(raw, dict):
        raise ValueError(f"critic expected JSON object, got {type(raw).__name__}")

    out_dir.mkdir(parents=True, exist_ok=True)
    score_path = out_dir / f"{slug}.score.json"
    score_path.write_text(json.dumps(raw, indent=2))
    print(
        f"[critic] score={raw.get('score')!r} — "
        f"{raw.get('one_line_take', '(no take)')[:120]}"
    )

    # Surface system-level corrections to the operator. These are
    # CLASS-OF-BUG fixes that should be made in code, not patched
    # on this single video. They're the highest-value output of the
    # critic loop — every Short reviewed should grow the pipeline
    # smarter, not just reach a passing score for this artifact.
    sys_corrections = raw.get("system_corrections") or []
    if sys_corrections:
        print(f"[critic] === ENGINEERING (class-of-bug) fixes for the next 100 Shorts ===")
        for sc in sys_corrections:
            print(
                f"[critic]   • [{sc.get('issue_class', '?')}] "
                f"{sc.get('where', '?')}\n"
                f"[critic]     fix: {sc.get('fix', '?')}\n"
                f"[critic]     (principle: {sc.get('principle', '?')})"
            )
        print(f"[critic] ============================================================")

    return raw


def regenerate_with_corrections(
    *,
    slug: str,
    cache_dir: Path,
    beat_corrections: dict[str, str],
) -> set[int]:
    """Patch prompts.json per ``beat_corrections``, delete the affected
    img_NN.png so the orchestrator regenerates them on next run.

    Returns the set of beat indices that were patched. Empty set =
    nothing to do.
    """
    if not beat_corrections:
        return set()

    prompts_path = cache_dir / "prompts.json"
    if not prompts_path.exists():
        print(f"[critic] no prompts.json at {prompts_path}; cannot patch")
        return set()

    prompts = json.loads(prompts_path.read_text())
    patched: set[int] = set()
    for k, fix in beat_corrections.items():
        try:
            i = int(k)
        except ValueError:
            print(f"[critic] skipping non-integer beat key {k!r}")
            continue
        if i < 0 or i >= len(prompts):
            print(f"[critic] beat index {i} out of range; skipping")
            continue
        # Sanitise the critic's fix BEFORE concatenating into the scene.
        # Critic patches sometimes include meta-instructions like
        # "Spoken closer rewrite to: 'AITA for kicking them out...'" —
        # the image model sees the quoted sentence as scene content and
        # hallucinates the text INTO the rendered image (e.g. a black
        # header banner with the typo'd CTA stamped over the closer
        # panel). Keep only the visual-description portion of the fix.
        clean_fix = _sanitise_scene_patch(fix)
        if not clean_fix:
            print(
                f"[critic] beat {i} patch rejected — only meta-instructions "
                f"(no visual content): {fix[:80]!r}"
            )
            continue
        # Second-pass strip — catches text-bait phrases / meta-imperatives
        # the regex above missed. Same enforcer the author path uses
        # (prompts.py:_validate_and_clean), shared via images.strip_text_bait.
        from . import images as _images
        clean_fix, removed_secondpass = _images.strip_text_bait(clean_fix)
        if removed_secondpass:
            print(
                f"[critic] beat {i} second-pass stripped {removed_secondpass} "
                f"from patch"
            )
        if not clean_fix or len(clean_fix) < 8:
            print(
                f"[critic] beat {i} patch rejected after second-pass strip "
                f"(no usable visual content left)"
            )
            continue
        original_scene = prompts[i].get("scene", "")
        merged = (
            f"{original_scene}. {clean_fix}"
            if original_scene
            else clean_fix
        )
        # Final pass: also strip the merged scene, since concatenating
        # original + fix can re-introduce a quoted-text or meta clause
        # that originally sat on the boundary between the two.
        merged, removed_merge = _images.strip_text_bait(merged)
        if removed_merge:
            print(
                f"[critic] beat {i} merged-scene strip removed: {removed_merge}"
            )
        prompts[i]["scene"] = merged
        patched.add(i)
        print(f"[critic] patched beat {i}: + {clean_fix[:80]}")

    if patched:
        prompts_path.write_text(json.dumps(prompts, indent=2))
        # Delete affected images so orchestrator regenerates.
        for i in patched:
            img = cache_dir / f"img_{i:02d}.png"
            if img.exists():
                img.unlink()
                print(f"[critic] removed cached {img.name} for regen")

    return patched
