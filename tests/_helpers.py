"""Shared test helpers: fake Beat objects, project-root sys.path fix."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

# Make the project importable when tests are run from any cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _real_googleapiclient_spec():
    """Return ``importlib.util.find_spec('googleapiclient')`` resolved
    against the real on-disk package, NOT whatever fake some prior
    test stuffed into ``sys.modules``. Returns None when the real
    package isn't installed (CI minimal image, future expectation).

    Why temporarily evict sys.modules entries: ``find_spec`` checks
    sys.modules first and returns the cached module's spec when found.
    A previous test's fake ``googleapiclient`` shows up there with
    ``__path__ = []``, masking the real package on disk.
    """
    saved: dict[str, types.ModuleType | None] = {}
    for key in list(sys.modules.keys()):
        if key == "googleapiclient" or key.startswith("googleapiclient."):
            saved[key] = sys.modules.pop(key)
    try:
        return importlib.util.find_spec("googleapiclient")
    except Exception:  # noqa: BLE001
        return None
    finally:
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v


def make_fake_googleapiclient_pkg() -> types.ModuleType:
    """Build a fake ``googleapiclient`` package object that's safe to drop
    into ``sys.modules`` without breaking later imports of submodules.

    POLLUTION-SAFE: when the real ``googleapiclient`` package is installed
    (CI or laptop full venv), copy its ``__path__`` and mirror any
    already-loaded ``googleapiclient.*`` submodules onto the fake. That
    way a later test doing ``from googleapiclient.http import
    MediaFileUpload`` still resolves either via the cached attribute on
    the package OR via the real package's __path__ + import system.

    Pre-fix the various ad-hoc fakes scattered across
    ``test_research_youtube.py`` /
    ``test_research_cross_engage.py`` /
    ``test_pipeline_youtube_stats.py`` had no ``__path__`` and
    preserved no submodules, so a later import of
    ``googleapiclient.http`` raised
    ``ModuleNotFoundError: 'googleapiclient' is not a package`` and
    every test_upload_youtube test that touched MediaFileUpload turned
    into a CI ERROR. Fixed 2026-05-11.
    """
    fake_pkg = types.ModuleType("googleapiclient")

    real_path: list[str] = []
    spec = _real_googleapiclient_spec()
    if spec and spec.submodule_search_locations:
        real_path = list(spec.submodule_search_locations)
    fake_pkg.__path__ = real_path  # type: ignore[attr-defined]

    # Mirror any already-loaded real submodules onto the fake so later
    # ``from googleapiclient.X import Y`` resolves via getattr without
    # re-invoking the loader.
    for name, mod in list(sys.modules.items()):
        if name.startswith("googleapiclient."):
            short = name.split(".", 1)[1]
            if "." not in short:  # only direct submodules, not nested
                setattr(fake_pkg, short, mod)
    return fake_pkg


def make_fake_googleapiclient_errors() -> types.ModuleType:
    """Build a fake ``googleapiclient.errors`` module that's safe to
    drop into ``sys.modules`` even if the real ``googleapiclient.http``
    later imports symbols (e.g. ``BatchError``) from it.

    Strategy: clone the real ``googleapiclient.errors`` module (if
    importable) so all its public names survive, then OVERRIDE the
    HttpError with a tests-friendly version that accepts
    ``HttpError(resp, content)`` where ``resp`` may be a plain
    ``MagicMock`` with a ``.status`` attribute. The real HttpError
    requires a ``httplib2.Response`` shaped object and rejects bare
    Mocks at construction time, so tests need this looser variant.
    """
    real_errors = None
    pkg_spec = _real_googleapiclient_spec()
    if pkg_spec and pkg_spec.submodule_search_locations:
        # Use FileFinder against the real package's __path__ so we
        # bypass any fake currently sitting in sys.modules.
        try:
            spec = importlib.machinery.PathFinder.find_spec(
                "googleapiclient.errors",
                path=list(pkg_spec.submodule_search_locations),
            )
            if spec and spec.loader is not None:
                real_errors = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(real_errors)
        except Exception:  # noqa: BLE001
            real_errors = None

    fake = types.ModuleType("googleapiclient.errors")
    if real_errors is not None:
        for k, v in vars(real_errors).items():
            if not k.startswith("__"):
                setattr(fake, k, v)
    return fake


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
