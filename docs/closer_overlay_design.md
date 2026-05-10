# Closer overlay design — keyword gate + skip span + dual compose paths

**Status:** ACTIVE rule for every channel that ships LIKE/SUBSCRIBE
closer Shorts.
**First surfaced:** 2026-05-08 (pompeii-79 v4 → v7 iteration).
**Owner:** `pipeline/render/shorts.py` + `pipeline/compose.py`.

---

## What "the closer" actually is

A typical 50-60s Short ends with a 3-4s spoken CTA ask, e.g.:

```
LIKE if you learned something. SUBSCRIBE for more such stories.
```

The renderer puts TWO things on screen during this window:

1. **LIKE+SUBSCRIBE icons baked INTO the last beat's image** — a
   yellow thumbs-up + red bell PNG appended to the diffusion prompt
   at `_append_last_beat_icons` (`pipeline/render/shorts.py:93`).
2. **Per-word captions stay OFF during the closer** — the visual
   block speaks alone. Filtered by closer-skip span detection in
   `pipeline/compose.py`.

Three sibling bugs surfaced in pompeii-79 v4-v7. All three now
have fixes; future renders should encounter none of them.

---

## Bug 1: keyword false-match (icons firing mid-Short)

**Symptom:** the LIKE+SUBSCRIBE icons appear over a frame in the
MIDDLE of the Short (e.g. at t=23s of a 50s Short, on the snow-on-
roofs frame).

**Cause:** `_CLOSER_KEYWORDS` was lowercase substring-matched on
`beat_text.lower()`. The narration "by evening it falls **like** snow
on the roofs" matched the `"like"` keyword → icons injected on that
beat's image. Same risk: "the **comment** was anonymous", "we
**agree** to disagree", "**swap** the meeting".

**Fix:** `pipeline/render/shorts.py:90`:
- `_CLOSER_KEYWORDS` keys are UPPERCASE: `("LIKE", "COMMENT",
  "SUBSCRIBE", "AGREE", "SWAP")`.
- Match is case-SENSITIVE on the original `beat_text`.
- Positional gate added: only fire on beats in the last 25% of the
  narration (`beat_idx >= int(n_beats * 0.75)`).

Any closer keyword in earlier beats is ignored by both gates.

---

## Bug 2: single-beat skip detection too narrow

**Symptom:** per-word captions ("LIKE", "if", "you", "learned",
"something", "SUBSCRIBE", "for", "more", ...) render through the
closer window, fighting the visual ask block for attention.

**Cause:** the closer-skip filter required BOTH "like" AND "subscribe"
in the SAME beat. The whisper splitter often puts the 2-sentence
closer in two beats — beat N has "like..." but not "subscribe...";
beat N+1 has "subscribe..." but not "like...". Detection missed both.

**Fix:** span-based detection in `pipeline/compose.py`:
- Find the FIRST beat in the last 30% whose first word (lowercased,
  punctuation-stripped) is in `{"like", "subscribe", "comment"}`.
- That beat marks `closer_start`.
- ALL beats from `closer_start` to `len(beats)` are part of the span.
- Per-word captions skip for the entire span.

---

## Bug 3: dual compose paths

**Symptom:** the v3-v6 closer-skip patch in `compose.compose()`
worked — but v7 still showed per-word captions during the closer.

**Cause:** `pipeline/compose.py` has TWO sibling functions with their
own per-word loops:

| Function | Used by |
|---|---|
| `compose()` | slideshow renderer (footage-only path) |
| `compose_clips()` | per-clip renderer (`make_shorts.py` Shorts path) |

The shorts pipeline calls `compose_clips()`. The fix in `compose()`
never ran. Patch had to be MIRRORED into both.

**Fix:** identical span-detection block in both functions
(`pipeline/compose.py` lines ~366-394 and ~1212-1245).

---

## Defensive habit for future patches

When patching ANY pipeline file with multiple entry points:

1. **Grep for sibling functions before declaring fix complete.** If
   `compose()` has the bug, check `compose_clips()`. If
   `_append_last_beat_icons` is called once, check critic-regen
   path too.
2. **The four `pipeline/render/*.py` entry points are sibling.**
   Pre-warm hooks, era_lock injection, voice-fallback logic — apply
   to all four (`shorts.py` + `footage_only.py` + `long_form.py` +
   `sports_doc.py`) or document why not.
3. **Compose has TWO entry points.** Same logic.

See: [`memory/feedback_render_path_dual_mirror.md`](../../.claude/projects/-Users-rohit-ytFactory/memory/feedback_render_path_dual_mirror.md).

---

## Related

- `pipeline/render/shorts.py:90` — `_CLOSER_KEYWORDS`
- `pipeline/render/shorts.py:_append_last_beat_icons`
- `pipeline/compose.py:_is_closer_beat` (deprecated single-beat) +
  span detection in `compose()` + `compose_clips()`
- Memory: `feedback_closer_overlay_design.md`
