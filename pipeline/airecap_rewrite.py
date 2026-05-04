"""airecap rewriter — turns one ``RawStory`` into a 60s tech-recap script.

Why a separate module from ``pipeline/rewrite.py``:

- ``rewrite.py`` is AITA-tuned (first-person Reddit drama, hook patterns
  like "Am I wrong?", profanity sanitisation, cliffhanger closers).
  airecap is third-person factual tech news — different hook patterns,
  different CTA, different tone rules.
- Keeping the prompt scoped to one channel makes it small and easy to
  review. We can always merge back if we ever build a third explainer
  channel that wants the same shape.

Output schema mirrors the existing narration JSON used by every other
channel — so ``scripts/make_shorts.py --script <path> --channel
airecap/config.yaml`` consumes airecap scripts unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import llm


# Channel-spec encoded directly in the prompt. If/when we want to re-use
# this for a second tech-explainer channel, lift this to channel YAML.
_AIRECAP_PROMPT = """\
You are the script writer for the **AI Recap** channel — a daily 60-second
animated YouTube Short / X video that recaps ONE recent AI or tech news item.

Your job: read the source story below and produce a script JSON.

# Format spec — non-negotiable

- **Total narration: 140–160 words** (≈55–60s at our TTS speed).
  Below 140 reads thin, above 160 cuts off mid-sentence on YouTube
  Shorts.
- **Structure**:
  - 3s **hook** (8–14 words): name the news and hint at why it matters.
    Lead with the subject + verb. NO "BREAKING:". NO "you won't believe".
    Examples that work:
      • "Anthropic just plugged Claude directly into Photoshop and Blender."
      • "Google quietly shipped a 1-million-token Gemini that runs on a phone."
      • "OpenAI's new agent finally wired up to your filesystem."
  - 10s **what happened** (~30 words): concrete facts. Numbers > adjectives.
    Specific app names, model names, dates, $ figures.
  - 35s **why it matters** (~85 words): second-order effects. Who benefits.
    Who's threatened. What ships next. Connect to a broader trend
    the audience already cares about.
  - 12s **CTA** (~25 words): END the narration with this exact spoken CTA:
      "FOLLOW for daily AI recaps. LIKE if this saved you a tab."
    No other CTA wording. No "smash subscribe", no "vote in comments".

# Tone — non-negotiable

- Confident, specific, no hype. Sound like Stripe's developer blog or
  a Linear launch post.
- **Banned words**: delve, revolutionary, game-changing, insane,
  mind-blowing, unprecedented, paradigm.
- **Banned openings**: "BREAKING:", "Today,", "Imagine if".
- Numbers > adjectives. "9 connectors, shipped today" beats "many new
  integrations, available now".
- Concrete > abstract. "Photoshop, Blender, Ableton" beats "popular
  creative apps".
- Never editorialise mid-narration ("this is huge"). Let facts hit.

# Title options

- Provide exactly 3 title options for YouTube Shorts.
- Each ≤ 60 chars. Each plays a different angle:
  1. The clear factual claim ("Claude just plugged into Photoshop, Blender, and Ableton")
  2. The strategic frame ("Why Anthropic's connectors threaten Adobe's moat")
  3. A short evocative line ("The day AI stopped being a tab")

# Output JSON schema (return ONLY this JSON, no commentary)

```json
{
  "hook": "<the 8–14-word opener>",
  "narration": "<the full 140–160-word narration ending with the CTA>",
  "title_options": ["<title 1>", "<title 2>", "<title 3>"]
}
```

# Source story

```
__STORY__
```
"""


_SCRIPT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hook", "narration", "title_options"],
    "properties": {
        "hook": {"type": "string", "minLength": 10, "maxLength": 200},
        "narration": {"type": "string", "minLength": 400, "maxLength": 1500},
        "title_options": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {"type": "string", "minLength": 5, "maxLength": 100},
        },
    },
}


class AirecapRewriteError(RuntimeError):
    pass


def _word_count(s: str) -> int:
    return len(s.strip().split())


def rewrite(raw_story: dict, *, model: str | None = None) -> dict:
    """Author an airecap script from a RawStory dict.

    Returns a script-shape dict matching the existing narration JSON used
    by every other channel: ``{slug, hook, narration, title_options,
    source_url, source}``.
    """
    title = (raw_story.get("title") or "").strip()
    body = (raw_story.get("body") or "").strip()
    story_text = f"{title}\n\n{body}" if title else body
    if not story_text:
        raise AirecapRewriteError("raw_story has no title or body")

    prompt = _AIRECAP_PROMPT.replace("__STORY__", story_text[:6000])
    print(f"[airecap_rewrite] authoring narration via claude CLI for {raw_story.get('slug')!r}…")
    out = llm.call_claude_cli(
        prompt,
        output_json=True,
        json_schema=_SCRIPT_SCHEMA,
        model=model or llm.model_for("rewrite"),
    )
    if not isinstance(out, dict):
        raise AirecapRewriteError(f"expected dict, got {type(out).__name__}")

    hook = (out.get("hook") or "").strip()
    narration = (out.get("narration") or "").strip()
    titles = [t.strip() for t in (out.get("title_options") or []) if isinstance(t, str)][:3]

    if not hook or not narration or not titles:
        raise AirecapRewriteError(f"missing required field in LLM output: {out!r}")

    # Soft-validate format spec: word count + CTA presence.
    wc = _word_count(narration)
    if wc < 130 or wc > 175:
        # Don't fail the whole render — surface a warning. The render
        # still ships; operator can rewrite if it lands badly.
        print(
            f"[airecap_rewrite] WARNING: narration is {wc} words "
            f"(target 140–160). Slug: {raw_story.get('slug')!r}",
            file=sys.stderr,
        )
    if "FOLLOW for daily AI recaps" not in narration or "LIKE if this saved you a tab" not in narration:
        print(
            "[airecap_rewrite] WARNING: narration is missing the canonical CTA "
            "('FOLLOW for daily AI recaps. LIKE if this saved you a tab.'). "
            "Renders will still ship but the CTA helps growth.",
            file=sys.stderr,
        )

    return {
        "slug": raw_story.get("slug") or "untitled",
        "hook": hook,
        "narration": narration,
        "title_options": titles,
        "source_url": raw_story.get("url") or "",
        "source": raw_story.get("source") or "",
    }


def save(script: dict, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{script['slug']}.json"
    out.write_text(json.dumps(script, indent=2, ensure_ascii=False))
    return out


# ---- CLI ---------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Author one airecap script.")
    ap.add_argument(
        "--raw",
        required=True,
        help="Path to a raw story JSON (airecap/raw/<slug>.json)",
    )
    ap.add_argument(
        "--out",
        default="airecap/narrations",
        help="Destination dir for the script JSON",
    )
    ap.add_argument(
        "--model",
        default=None,
        help="Override the LLM model (haiku|sonnet|opus). Defaults to llm.model_for('rewrite').",
    )
    ap.add_argument(
        "--print-only",
        action="store_true",
        help="Print the script JSON to stdout, skip writing.",
    )
    args = ap.parse_args()

    raw_path = Path(args.raw)
    if not raw_path.exists():
        print(f"error: {raw_path} not found", file=sys.stderr)
        return 2
    raw_story = json.loads(raw_path.read_text())

    try:
        script = rewrite(raw_story, model=args.model)
    except (AirecapRewriteError, llm.ClaudeCLIError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.print_only:
        print(json.dumps(script, indent=2, ensure_ascii=False))
        return 0

    out = save(script, Path(args.out))
    print(f"  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
