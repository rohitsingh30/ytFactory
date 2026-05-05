"""Download archive.org PD WW2 Pacific films into the channel footage cache.

All three films are US Government public domain (Capra/Ford 1942-45). Zero
ContentID risk because nothing copyrighted sits on top.
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "historyrecapped/footage/long_sources"
OUT.mkdir(parents=True, exist_ok=True)

FILES = [
    # (slug,                       url,                                                                                                                                local_name)
    ("battle-of-midway-1942",     "https://archive.org/download/Battle.of.Midway.1942/Battle.of.Midway.1942.mp4",                                                       "battle-of-midway-1942.mp4"),
    ("know-your-enemy-japan-1945","https://archive.org/download/28232BKnowYourEnemyJapan/28232B%20Know%20Your%20Enemy%20Japan.mp4",                                       "know-your-enemy-japan-1945.mp4"),
    ("battle-of-china-1944",      "https://archive.org/download/BattleOfChina/BattleOfChina_512kb.mp4",                                                                 "battle-of-china-1944.mp4"),
]


def fetch(url: str, dst: Path) -> None:
    if dst.exists() and dst.stat().st_size > 1_000_000:
        print(f"[skip] {dst.name} already {dst.stat().st_size // 1024 // 1024} MB")
        return
    print(f"[get ] {dst.name} ← {url}")
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, dst.open("wb") as f:
        chunk_total = 0
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            chunk_total += len(chunk)
            if chunk_total % (10 * 1024 * 1024) < (1024 * 1024):
                print(f"  ... {chunk_total // 1024 // 1024} MB ({time.time()-t0:.1f}s)")
    print(f"[done] {dst.name} {dst.stat().st_size // 1024 // 1024} MB in {time.time()-t0:.1f}s")


def main() -> int:
    for slug, url, name in FILES:
        fetch(url, OUT / name)
    print()
    print("All sources cached at:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
