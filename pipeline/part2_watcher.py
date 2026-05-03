"""Part-2 watcher — autoship the cliffhanger finale once Part 1 pops.

Part-1 cliffhanger uploads drop a sidecar at
``data/intermediate/<part1_chan>/part2_pending/<slug>.json`` (see
``pipeline.upload.write_part2_pending``). Each sidecar carries the
baseline channel sub count at upload time, the threshold delta (default
+100 subs), the watch window (default 7 days), the Part-2 channel YAML
to render with, and everything Part 2 needs as input — the original raw
story, the Part-1 narration, and a pointer to the per-story cast.json.

This module polls every sidecar's account once per tick, computes
``current_subs - baseline_subs``, and fires Part-2 production for any
sidecar that has crossed its threshold. Production is end-to-end and
fully automatic: ``rewrite_part2()`` writes a fresh script JSON into
the Part-2 channel_dir, the per-story cast and raw files are copied
across so visuals stay continuous, and ``make_shorts.py --upload`` is
shelled out as a subprocess. The sidecar is deleted on a successful
exit; failed runs leave the sidecar in place so the next tick retries.

Run it manually for testing::

    python -m pipeline.part2_watcher --once

Long-running form (production)::

    python -m pipeline.part2_watcher

That sleeps 30 minutes between iterations. The website's Flask process
can spawn it as a background subprocess; until that wiring exists, run
it under a screen/tmux session or a launchd / systemd unit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import rewrite as rewrite_mod
from . import upload as upload_mod


PROJECT_ROOT = Path(__file__).resolve().parent.parent
POLL_INTERVAL_S = 30 * 60  # 30 minutes between watcher ticks


def _iter_pending_sidecars() -> list[Path]:
    """All current part2_pending sidecars across every channel."""
    base = PROJECT_ROOT / "data" / "intermediate"
    if not base.exists():
        return []
    return sorted(base.glob("*/part2_pending/*.json"))


def _load_sidecar(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[part2-watcher] skip {path.name}: unreadable ({e})")
        return None


def _is_expired(sidecar: dict) -> bool:
    expires_at = sidecar.get("window_expires_at")
    if not expires_at:
        return False
    try:
        when = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    return datetime.now(timezone.utc) >= when


def _channel_dir_from_yaml(yaml_path: Path) -> str:
    """Derive channel_dir from the YAML stem.

    Mirrors how make_shorts.py auto-detects channel_dir at upload time
    (it scans ``data/intermediate/<dir>/scripts/`` for the slug). Using
    the YAML stem keeps Part-2 outputs in their own directory so they
    don't clobber the Part-1 channel's files.
    """
    return yaml_path.stem


def _copy_continuity_files(
    part1_chan_dir: str, part2_chan_dir: str, slug: str
) -> None:
    """Copy cast.json + raw.json from the Part-1 channel dir to the
    Part-2 channel dir so make_shorts uses the same narrator and the
    same source-story metadata for the Part-2 render.

    Idempotent: skips files that already exist at the destination.
    """
    base = PROJECT_ROOT / "data" / "intermediate"
    src_chan = base / part1_chan_dir
    dst_chan = base / part2_chan_dir
    for sub in ("cast", "raw"):
        src = src_chan / sub / f"{slug}.json"
        if not src.exists():
            continue
        dst = dst_chan / sub / f"{slug}.json"
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"[part2-watcher]   copied {sub}/{slug}.json → {part2_chan_dir}/")


def _produce_and_upload(sidecar_path: Path, sidecar: dict) -> bool:
    """Run the full Part-2 pipeline for one triggered sidecar.

    Returns True on success (sidecar should be deleted), False on any
    failure (sidecar stays so the next tick retries).
    """
    slug = sidecar["slug"]
    part2_yaml = sidecar["part2_channel"]
    part1_chan_dir = sidecar["part1_channel_dir"]
    part2_chan_dir = _channel_dir_from_yaml(Path(part2_yaml))
    raw_story = sidecar.get("raw_story") or {}
    part1_narration = sidecar.get("part1_narration") or ""

    # Load Part-2 channel YAML — needed by rewrite_part2 for closer_format.
    part2_cfg_path = PROJECT_ROOT / part2_yaml
    if not part2_cfg_path.exists():
        print(f"[part2-watcher] {slug}: Part-2 YAML missing at {part2_yaml}")
        return False
    with part2_cfg_path.open() as f:
        part2_cfg = yaml.safe_load(f) or {}

    # Stage 3 — rewrite Part 2.
    try:
        script = rewrite_mod.rewrite_part2(
            raw_story=raw_story,
            part1_narration=part1_narration,
            channel_cfg=part2_cfg,
        )
    except Exception as e:
        print(f"[part2-watcher] {slug}: rewrite_part2 failed: {e}")
        return False

    # Save script to Part-2 channel_dir so make_shorts.py picks it up.
    script_path = (
        PROJECT_ROOT / "data" / "intermediate" / part2_chan_dir
        / "scripts" / f"{slug}.json"
    )
    rewrite_mod.save_script(script, script_path)
    print(f"[part2-watcher]   wrote Part-2 script → {script_path.relative_to(PROJECT_ROOT)}")

    # Carry cast + raw across so the Part-2 render keeps narrator + author info.
    _copy_continuity_files(part1_chan_dir, part2_chan_dir, slug)

    # Stages 4-8 — render + upload via make_shorts.py.
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "make_shorts.py"),
        "--script", str(script_path),
        "--channel", str(part2_yaml),
        "--upload",
    ]
    print(f"[part2-watcher]   running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=False)
    except Exception as e:
        print(f"[part2-watcher] {slug}: subprocess raised: {e}")
        return False
    if proc.returncode != 0:
        print(f"[part2-watcher] {slug}: make_shorts exited {proc.returncode}")
        return False

    print(f"[part2-watcher] ✓ {slug}: Part 2 rendered + uploaded")
    return True


def _tick_for_account(account: str, sidecars: list[tuple[Path, dict]]) -> None:
    """Process every sidecar for a single account in one pass.

    Single sub-count fetch per account (1 unit of YouTube quota) then
    compare each sidecar's baseline against it.
    """
    try:
        current_subs = upload_mod.get_channel_sub_count(account)
    except Exception as e:
        print(
            f"[part2-watcher] account={account!r}: sub-count read failed "
            f"({e}); skipping its sidecars this tick"
        )
        return

    print(f"[part2-watcher] account={account!r}: current_subs={current_subs}")
    for sc_path, sc in sidecars:
        slug = sc.get("slug") or sc_path.stem
        baseline = int(sc.get("baseline_subs") or 0)
        threshold = int(sc.get("threshold_subs_delta") or 100)
        delta = current_subs - baseline
        if delta < threshold:
            print(
                f"[part2-watcher]   {slug}: +{delta} subs / +{threshold} "
                f"required — waiting"
            )
            continue
        print(f"[part2-watcher]   {slug}: TRIGGERED (+{delta} ≥ +{threshold})")
        if _produce_and_upload(sc_path, sc):
            try:
                sc_path.unlink()
                print(f"[part2-watcher]   removed sidecar {sc_path.name}")
            except OSError as e:
                print(f"[part2-watcher]   sidecar unlink failed (non-fatal): {e}")


def tick() -> None:
    """One pass over every pending sidecar."""
    sidecars = _iter_pending_sidecars()
    if not sidecars:
        print("[part2-watcher] no pending Part-2 records")
        return

    by_account: dict[str, list[tuple[Path, dict]]] = {}
    for path in sidecars:
        sc = _load_sidecar(path)
        if sc is None:
            continue
        if _is_expired(sc):
            print(
                f"[part2-watcher] {sc.get('slug', path.stem)}: window expired "
                f"({sc.get('window_expires_at')}) — removing sidecar without firing"
            )
            try:
                path.unlink()
            except OSError as e:
                print(f"[part2-watcher]   unlink failed: {e}")
            continue
        acct = sc.get("account") or "default"
        by_account.setdefault(acct, []).append((path, sc))

    for acct, sc_list in by_account.items():
        _tick_for_account(acct, sc_list)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Watch for Part-1 cliffhangers that crossed their "
        "subscriber threshold and auto-render Part 2."
    )
    ap.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit (default: loop forever every 30 min).",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_S,
        help=f"Seconds between ticks (default: {POLL_INTERVAL_S}).",
    )
    args = ap.parse_args()

    if args.once:
        tick()
        return

    print(f"[part2-watcher] starting; interval={args.interval}s")
    while True:
        try:
            tick()
        except KeyboardInterrupt:
            print("[part2-watcher] interrupted")
            break
        except Exception as e:
            # Don't let one bad tick kill the daemon. Log and continue.
            print(f"[part2-watcher] tick crashed (continuing): {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
