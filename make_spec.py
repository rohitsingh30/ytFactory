"""Build a per-Short video spec from a script + a channel's spec_template.

Pipeline:

    pull_stories.py     →  data/intermediate/<channel>/raw/<slug>.json
    /make-script        →  data/intermediate/<channel>/scripts/<slug>.json
    make_spec.py        →  data/intermediate/<channel>/specs/<slug>.yaml
    render_from_spec.py →  data/shorts/<slug>.mp4

This step substitutes `${script.<path>}`, `${raw.<path>}`, and
`${channel.<path>}` references in the channel's `spec_template` block.
Other `${...}` references (template-internal: `${width}`, `${btn.bg}`,
etc.) are left untouched for the render-time engine.

Run:

    .venv/bin/python make_spec.py \\
        --channel channels/aita_cooking.yaml \\
        --script  data/intermediate/aita_cooking/scripts/<slug>.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml


# Substitute only references with these namespaces. Other ${...}
# references (template-internal width/btn/i/etc.) are left as-is.
_NAMESPACES = ("script", "raw", "channel")
_PATTERN = re.compile(
    r"\$\{(" + "|".join(_NAMESPACES) + r")\.([\w.\-]+)\}"
)


def _lookup_path(path: str, ns: dict) -> Any:
    parts = path.split(".")
    v: Any = ns
    for p in parts:
        if isinstance(v, dict):
            v = v[p]
        elif isinstance(v, (list, tuple)) and p.lstrip("-").isdigit():
            v = v[int(p)]
        else:
            v = getattr(v, p)
    return v


def substitute(value: Any, ns: dict) -> Any:
    """Recursively substitute ${script|raw|channel.<path>} in strings.

    Leaves other ${...} references intact for the render-time engine.
    """
    if isinstance(value, dict):
        return {k: substitute(v, ns) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, ns) for v in value]
    if not isinstance(value, str):
        return value

    # Pure substitution: the whole string is "${ns.path}" → return raw value.
    full = _PATTERN.fullmatch(value.strip())
    if full:
        return _lookup_path(f"{full.group(1)}.{full.group(2)}", ns)

    # Embedded: text-interpolate (always stringified).
    if _PATTERN.search(value):
        return _PATTERN.sub(
            lambda m: str(_lookup_path(f"{m.group(1)}.{m.group(2)}", ns)),
            value,
        )

    return value


def make_spec(channel_path: Path, script_path: Path, raw_path: Path | None, out_dir: Path) -> Path:
    channel = yaml.safe_load(channel_path.read_text())
    if not isinstance(channel, dict) or "spec_template" not in channel:
        raise ValueError(f"channel {channel_path} has no spec_template block")

    script = json.loads(script_path.read_text())

    # Auto-locate raw if not given. Convention: <channel-dir>/raw/<slug>.json
    if raw_path is None:
        guess = script_path.parent.parent / "raw" / f"{script['slug']}.json"
        raw_path = guess if guess.exists() else None
    raw = json.loads(raw_path.read_text()) if raw_path and raw_path.exists() else {}

    ns = {"script": script, "raw": raw, "channel": channel}
    spec = substitute(channel["spec_template"], ns)

    slug = spec.get("slug") or script["slug"]
    if out_dir is None:
        # default: data/intermediate/<channel-name>/specs/
        out_dir = script_path.parent.parent / "specs"
    out_path = out_dir / f"{slug}.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    out_path.write_text(
        yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, default_flow_style=False)
    )
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--channel", required=True, help="channels/<name>.yaml")
    ap.add_argument("--script",  required=True, help="data/intermediate/<channel>/scripts/<slug>.json")
    ap.add_argument("--raw",     default=None,  help="optional raw story json (auto-located by default)")
    ap.add_argument("--out",     default=None,  help="output dir (default: <script-dir>/../specs)")
    args = ap.parse_args()

    out_path = make_spec(
        channel_path=Path(args.channel),
        script_path=Path(args.script),
        raw_path=Path(args.raw) if args.raw else None,
        out_dir=Path(args.out) if args.out else None,
    )
    print(f"✓ wrote {out_path}")


if __name__ == "__main__":
    main()
