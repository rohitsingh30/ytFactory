"""Test package init — installs a global safety net that refuses to launch
real browsers (Chrome / Chromium / Firefox) or real Playwright sessions
no matter which test forgets to mock them.

Background: 2026-05-10 — a sub-agent's tests for cross-engagement code
forgot to patch every subprocess.Popen call site, and the full suite
launched real Chrome with the user's profile against youtube.com.
This module raises immediately on any such attempt so a missed mock
becomes a loud test failure instead of a real browser window.
"""

from __future__ import annotations

import os as _os
import subprocess as _subprocess

_BROWSER_NEEDLES = (
    "google chrome",
    "google-chrome",
    "/applications/google chrome.app",
    "chrome.app/contents/macos/google chrome",
    "chromium",
    "/chromium",
    "firefox",
    "msedge",
    "microsoft edge",
)
_PLAYWRIGHT_NEEDLES = (
    ".cache/ms-playwright",
    "ms-playwright",
)


class TestSafetyError(RuntimeError):
    """Raised when a test attempts to launch a real browser."""


def _looks_like_browser(argv) -> bool:
    if argv is None:
        return False
    if isinstance(argv, (str, bytes, _os.PathLike)):
        if isinstance(argv, bytes):
            s = argv.decode("utf-8", "ignore").lower()
        else:
            s = _os.fspath(argv).lower()
        return any(n in s for n in _BROWSER_NEEDLES) or any(n in s for n in _PLAYWRIGHT_NEEDLES)
    try:
        parts = []
        for p in argv:
            if isinstance(p, bytes):
                parts.append(p.decode("utf-8", "ignore"))
            else:
                parts.append(_os.fspath(p))
        joined = " ".join(parts).lower()
    except Exception:
        return False
    return any(n in joined for n in _BROWSER_NEEDLES) or any(n in joined for n in _PLAYWRIGHT_NEEDLES)


_real_popen = _subprocess.Popen
_real_run = _subprocess.run


class _BlockingPopen(_real_popen):  # type: ignore[misc]
    def __init__(self, args=None, *posargs, **kwargs):
        if _looks_like_browser(args):
            raise TestSafetyError(
                f"Refusing to launch real browser from test: {args!r}. "
                "Patch subprocess.Popen (or pipeline.<module>.subprocess.Popen) in your test."
            )
        super().__init__(args, *posargs, **kwargs)


def _blocking_run(args=None, *posargs, **kwargs):
    if _looks_like_browser(args):
        raise TestSafetyError(
            f"Refusing to invoke real browser from test: {args!r}. "
            "Patch subprocess.run in your test."
        )
    return _real_run(args, *posargs, **kwargs)


_subprocess.Popen = _BlockingPopen  # type: ignore[assignment]
_subprocess.run = _blocking_run  # type: ignore[assignment]
