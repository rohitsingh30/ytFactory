#!/usr/bin/env python3
"""Author per-panel `scene` strings for a long-form sleep narration.

Reads `historyrecapped/narrations/<slug>.json`. If the narration JSON has no
`panels:` field, this tool slices the text into ~`hold_s`-sized chunks (based
on a 105-wpm sleep cadence), asks `claude -p` to write a graphic-novel scene
prompt per chunk per the locked style rules in
`historyrecapped/learnings/long_form_visual_signature.md`, and writes the
results back into the narration JSON under `panels`.

Each panel scene must:
  - Name the era + period dress (diffusion drifts to anachronistic kit)
  - EXPLICITLY name a small warm-yellow light source (campfire / brazier /
    lantern / hearth / oil lamp / sun catching on a window). Z-Image-Turbo
    drops it if not in the per-panel scene text — the channel-level style
    prefix alone is not enough.
  - Read as a single comma-joined sentence ~30-60 words.

Usage:
    .venv/bin/python historyrecapped/scripts/author_panels.py \\
        --channel historyrecapped --slug western-front-1914-1918-sleep
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.llm import call_claude_cli


PANEL_PROMPT = """You are authoring per-panel scene prompts for a long-form
sleep-history YouTube video, in the style of the channel "Sleepy Time History".
The renderer feeds your `scene` strings into Z-Image-Turbo with this locked
style prefix:

    hand-drawn illustrated panel in the style of a graphic-novel history book,
    bold dark ink linework with visible hatching for shadow, soft watercolor
    wash fills, earthy palette of ochre khaki forest-green dusk-blue and warm
    brown, a small bright yellow campfire or hearth visible at the focal point
    as the warm-light anchor, cool blue ambient backdrop with distant mountains
    or water, wide cinematic 16:9 landscape framing with small human figures
    in period dress, atmospheric depth via foreground silhouette frame (cave
    mouth or tree silhouette or rock arch).

Hard rules per panel (NON-NEGOTIABLE — diffusion drops these otherwise):
  1. Pin the era + period dress explicitly (e.g. "1916 British Expeditionary
     Force soldiers in trench coats and steel helmets").
  2. EXPLICITLY name a small warm-yellow light source IN the scene — campfire,
     brazier, lantern, hearth, oil lamp, lit forge, sun catching on a tin cup,
     candle. Don't trust the style prefix to add it. Always say where it is
     ("at the focal point", "in the foreground", "on the table").
  3. Wide landscape framing with small human figures (1-6 people max). Avoid
     close-up portraits — the channel signature is environmental.
  4. Single comma-joined sentence, 30-60 words, no quotation marks, no
     bullet points. Plain English describing the painting.

Your task: take the narration excerpt below and write ONE panel scene that
matches a still illustration of that moment. Your output is JSON only.

Narration excerpt (panel #{idx} of {total}, audio time approx {t0:.0f}s-{t1:.0f}s):

```
{excerpt}
```

Episode-wide context (era/topic for grounding):
  Title: {title}
  Era: {era}

Output a single JSON object: {{"scene": "<your scene description>"}}.
"""


def split_narration_into_chunks(narration: str, target_chars: int = 1700) -> list[str]:
    """Split narration into chunks roughly aligned to one panel each.

    1700 chars at our 5.8 chars/word avg is ~290 words; at 105 wpm that's
    ~167 seconds — way too long for one panel. Calibrate per-call instead.
    Splits on paragraph boundaries first, then packs paragraphs into chunks
    until target_chars is reached. Never breaks mid-paragraph.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", narration) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paragraphs:
        if not cur:
            cur = para
        elif len(cur) + 2 + len(para) <= target_chars:
            cur = f"{cur}\n\n{para}"
        else:
            chunks.append(cur)
            cur = para
    if cur:
        chunks.append(cur)
    return chunks


def _wpm_chars(chars: int, wpm: float = 105.0) -> float:
    """Convert char count → estimated audio seconds at sleep narration cadence."""
    words = chars / 5.8
    return (words / wpm) * 60.0


def author_panels(
    narration_path: Path,
    target_panel_s: float = 22.0,
    title: str = "",
    era: str = "",
    model: str = "haiku",
    overwrite: bool = False,
) -> list[dict]:
    """Read narration JSON, slice + author panel scene strings, write back."""
    data = json.loads(narration_path.read_text())
    if data.get("panels") and not overwrite:
        print(f"[panels] {narration_path.name} already has {len(data['panels'])} panels "
              f"(use --overwrite to regenerate)")
        return data["panels"]

    text = data.get("narration") or "\n\n".join(s.get("text", "") for s in data.get("sections", []))
    if not text.strip():
        raise SystemExit(f"narration JSON {narration_path} has no narration text")

    # Target chars per chunk = wpm-converted to audio_s of ~target_panel_s.
    # 105 wpm × 5.8 chars/word ÷ 60 = ~10.15 chars/sec.
    target_chars = int(target_panel_s * (105.0 / 60.0) * 5.8)
    chunks = split_narration_into_chunks(text, target_chars=target_chars)
    print(f"[panels] {narration_path.name}: {len(text)} chars → {len(chunks)} panels "
          f"(target ~{target_panel_s:.0f}s/panel ≈ {target_chars} chars)")

    schema = {
        "type": "object",
        "properties": {"scene": {"type": "string", "minLength": 30, "maxLength": 600}},
        "required": ["scene"],
    }

    panels: list[dict] = []
    cumtime = 0.0
    for i, chunk in enumerate(chunks):
        chunk_audio_s = _wpm_chars(len(chunk))
        prompt = PANEL_PROMPT.format(
            idx=i + 1, total=len(chunks),
            t0=cumtime, t1=cumtime + chunk_audio_s,
            excerpt=chunk[:2400],   # cap to avoid token blowup
            title=title or data.get("title", ""),
            era=era or data.get("era", ""),
        )
        try:
            r = call_claude_cli(
                prompt, output_json=True, json_schema=schema, model=model,
                timeout_s=120, budget_usd=0.50,
            )
            scene = (r.get("scene") if isinstance(r, dict) else "").strip()
        except Exception as e:
            print(f"[panels]   #{i+1} authoring failed: {e!r} — skipping")
            scene = ""
        if not scene:
            # Fallback so the panels list stays aligned with the chunks.
            scene = (
                f"a wide period-accurate landscape from the era described in "
                f"the narration excerpt, with small figures in period dress "
                f"gathered around a small bright yellow campfire at the focal "
                f"point as the warm-light anchor, cool blue distant ambient"
            )
        panels.append({
            "scene": scene,
            "hold_s": round(chunk_audio_s, 2),
            "seed_offset": i,
        })
        print(f"[panels]   #{i+1}/{len(chunks)} ({chunk_audio_s:.1f}s) → {scene[:90]}…")
        cumtime += chunk_audio_s

    data["panels"] = panels
    narration_path.write_text(json.dumps(data, indent=2))
    print(f"[panels] wrote {len(panels)} panels back into {narration_path.name}")
    return panels


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="historyrecapped")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--target-panel-s", type=float, default=22.0,
                    help="Target audio seconds per panel (default 22)")
    ap.add_argument("--title", default="", help="Episode title for LLM context")
    ap.add_argument("--era", default="", help="Episode era for LLM grounding")
    ap.add_argument("--model", default="haiku", choices=["haiku", "sonnet", "opus"])
    ap.add_argument("--overwrite", action="store_true",
                    help="Regenerate panels even if narration JSON already has them")
    args = ap.parse_args()

    nar = REPO_ROOT / args.channel / "narrations" / f"{args.slug}.json"
    if not nar.exists():
        raise SystemExit(f"missing narration: {nar}")

    author_panels(
        nar,
        target_panel_s=args.target_panel_s,
        title=args.title,
        era=args.era,
        model=args.model,
        overwrite=args.overwrite,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
