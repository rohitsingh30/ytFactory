#!/usr/bin/env python
"""Smoke test for ytfactory-tts-cosyvoice Cloud Run service.

Usage:
  URL=$(gcloud run services describe ytfactory-tts-cosyvoice \
        --region=asia-southeast1 --project=ytfactory-prod \
        --format='value(status.url)')
  python cloud/tts-cosyvoice/smoke_test.py "$URL"

Renders two clips:
  - English (Sarah voice): a short narration line
  - Hindi (Sarah voice):   the Bhagavad Gita verse from the bench
                            harness sample 05_hindi_kathaa
Writes them to /tmp/cosyvoice-smoke-{en,hi}.wav.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REF_WAV = REPO_ROOT / "pipeline" / "voice_refs" / "sarah.wav"
REF_TXT = REPO_ROOT / "pipeline" / "voice_refs" / "sarah.txt"

EN_TEXT = (
    "In nineteen nineteen, on a small island off the coast of West Africa, "
    "Arthur Eddington pointed his telescope at the eclipsed sun."
)
HI_TEXT = (
    "श्रीभगवानुवाच — यदा यदा हि धर्मस्य ग्लानिर्भवति भारत। "
    "अभ्युत्थानमधर्मस्य तदात्मानं सृजाम्यहम्। "
    "परित्राणाय साधूनां विनाशाय च दुष्कृताम्।"
)


def _token() -> str:
    return subprocess.check_output(
        ["gcloud", "auth", "print-identity-token"], text=True
    ).strip()


def _post(url: str, token: str, body: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        url=f"{url}/synth",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str, token: str, path: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        f"{url}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(url: str) -> None:
    url = url.rstrip("/")
    token = _token()

    print(f"==> healthz")
    print(json.dumps(_get(url, token, "/healthz"), indent=2))

    print(f"\n==> readyz (warms model — first call ~5-8 s)")
    t0 = time.time()
    print(json.dumps(_get(url, token, "/readyz", timeout=300), indent=2))
    print(f"   wall: {time.time()-t0:.1f}s")

    ref_b64 = base64.b64encode(REF_WAV.read_bytes()).decode("ascii")
    ref_text = REF_TXT.read_text(encoding="utf-8").strip()

    for lang, text, out in [
        ("en", EN_TEXT, "/tmp/cosyvoice-smoke-en.wav"),
        ("hi", HI_TEXT, "/tmp/cosyvoice-smoke-hi.wav"),
    ]:
        print(f"\n==> /synth {lang}: {text[:60]}…")
        t0 = time.time()
        resp = _post(
            url, token,
            {
                "model": "cosyvoice",
                "text": text,
                "ref_audio_b64": ref_b64,
                "ref_text": ref_text,
                "speed": 1.0,
                "output": "inline",
            },
            timeout=600,
        )
        Path(out).write_bytes(base64.b64decode(resp["output_inline"]))
        wall = time.time() - t0
        meta = {k: v for k, v in resp.items() if k != "output_inline"}
        meta["wrote"] = out
        meta["e2e_wall_s"] = round(wall, 2)
        print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
