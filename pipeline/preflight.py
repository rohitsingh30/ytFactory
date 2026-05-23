"""Shared power-state preflight for long renders.

Refuses to start a render under power conditions that routinely crash
the macOS WindowServer watchdog. Originally lived inline in
``pipeline/render/long_form.py`` (function ``_preflight_power_check``);
extracted here in 2026-05-05 so the same guard applies to **every**
renderer entry point — long_form, footage_only, shorts, sports_doc.

History: incident F743A4C5 (2026-05-04) and the two SIGABRT
``com.Metal.CompletionQueueDispatch`` aborts on 2026-05-04 22:39 +
2026-05-05 00:50 all share the same preconditions:

    lowPowerMode: 1
    displayState: "OFF" (lid closed / display asleep)
    z_image_turbo holding the Metal command queue

Under Low Power Mode the GPU is clocked down. Long Metal command
buffers stretch from ~5–20 s to 40 s+. WindowServer also needs the
GPU to drive any UI and can't get a slot in time. The kernel
watchdog then either kills WindowServer (system logs out / panics)
or aborts the GPU command buffer (Python process dies with SIGABRT
on Metal's completion queue). From the user's POV both look like
"a memory error".

Override via ``YTFACTORY_SKIP_POWER_CHECK=1`` if you understand the
risk (e.g. desktop M2 Studio where WindowServer is on a different
display, or a recovery-from-crashed-run where you accept the risk).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys


def power_check(*, label: str = "render") -> None:
    """Block render start under known-crashy power conditions.

    - Hard-rejects macOS Low Power Mode (raises ``SystemExit``).
    - Warns on battery power but does not block.
    - No-op on non-Darwin platforms.
    - No-op when ``YTFACTORY_SKIP_POWER_CHECK=1``.

    ``label`` is included in the rejection / warning text so the user
    can see which renderer was about to start (e.g. ``"long-form"``,
    ``"footage-only Shorts"``, ``"sports-doc"``).
    """
    if os.environ.get("YTFACTORY_SKIP_POWER_CHECK") == "1":
        return
    if sys.platform != "darwin":
        return
    try:
        out = subprocess.check_output(
            ["pmset", "-g"], text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return  # pmset unavailable; don't block

    low_power = bool(re.search(r"lowpowermode\s+1\b", out))
    on_battery = "Battery Power" in out
    if low_power:
        raise SystemExit(
            f"Low Power Mode is ON. {label} renders hold the Metal command\n"
            "queue for tens of seconds at a time; under Low Power Mode the\n"
            "WindowServer watchdog times out and the system logs out or panics\n"
            "(or the Python process aborts with SIGABRT on the Metal completion\n"
            "queue — looks like a 'memory error').\n"
            "Disable Low Power Mode (Settings → Battery), or set\n"
            "YTFACTORY_SKIP_POWER_CHECK=1 to override at your own risk."
        )
    if on_battery:
        # Don't hard-block — running on battery is sometimes intentional.
        # Just warn and recommend caffeinate -dimsu (display, idle, mouse,
        # system, user).
        print(
            f"[preflight] WARNING: running {label} on battery. Prefer AC power +\n"
            "            lid open. If you must run on battery, wrap with\n"
            "            `caffeinate -dimsu` (NOT just -i) so the display\n"
            "            stays on — display-off + heavy MLX crashes WindowServer.",
            file=sys.stderr,
        )


def reset_mlx_state(*, drop_f5: bool = False, drop_image: bool = False, label: str = "") -> None:
    """Drop in-process MLX singletons + flush the Metal cache.

    Call this at a renderer-stage boundary when an MLX-heavy stage has
    finished and the next stages (ffmpeg, PIL, libass) won't need the
    model. Free unified-memory headroom and reduces the chance of the
    next Metal-using stage hitting fragmentation.

    ``drop_f5`` (kept as a no-op for backward-compat; local TTS providers
    that held an MLX singleton were retired — kwarg is ignored).

    ``drop_image`` (default False): drops the z_image / mflux pipes
    from ``pipeline.images`` (~3-4 GB resident). Only set True when
    the run will not generate more images.

    ``label`` is included in the print line so the user can see which
    renderer-stage boundary cleared the heap.

    Best-effort: if MLX isn't loaded, or any of the targeted modules
    aren't importable, this function silently no-ops. Never raises —
    this is a hygiene call, not a correctness call.
    """
    del drop_f5  # retired — kwarg kept for backward-compat
    cleared: list[str] = []
    if drop_image:
        try:
            from pipeline import images as _img  # type: ignore  # noqa: PLC0415
            if hasattr(_img, "reset_image_state"):
                _img.reset_image_state()
                cleared.append("image")
        except Exception as e:  # noqa: BLE001
            print(f"[mem] reset_image_state failed: {e}", file=sys.stderr)
    try:
        import mlx.core as _mx  # type: ignore  # noqa: PLC0415
        if hasattr(_mx, "clear_cache"):
            _mx.clear_cache()
        elif hasattr(_mx, "metal") and hasattr(_mx.metal, "clear_cache"):
            _mx.metal.clear_cache()
        cleared.append("metal-cache")
    except Exception:  # noqa: BLE001
        pass
    if cleared:
        suffix = f" (after {label})" if label else ""
        print(f"[mem] dropped {' + '.join(cleared)}{suffix}")
