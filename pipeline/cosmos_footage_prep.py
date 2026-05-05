"""Auto source-fetch + Ken Burns prep for footage-only channels (Cosmos Decoded).

Runs after a /make-cosmos-decoder authoring pass. Reads the channel/slug
shotlist, fetches every `source_url` to disk, and converts every
`still_ken_burns` entry to an mp4 clip via ffmpeg zoompan. Idempotent:
re-runs skip already-present files.

Why this exists (class-of-bug fix, 2026-05-05):
    The cosmosdecoded long-form footage_only renderer expects local mp4
    files in `<channel>/footage/long_sources/` (long-form) or
    `<channel>/footage/sources/` (Shorts). Authoring shotlists in the
    /make-cosmos-decoder skill only produced URLs + filenames — leaving
    the curator to manually wget every asset and run ffmpeg-zoompan
    one-liners. Every future invocation of the skill would have hit the
    same gap. This module bakes the prep step into the skill flow so
    the renderer can be invoked immediately after authoring.

Resolvers supported:
    * commons.wikimedia.org/wiki/File:Foo.jpg   → direct asset URL via
        Wikimedia API; downloaded as a still and Ken-Burns'd to mp4.
    * upload.wikimedia.org/wikipedia/commons/.. → direct download.
    * images.nasa.gov/details/<nasa_id>          → highest-res asset
        via NASA images-api.
    * archive.org/details/<id>                   → mp4 via archive.org
        download path.
    * direct .mp4 / .jpg / .png / .webp URLs     → urllib download.
    * royalsocietypublishing.org / nature.com /
      arxiv.org / pexels.com / others           → MANUAL fallback
        (logged with a clear curator instruction).

CLI:
    .venv/bin/python -m pipeline.cosmos_footage_prep \\
        --channel cosmosdecoded --slug eddington-1919-eclipse
    .venv/bin/python -m pipeline.cosmos_footage_prep \\
        --channel cosmosdecoded --slug eddington-1919-eclipse-short
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

UA = "ytFactory/cosmos-decoded-prep (+rohittomar@microsoft.com)"

ASPECT_DIMS = {"16:9": (1920, 1080), "9:16": (1080, 1920)}


@dataclass
class PrepResult:
    fetched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    manual: list[tuple[str, str, str]] = field(default_factory=list)  # (source, url, reason)
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # (source, url, error)


def _http_get(url: str, dest: Path, *, timeout: int = 60, expect_kind: str = "any") -> None:
    """Stream a URL to a file with a UA header. ``expect_kind`` is a
    content-type guard — pass 'image' or 'video' to fail loudly if the
    server returns text/html (i.e. the URL was not actually a direct
    asset)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ct = (r.headers.get("Content-Type") or "").lower()
        if expect_kind == "image" and "image/" not in ct:
            raise RuntimeError(f"expected image, got Content-Type {ct!r} — URL probably resolves to a wiki page rather than a direct asset")
        if expect_kind == "video" and "video/" not in ct and "octet-stream" not in ct:
            raise RuntimeError(f"expected video, got Content-Type {ct!r}")
        with dest.open("wb") as f:
            shutil.copyfileobj(r, f)


def _resolve_wikimedia(url: str) -> Optional[str]:
    """Resolve a `commons.wikimedia.org/wiki/File:Name.ext` URL to its direct upload URL."""
    parsed = urllib.parse.urlparse(url)
    # Already a direct upload URL — return as-is.
    if parsed.netloc == "upload.wikimedia.org":
        return url
    if "wikimedia.org" not in parsed.netloc and "wikipedia.org" not in parsed.netloc:
        return None
    # Extract File: title from path. Patterns:
    #   /wiki/File:Foo.jpg
    #   /wiki/Category:...     (skip — not an asset)
    path = urllib.parse.unquote(parsed.path)
    if "/wiki/File:" not in path:
        return None
    title = path.split("/wiki/", 1)[1]  # "File:Foo.jpg"
    api = (
        "https://commons.wikimedia.org/w/api.php?action=query"
        f"&titles={urllib.parse.quote(title)}"
        "&prop=imageinfo&iiprop=url&format=json&iiurlwidth=2400"
    )
    try:
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    pages = data.get("query", {}).get("pages", {})
    for _, page in pages.items():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        # iiurlwidth produces a thumbnail at the requested width — better
        # for Ken Burns than the full multi-MB original.
        return info.get("thumburl") or info.get("url")
    return None


def _resolve_nasa_image(url: str) -> Optional[str]:
    """Resolve a `images.nasa.gov/details/<id>` URL to its highest-res asset URL."""
    parsed = urllib.parse.urlparse(url)
    if "images.nasa.gov" not in parsed.netloc:
        return None
    if "/details/" not in parsed.path:
        return None
    nasa_id = parsed.path.split("/details/", 1)[1].rstrip("/").split(".")[0]
    api = f"https://images-api.nasa.gov/asset/{urllib.parse.quote(nasa_id)}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    items = data.get("collection", {}).get("items", [])
    # Pick the largest .jpg / .mp4 by URL pattern (orig > large > medium > small).
    rank = {"orig": 0, "large": 1, "medium": 2, "small": 3, "thumb": 4}
    best = None
    best_rank = 999
    for it in items:
        href = it.get("href") or ""
        if not href:
            continue
        for token, r in rank.items():
            if f"~{token}." in href and r < best_rank:
                best = href
                best_rank = r
                break
    if not best and items:
        best = items[0].get("href")
    return best


def _resolve_archive_org(url: str) -> Optional[str]:
    """Resolve a archive.org details URL to the primary mp4 download URL."""
    parsed = urllib.parse.urlparse(url)
    if "archive.org" not in parsed.netloc:
        return None
    if "/details/" not in parsed.path:
        return url if "/download/" in parsed.path else None
    item_id = parsed.path.split("/details/", 1)[1].rstrip("/")
    api = f"https://archive.org/metadata/{urllib.parse.quote(item_id)}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    files = data.get("files", [])
    # Prefer h264 mp4. Fall back to any mp4. Then any video.
    for want_format in ("h.264", "MPEG4", "512Kb MPEG4", "h.264 IA"):
        for f in files:
            if f.get("format") == want_format:
                return f"https://archive.org/download/{item_id}/{urllib.parse.quote(f['name'])}"
    for f in files:
        n = f.get("name") or ""
        if n.lower().endswith(".mp4"):
            return f"https://archive.org/download/{item_id}/{urllib.parse.quote(n)}"
    return None


def _resolve_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (resolved_url, manual_reason). One of the two is non-None.

    Dispatch order matters: host-specific resolvers run BEFORE the
    'looks like a direct asset' fallback, because wiki File: pages end
    in `.jpg` / `.png` / `.svg` extensions yet serve HTML at those URLs.
    """
    parsed = urllib.parse.urlparse(url)

    # Host-specific resolvers first.
    if parsed.netloc == "upload.wikimedia.org":
        return url, None  # already a direct upload URL
    if "wikimedia.org" in parsed.netloc or "wikipedia.org" in parsed.netloc:
        r = _resolve_wikimedia(url)
        if r:
            return r, None
        return None, "could not resolve wikimedia URL → check that the File: page exists"

    if "images.nasa.gov" in parsed.netloc:
        r = _resolve_nasa_image(url)
        if r:
            return r, None
        return None, "could not resolve NASA images-api asset → check the /details/<id> path"

    if "archive.org" in parsed.netloc:
        r = _resolve_archive_org(url)
        if r:
            return r, None
        return None, "could not resolve archive.org collection — the item may be unavailable"

    # Fallback: direct asset URL (any other host with a known extension).
    if any(parsed.path.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".gif")):
        return url, None

    if any(d in parsed.netloc for d in (
        "royalsocietypublishing.org", "nature.com", "wiley.com", "arxiv.org",
        "ligo.caltech.edu", "ligo.org", "pexels.com", "pixabay.com", "storyblocks.com",
        "thetimes.co.uk", "timesmachine.nytimes.com", "articles.adsabs.harvard.edu",
    )):
        return None, "site requires manual download (paywall / login / scraping protection)"

    return None, "unrecognised host — fetch manually"


def _kenburns(still: Path, out_path: Path, *, duration_s: float, aspect: str, fps: int = 30) -> None:
    """Convert a still image into an N-second 1080x1920 (9:16) or 1920x1080 (16:9)
    mp4. Footage-only; the renderer adds blurred-letterbox at concat time.

    Speed-tuned (2026-05-05): the original zoompan-based ken-burns ran
    ~2-3 minutes per clip on M2 Max (zoompan on a looped still is
    pathologically slow inside libavfilter). This version uses `-tune
    stillimage` + a single scale+pad pass — runs in ~0.5-2s per clip.
    Trade-off: no on-clip pan/zoom motion. For a 25-min footage-only
    decoder with 5-22s cuts, the cut tempo provides enough motion;
    intra-clip Ken Burns animation is a follow-up engineering item."""
    out_w, out_h = ASPECT_DIMS[aspect]
    # Scale to fit output aspect (preserve aspect ratio, pad with black).
    vf = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-framerate", str(fps), "-i", str(still),
        "-t", f"{duration_s:.3f}", "-vf", vf,
        "-r", str(fps), "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-an", str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def _entries_from_shotlist(shotlist: dict) -> tuple[list[dict], str]:
    """Return (entries, container_field). Long-form shotlists use `clips`; Shorts use `windows`."""
    if shotlist.get("clips"):
        return shotlist["clips"], "clips"
    if shotlist.get("windows"):
        return shotlist["windows"], "windows"
    raise ValueError("shotlist has neither `clips` nor `windows`")


def prep_shotlist(channel: str, slug: str, *, force: bool = False) -> PrepResult:
    """Fetch every source_url + Ken Burns convert every still_ken_burns. Idempotent."""
    chan_dir = REPO_ROOT / channel
    shotlist_path = chan_dir / "shotlist" / f"{slug}.json"
    if not shotlist_path.exists():
        raise SystemExit(f"missing shotlist: {shotlist_path}")
    shotlist = json.loads(shotlist_path.read_text())
    entries, container = _entries_from_shotlist(shotlist)
    aspect = shotlist.get("aspect", "16:9")
    if aspect not in ASPECT_DIMS:
        raise SystemExit(f"unsupported aspect {aspect!r}; expected 16:9 or 9:16")

    # Long-form sources go in footage/long_sources/, Shorts in footage/sources/.
    sources_subdir = "long_sources" if container == "clips" else "sources"
    sources_dir = chan_dir / "footage" / sources_subdir
    sources_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = chan_dir / "cache" / f"_prep_{slug}"
    cache_dir.mkdir(parents=True, exist_ok=True)

    res = PrepResult()
    print(f"[prep] {channel}/{slug}  aspect={aspect}  {len(entries)} entries  → {sources_dir}")

    for i, entry in enumerate(entries):
        src_name = entry.get("source")
        url = entry.get("source_url")
        kind = entry.get("source_type", "video")
        in_s = float(entry.get("in_s", 0.0))
        out_s = float(entry.get("out_s", 8.0))
        if not src_name:
            res.errors.append((f"<entry {i}>", str(url), "no `source` filename"))
            continue

        dest = sources_dir / src_name
        if dest.exists() and not force:
            res.skipped.append(src_name)
            continue

        if not url:
            res.manual.append((src_name, "<no source_url>", "shotlist entry missing source_url"))
            continue

        resolved, reason = _resolve_url(url)
        if not resolved:
            res.manual.append((src_name, url, reason or "unsupported host"))
            print(f"[prep] {i+1}/{len(entries)}  MANUAL  {src_name}  ({reason})")
            continue

        try:
            if kind == "still_ken_burns":
                # Download still → cache, then Ken-Burns → dest.
                still_ext = Path(urllib.parse.urlparse(resolved).path).suffix or ".jpg"
                still_path = cache_dir / f"{src_name}.still{still_ext}"
                if not still_path.exists():
                    print(f"[prep] {i+1}/{len(entries)}  fetch still  {src_name}")
                    _http_get(resolved, still_path, expect_kind="image")
                duration = max(1.0, out_s - in_s)
                print(f"[prep] {i+1}/{len(entries)}  ken-burns  {src_name}  {duration:.1f}s {aspect}")
                _kenburns(still_path, dest, duration_s=duration, aspect=aspect)
            else:
                # Direct download.
                print(f"[prep] {i+1}/{len(entries)}  fetch video  {src_name}")
                _http_get(resolved, dest, timeout=300, expect_kind="video")
                if not dest.exists() or dest.stat().st_size < 1024:
                    raise RuntimeError(f"download produced empty file")
            res.fetched.append(src_name)
        except Exception as e:
            res.errors.append((src_name, url, f"{type(e).__name__}: {e}"))
            print(f"[prep] {i+1}/{len(entries)}  ERROR  {src_name}  {type(e).__name__}: {e}")
            # Tidy up partial download.
            if dest.exists() and dest.stat().st_size < 1024:
                dest.unlink()

    print()
    print(f"[prep] DONE channel={channel} slug={slug}")
    print(f"       ✓ fetched: {len(res.fetched)}")
    print(f"       ✓ skipped (already on disk): {len(res.skipped)}")
    print(f"       ⚠ manual fallback needed: {len(res.manual)}")
    print(f"       ✗ errors: {len(res.errors)}")
    if res.manual:
        print()
        print("[prep] MANUAL FETCH NEEDED — paste each URL into a browser, save the asset to:")
        print(f"       {sources_dir}/<source-filename>")
        for src, url, reason in res.manual:
            print(f"       • {src}\n         from {url}\n         reason: {reason}")
    if res.errors:
        print()
        print("[prep] ERRORS — fetch attempted but failed:")
        for src, url, err in res.errors:
            print(f"       • {src} ← {url}\n         {err}")
    return res


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--channel", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even if destination file already exists")
    args = ap.parse_args(argv)
    res = prep_shotlist(args.channel, args.slug, force=args.force)
    return 0 if not res.errors else 1


if __name__ == "__main__":
    sys.exit(main())
