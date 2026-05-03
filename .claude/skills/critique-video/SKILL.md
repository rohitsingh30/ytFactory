---
name: critique-video
description: Watch a finished ytFactory Short as if you were a YouTube Shorts viewer scrolling past it AND as a pipeline engineer, and give honest moment-by-moment feedback through every relevant lens (hook, sync, continuity, pacing, captions, glitches, composition, palette, mute-mode, thumbnail, arc, CTA, source-fidelity, re-watch, comments-bait). Use when the user wants a viewer's reaction to a generated video — e.g. "critique my video", "watch the AITA short", "would you scroll past this", "tell me what's wrong with aita02". Samples the mp4 densely with ffmpeg, reads each frame, reacts as a viewer, then surfaces concrete per-frame fixes plus class-of-bug system corrections.
---

# /critique-video — watch a Short through every lens, then engineer

Two jobs at once:

1. **Be the viewer.** Pretend you opened YouTube Shorts, this video
   started auto-playing, and you have to decide in 1.5 seconds whether
   to keep watching or flick up. Then if you stayed, react beat-by-beat
   the way a real viewer would.

2. **Be the pipeline engineer.** For every issue you spot, classify it
   ONE-OFF (fix the prompt for this beat) vs CLASS-OF-BUG (fix the
   pipeline so the next 100 Shorts don't repeat it). The critic that
   runs in-pipeline (``pipeline/critic.py``) does the same thing — this
   skill is the human-readable mirror.

Not a checklist score. A reaction PLUS engineering.

## How to run it

### 1. Pick the video

- If the user passed a path or slug, use it.
- Otherwise pick the most recently modified ``data/shorts/*.mp4``.
  State which one in one line before continuing.

The user is on the website (``http://127.0.0.1:8765``) for generation
now, but the website writes to the same ``data/shorts/<slug>.mp4`` and
``data/cache/<slug>/`` paths the CLI uses, so this skill needs no
website-specific path logic. If the user pastes a job URL like
``/api/jobs/<id>/short`` or ``/api/jobs/<id>/short/<i>``, look up the
slug from ``JOBS[<id>]`` (logged in uvicorn stdout) or just resolve to
the most recent mp4 in ``data/shorts/``.

From the slug, locate (best-effort — keep going if any are missing):

- ``data/shorts/<slug>.mp4`` — the video
- ``data/cache/<slug>/`` — narration.wav, beats.json (word-level
  timestamps), prompts.json, ``img_NN.png``, ``caption_NN.png``,
  ``closer_panel.png``
- ``data/intermediate/*/scripts/<slug>.json`` — find via
  ``find data/intermediate -name "<slug>.json"``
- ``data/intermediate/*/shotlist/<slug>.json`` — only if /make-movie-short
  produced it. Read this if it exists; it's the directorial intent
  you're checking against.
- ``data/intermediate/*/cast/<slug>.json`` — per-story characters.

### 2. Get the spoken-words timeline

You can't listen to ``narration.wav`` directly, but ``beats.json`` has
word-level timestamps for the entire narration. Read it. **That's your
audio track** — every word the viewer hears, with the exact second it
arrives.

### 3. Sample the mp4 densely so you can actually see it

Get duration first:

```bash
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 data/shorts/<slug>.mp4
```

Then extract one frame per second from t=0 to the end, **plus** an
extra frame at t=0.3 and t=0.8 (the hook is decided in the first
~1.5s, so over-sample there):

```bash
mkdir -p data/critiques/<slug>/frames

ffmpeg -y -i data/shorts/<slug>.mp4 -vf fps=1 -q:v 2 data/critiques/<slug>/frames/t_%02d.png
ffmpeg -y -ss 0.3 -i data/shorts/<slug>.mp4 -frames:v 1 -q:v 2 data/critiques/<slug>/frames/t_hook_03.png
ffmpeg -y -ss 0.8 -i data/shorts/<slug>.mp4 -frames:v 1 -q:v 2 data/critiques/<slug>/frames/t_hook_08.png
```

A 16s short → ~17 frames + 2 hook frames. ``Read`` every PNG in order.

### 4. Watch it through every lens

For each frame, run it past every lens below. Don't write a paragraph
for every lens on every frame — most lenses will be silent, which is
fine. **Speak up only when a lens flags something.** The point of
having all the lenses listed is that you remember to *look*.

| # | lens | the question |
|---|---|---|
| L1 | **Hook (0.0–1.5s)** | Am I scrolling past this in 1.5s? Why exactly — words, image, energy, framing? Is the punchline of the first frame readable in 0.3s? |
| L2 | **Audio-visual sync** | When the image swaps, does it match the words being spoken *right then* — or am I seeing a steak while hearing about a wine bottle? Note the lag in seconds when it happens. |
| L3 | **Character continuity** | Is the character on screen the same person frame-to-frame? Same hair, age, outfit, body shape? Or do they morph? (Locked channel character + per-story cast — both should hold.) |
| L4 | **Pacing / retention** | Where am I bored? Be specific: "at 7s I checked out because the image hadn't changed for 3 seconds." Where do I lean in? |
| L5 | **Caption legibility** | Can I read the captions on a phone — contrast, size, position, line wrap? Are they covered by anything? Do the captions match the spoken word? |
| L6 | **AI-glitch** | Wrong fingers, melting faces, ghost-doubled bodies, gibberish text, wrong number of people, bad anatomy, mismatched scene? Viewers notice instantly. |
| L7 | **Composition** | Rule of thirds, headroom, leadroom, negative space. Is the subject sitting in dead center for 16 seconds? Is anything cropped at a joint? |
| L8 | **Palette / channel aesthetic** | Does the channel's locked look hold (crayon, pastel, beige bg, etc.)? Any frame that drifts into a different style? |
| L9 | **Mute mode** | 80% of Shorts viewers mute by default. Does the video TELL THE STORY without sound — captions + visuals alone? Where does mute mode break? |
| L10 | **Thumbnail (frame 0)** | If frame 0 was the static thumbnail, would it stop a scrolling thumb? Is there a clear punchline visual? Or is it generic? |
| L11 | **Story arc / escalation** | Does the narrative escalate? Is there a beat where I think "oh no" or "wait what"? Or does the energy stay flat? |
| L12 | **Closer / CTA** | Does the closer panel render legibly and hold long enough to read? Does the spoken closer match the visual panel? Does it actually drive action (LIKE/COMMENT split present)? |
| L13 | **Source fidelity** | Versus the raw Reddit story (``data/intermediate/*/raw/<slug>.json``), did the narration miss the punchline? Sand off a juicy detail? Soften a contentious word? |
| L14 | **Re-watchability** | Would I tap replay? Is there a detail in the hook that I'd notice on a second pass? |
| L15 | **Comments-bait** | Will the closer drive specific YTA-vs-NTA opinions, or vague "what do you think" mush? Is there a deliberate ambiguity that makes commenting irresistible? |
| L16 | **Footage cut-in timing** (sports / hybrid) | Is the broadcast cut held PAST its natural emotional climax — does the clip drag through 2-3s of post-call filler before cutting back to cartoon? Conversely, is it cut SHORT before the call peak / net-bulge / catch lands? Sample frames every 0.5s INSIDE the footage window, identify where the natural-edit "cut here" moment is (peak crowd reaction OR slow-mo replay starts OR camera changes angle), and call it out if our trim runs past that. Footage that overstays drops the energy fast — viewers feel it as "why am I still watching this." |
| L17 | **Footage in/out continuity** (sports / hybrid) | Does the cut TO footage land cleanly (does the cartoon-→-broadcast transition feel earned by the previous beat) and the cut OUT land cleanly (does the broadcast-→-cartoon transition resume narration without dead air or visual whiplash)? Specifically: at the IN moment, was the last cartoon frame a payoff that EARNED a swap to "real life" — or a random scene shot? At the OUT moment, does the next cartoon match the emotional state the broadcast left us in (celebration → cartoon celebration, not celebration → empty trophy room)? |

### 5. Build the per-frame fix table

This is the engineering output. For every frame where any lens flagged
something, produce a row:

| beat | timestamp | lens(es) | what's wrong (concrete) | classification | the fix |
|---|---|---|---|---|---|
| 0 | 0.0–2.5s | L1, L10 | Generic shocked-face hook on plain bg — no concrete prop, no specific room. Thumb keeps scrolling. | **CLASS-OF-BUG** | ``pipeline/prompts.py`` already has opening_image_directives but the LLM ignored them on this run. Add a hard validator in ``script_check.py`` that fails if beat-0 prompt has <2 channel example_tokens. |
| 4 | 7.0–9.6s | L2, L3 | Image still shows the salad while voice says "three bottles of wine"; also character's hair switched from brown to dark. | **ONE-OFF** for the lag (rewrite beat 4 prompt to put wine bottles in foreground); **CLASS-OF-BUG** for hair drift (channel character lock isn't holding — investigate prompts.py prepend ordering). |

Concrete > abstract. "It's confusing" is useless; "at 7s I'm hearing
'three bottles of wine' but the image is still showing the salad from
beat 3" is useful.

For each row, the fix column should be **actionable** — a one-line
concrete change to the prompt, the channel YAML, or the pipeline file.
Not a lecture.

### 6. Write up the reaction

Write to ``data/critiques/<slug>.md`` and also print it inline. Format:

```markdown
# Watching <slug>.mp4

**One-line gut take**: <would I keep watching? would I share it? would I comment?>
**Score (1–10, 10 = I'd share)**: <n>

## Second-by-second reaction (the viewer)
- **0.0–1.5s (the hook moment)** — <what I see, what I hear, do I stay, which lens flagged>
- **1.5–4s** — <…>
- **4–8s** — <…>
- **8s–end** — <…>
- **Closer** — <does it land>

## Per-frame fix table (the engineer)

| beat | timestamp | lens | what's wrong | class | the fix |
|---|---|---|---|---|---|
| ... |

## Class-of-bug fixes for the next 100 Shorts
For each CLASS-OF-BUG row in the table above, describe the system
correction with a file:function target. Reference DESIGN.md §14
principles where one already exists; flag NEW principles otherwise.

- **<issue_class>** — `<file:function or schema field>` — <fix>
  (principle: <#N or NEW>)

## What pulled me in
- <specific frame + why>

## What pulled me out
- <specific timestamp + which lens caught it>

## If I were the creator, the single highest-leverage change is:
<one thing — prefer a CLASS-OF-BUG fix over a one-off if both apply, since
it lifts the next 100 Shorts not just this one>
```

Keep it honest. If the Short is bad, say it's bad and where. If it's
genuinely good, say that too — don't manufacture problems. The user
wants to know whether a stranger scrolling Shorts would watch this,
not whether it satisfied a checklist.

## Important rules

- **Read-only.** Never re-run TTS, image gen, or compose. Only write
  to ``data/critiques/<slug>/`` and the markdown file.
- The audio you "hear" comes from ``beats.json`` word timestamps —
  that's the actual narration timeline of the rendered mp4 (not the
  script's idealized text), so always prefer it over
  ``scripts/<slug>.json`` when they disagree.
- Sample frames at **1 fps minimum** plus the 0.3s + 0.8s hook samples.
  Don't try to critique a video from 3 thumbnails — you'll miss what a
  viewer sees between them.
- React in the present tense, as a viewer ("I see…", "now the voice
  says…"). Don't slip into pipeline-engineer voice in the reaction
  section — save that for the fix table.
- **Cite the timestamp on every complaint.** "It's confusing" is
  useless; "at 7s the image is still showing the salad while the voice
  says 'three bottles of wine'" is useful.
- **Run every lens on every frame, but only write up the lenses that
  flagged.** The point of L1–L17 is to catch what you'd miss without
  the checklist — not to fill 15 paragraphs of "lens L7 says: nothing
  to flag." Silence is fine.
- **Every issue gets a classification.** ONE-OFF (per-beat prompt
  edit) or CLASS-OF-BUG (pipeline schema/code change). If you can't
  decide, default to CLASS-OF-BUG — being wrong towards system fixes
  is cheaper than being wrong towards whack-a-mole patches. Per
  stored memory feedback.
- **Always propose a system correction for every class-of-bug.**
  File:function targets. Don't just complain about this video —
  engineer the next 100 to not repeat it.
- One Short per run. Don't batch.
