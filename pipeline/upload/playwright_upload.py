"""Playwright-driven YouTube Studio upload — programmatic entry.

Companion to the operator-facing ``/upload-via-playwright`` skill
(``.claude/skills/upload-via-playwright/SKILL.md``). The skill is the
canonical interactive driver; this module exposes a callable for the
control plane's publish route to invoke automatically when the API path
returns 403 quotaExceeded.

**Environment requirements** (any one of these missing → ``RuntimeError``):
  * ``playwright`` Python package installed.
  * ``YTFACTORY_CHROME_USER_DATA_DIR`` env var set to a Chrome
    ``--user-data-dir`` path where the operator is signed in to the
    target YouTube account. The skill writes its first-run readme to
    ``~/Library/Application Support/Google/Chrome-Debug`` on macOS.
  * The user-data-dir actually exists on disk.

On Cloud Run none of these are typically satisfied → the import-time
check in :func:`control.routes.render_routes._publish_via_playwright`
converts a ``RuntimeError`` here into a ``_PlaywrightUnavailable`` which
the route surfaces as ``status="queued_for_manual"`` so the operator
can run the skill manually from their laptop.

**Failure mode (per memory ``feedback_silent_fallback_unshippable_output``):**
if any precondition is missing we RAISE ``RuntimeError`` rather than
fall through to a no-op success. Silent failure here would tell the UI
the upload completed when it never started — unshippable.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _check_environment() -> None:
    """Verify playwright + signed-in Chrome profile are available.

    Raises ``RuntimeError`` with the specific missing precondition so
    the caller can surface an actionable error.
    """
    try:
        import playwright  # noqa: F401  PLC0415 — env probe import
    except ImportError as exc:
        raise RuntimeError(
            "playwright not installed; the playwright fallback is "
            "operator-only. Run /upload-via-playwright from your laptop."
        ) from exc

    profile_dir = os.environ.get("YTFACTORY_CHROME_USER_DATA_DIR", "").strip()
    if not profile_dir:
        raise RuntimeError(
            "YTFACTORY_CHROME_USER_DATA_DIR not set; the playwright "
            "path needs a signed-in Chrome profile. Run "
            "/upload-via-playwright from your laptop."
        )

    if not Path(profile_dir).expanduser().exists():
        raise RuntimeError(
            f"YTFACTORY_CHROME_USER_DATA_DIR={profile_dir!r} does not "
            f"exist on disk; the signed-in Chrome profile is missing."
        )


def playwright_upload(
    mp4_path: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    privacy: str = "private",
    publish_at: str | None = None,
    made_for_kids: bool = False,
    account: str = "default",
    thumbnail_path: Path | None = None,
    job_id: str | None = None,
    **_unused: Any,
) -> dict:
    """Drive a Studio-UI upload via playwright; return a dict matching
    :func:`pipeline.upload.upload.youtube_upload`'s shape.

    Today this only validates the environment + raises ``RuntimeError``
    when preconditions aren't met. The full Studio-driving loop lives
    in the ``/upload-via-playwright`` skill (which is what the operator
    invokes when this raises). Wiring the skill's logic into a
    programmatic Python flow is a separate work-stream — design notes
    in ``docs/playwright_with_signed_in_chrome.md``.

    Until that lands, the route at
    :func:`control.routes.render_routes.publish` will catch the
    ``RuntimeError`` from this module (via the ``_PlaywrightUnavailable``
    wrapper) and respond with ``status="queued_for_manual"`` so the UI
    surfaces the skill-run instruction to the user.
    """
    _check_environment()
    raise RuntimeError(
        "playwright_upload is not yet implemented as a programmatic "
        "flow; run /upload-via-playwright from the laptop to complete "
        f"the upload of {mp4_path.name} (account={account!r}, "
        f"job_id={job_id!r}). The skill drives studio.youtube.com "
        "end-to-end and writes the same uploads/<slug>.json record."
    )
