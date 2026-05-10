"""Shared test helpers: fake Beat objects, project-root sys.path fix."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

# Make the project importable when tests are run from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class FakeWord:
    text: str
    start: float
    end: float


@dataclass
class FakeBeat:
    """Stand-in for `pipeline.audio.beats.Beat` that tests can build without
    pulling in Whisper or audio. Same .start/.end/.duration API."""

    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def fake_beat_list(spans: list[tuple[float, float]], text: str = "x") -> list[FakeBeat]:
    """Build a list of FakeBeat from (start, end) tuples."""
    return [FakeBeat(text=text, start=s, end=e) for s, e in spans]


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

class FakePath:
    """A lightweight fake for pathlib.Path supporting / (truediv), exists(),
    is_dir(), iterdir(), mkdir(), read_bytes/write_bytes/read_text/write_text,
    unlink."""

    def __init__(self, name: str = "", *, exists: bool = True, is_dir: bool | None = None):
        self.name = name
        self._exists = exists
        self._is_dir: bool | None = is_dir  # explicit override; None = auto
        self._children: dict[str, "FakePath"] = {}
        self._bytes_data: bytes = b""
        self._text_data: str = ""

    # truediv: create on demand so chained / operations work
    def __truediv__(self, other: str) -> "FakePath":
        if other not in self._children:
            child = FakePath(name=other, exists=self._exists)
            self._children[other] = child
        return self._children[other]

    def exists(self) -> bool:
        return self._exists

    def is_dir(self) -> bool:
        if self._is_dir is not None:
            return self._is_dir
        return bool(self._children)

    def iterdir(self):
        return iter(self._children.values())

    def mkdir(self, *, parents: bool = False, exist_ok: bool = False) -> None:
        self._exists = True

    def read_bytes(self) -> bytes:
        return self._bytes_data

    def write_bytes(self, data: bytes) -> None:
        self._bytes_data = data

    def read_text(self, errors: str = "strict") -> str:
        return self._text_data

    def write_text(self, data: str, **kwargs) -> None:
        self._text_data = data

    def unlink(self, *, missing_ok: bool = False) -> None:
        pass

    def replace(self, target: "FakePath") -> None:
        pass

    def with_suffix(self, suffix: str) -> "FakePath":
        return FakePath(name=self.name + suffix, exists=self._exists)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"FakePath({self.name!r})"

    def open(self, mode: str = "r", **kwargs):
        """Return a fake file object for open() calls."""
        import io
        if "b" in mode:
            return io.BytesIO(self._bytes_data)
        return io.StringIO(self._text_data)

    def resolve(self) -> "FakePath":
        return self


def make_fake_playwright(
    page_url: str = "https://www.youtube.com/watch?v=abc123",
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Return (mock_sp_callable, pw, browser, ctx, page).

    ``mock_sp_callable`` is suitable as the ``sync_playwright`` replacement::

        with patch("playwright.sync_api.sync_playwright", mock_sp):
            ...

    ``pw.chromium.connect_over_cdp(url)`` returns ``browser``.
    ``browser.contexts[0]`` is ``ctx``.
    ``ctx.new_page()`` returns ``page``.
    ``page.url`` is set to ``page_url``.
    """
    page = MagicMock()
    page.url = page_url

    ctx = MagicMock()
    ctx.new_page.return_value = page
    ctx.pages = [page]

    browser = MagicMock()
    browser.contexts = [ctx]

    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=pw)
    cm.__exit__ = MagicMock(return_value=False)

    mock_sp = MagicMock(return_value=cm)
    return mock_sp, pw, browser, ctx, page
