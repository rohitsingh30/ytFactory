#!/usr/bin/env python
import json, sys
from yt_dlp import YoutubeDL
BASE_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True}
def search(query, n=10):
    opts = dict(BASE_OPTS); opts["extract_flat"] = True
    with YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in (res or {}).get("entries", []) or []:
        if not e: continue
        out.append({"id": e.get("id"), "title": e.get("title"), "channel": e.get("channel") or e.get("uploader"), "duration": e.get("duration"), "view_count": e.get("view_count")})
    return out
def deep(video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL(BASE_OPTS) as ydl:
        m = ydl.extract_info(url, download=False)
    if not m: return None
    best_w = m.get("width") or 0; best_h = m.get("height") or 0
    if (not best_w or not best_h) and m.get("formats"):
        for f in m["formats"]:
            w = f.get("width") or 0; h = f.get("height") or 0
            if w and h and (w*h) > (best_w*best_h): best_w, best_h = w, h
    return {"id": m.get("id"), "title": m.get("title"), "channel": m.get("channel") or m.get("uploader"), "duration": m.get("duration"), "view_count": m.get("view_count"), "width": best_w, "height": best_h, "vertical_native": (best_h > best_w) if (best_w and best_h) else None, "chapters": m.get("chapters") or [], "description": (m.get("description") or "")[:1500], "tags": (m.get("tags") or [])[:30]}
if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "search":
        q = sys.argv[2]; n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        print(json.dumps(search(q, n), indent=2, default=str))
    elif cmd == "deep":
        ids = sys.argv[2].split(",")
        out = {}
        for vid in ids:
            try: out[vid] = deep(vid)
            except Exception as e: out[vid] = {"error": str(e)}
        print(json.dumps(out, indent=2, default=str))
