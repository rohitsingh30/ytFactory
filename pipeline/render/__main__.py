"""``python -m pipeline.render`` — engine-driven CLI entry point.

NEW (2026-05-14): the bigbang PR's hard-cutover CLI per user direction.
Replaces the legacy `python -m pipeline.render.shorts --script foo.json`
chain with a single engine-driven entry that takes a typed RenderSpec
shape and routes via :func:`pipeline.render.video.render_via_engines`.

Usage
-----

::

    python -m pipeline.render \\
        --kind short \\
        --channel mystoriesanimated \\
        --slug aita-001 \\
        --script <path/to/script.json> \\
        --out <path/to/out.mp4>

    # OR for long:
    python -m pipeline.render \\
        --kind long \\
        --channel historyrecapped \\
        --slug 1066 \\
        --script <envelope.json> \\
        --out <out.mp4>

All other RenderSpec fields (visual_mode, voice_provider, music_policy,
captions_layout, etc.) are picked up from the channel YAML's
``defaults: { short / long }`` block via the standard build_spec()
chain. Repeated ``--override KEY=VALUE`` flags inject form-style
overrides (same shape as the cloud worker's --override flags).

Coexistence with the legacy ``python -m pipeline.render.shorts``:

The legacy CLI shims continue to work in this branch. The bigbang
merge that deletes the legacy renderers will also drop their
``cli_main()`` modules; this entry point becomes the only path then.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from pipeline.paths import RenderPaths
from pipeline.render.spec import build_spec
from pipeline.render.video import render_via_engines


_logger = logging.getLogger(__name__)


def _parse_overrides(raw: list[str]) -> dict[str, str]:
    """Parse ``--override KEY=VALUE`` repeats into a flat dict."""
    out: dict[str, str] = {}
    for entry in raw or []:
        if "=" not in entry:
            raise SystemExit(f"--override expects KEY=VALUE, got {entry!r}")
        k, v = entry.split("=", 1)
        out[k.strip()] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pipeline.render",
        description="Engine-driven render entry point. Dispatches via "
                    "pipeline.render.video.render_via_engines through the "
                    "short or long engine, which in turn calls plugin slots "
                    "(audio / timeline / visualize / overlays / music / compose).",
    )
    ap.add_argument("--kind", choices=["short", "long"], required=True,
                    help="short = 9:16, single-pass TTS, beat-driven. "
                         "long = 16:9, chunked TTS, section-driven.")
    ap.add_argument("--channel", required=True,
                    help="Channel slug (e.g. mystoriesanimated) OR path to "
                         "channel config.yaml. RenderPaths resolves both.")
    ap.add_argument("--slug", required=True,
                    help="Render slug — used to derive output path under the "
                         "channel layout (RenderPaths.short_for / long_form_for).")
    ap.add_argument("--script", required=True, type=Path,
                    help="Path to the script JSON the engine consumes "
                         "(beats[] for short, sections[] for long).")
    ap.add_argument("--out", default=None, type=Path,
                    help="Output mp4 path. Default: derived from RenderPaths "
                         "+ kind via short_for(slug) / long_form_for(slug).")
    ap.add_argument("--work-dir", default=None, type=Path,
                    help="Scratch dir for the engine. Default: <channel>/cache/<slug>/.")
    ap.add_argument("--niche", default=None,
                    help="Optional variant niche slug (e.g. aita_animated).")
    ap.add_argument("--override", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="Repeated form-style override forwarded to "
                         "build_spec via channel_overrides. Honored keys "
                         "include voice / music_bed / music_policy / "
                         "captions_layout / lower_thirds / chapter_cards / "
                         "overlay_timeline / critic_loop / tone / "
                         "audio_mode / visual_source / etc.")
    args = ap.parse_args(argv)

    if not args.script.exists():
        raise SystemExit(f"--script not found at {args.script}")

    # Power-check guard — was wired into every legacy renderer entry; lives
    # at the engine dispatch entry now so both engines inherit it. Refuses
    # macOS Low-Power-Mode (Metal frame-budget thrash → OOM crash class).
    try:
        from pipeline.preflight import power_check  # noqa: PLC0415
        power_check(label=f"render {args.kind}")
    except Exception:
        pass

    overrides = _parse_overrides(args.override)
    overrides.setdefault("kind", "long_form" if args.kind == "long" else "short")

    # Resolve channel paths.
    if args.channel.endswith(".yaml") or "/" in args.channel:
        channel_yaml_path = Path(args.channel)
        rp = RenderPaths.from_channel_yaml(channel_yaml_path)
    else:
        rp = RenderPaths.from_channel_dir(args.channel)
        channel_yaml_path = rp.config_yaml

    spec = build_spec(
        proposal={
            "channel": args.channel.split("/")[-1].replace(".yaml", ""),
            "format": args.niche,
            "channel_overrides": overrides,
        },
        channel_yaml_path=channel_yaml_path if channel_yaml_path.exists() else None,
        variant_yaml_path=None,
    )

    # Resolve out_path + work_dir.
    if args.out is None:
        out_path = (rp.short_for(args.slug) if args.kind == "short"
                    else rp.long_form_for(args.slug))
    else:
        out_path = args.out

    work_dir = args.work_dir or rp.cache_for(args.slug)

    script_dict = json.loads(args.script.read_text())

    print(f"[pipeline.render] kind={args.kind} channel={args.channel} "
          f"slug={args.slug}")
    print(f"[pipeline.render] spec.visual_mode={spec.visual_mode.value} "
          f"music_policy={spec.music_policy.value} aspect={spec.aspect_ratio}")
    print(f"[pipeline.render] out={out_path} work_dir={work_dir}")

    mp4 = render_via_engines(
        spec=spec,
        script=script_dict,
        work_dir=work_dir,
        out_path=out_path,
    )

    # OUTPUT_MANIFEST — same convention as the legacy CLI shims so the
    # cloud worker's log tailer + any external consumer keep working.
    manifest = {
        "mp4": str(mp4),
        "thumb": None,  # engine doesn't yet emit a thumb — bigbang TODO
        "slug": args.slug,
        "channel_dir": rp.channel_dir,
    }
    print(f"OUTPUT_MANIFEST: {json.dumps(manifest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
