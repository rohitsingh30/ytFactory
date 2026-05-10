---
name: create-handoff-eval
description: Drop a contract-compliant handoff into the agent-to-agent eval workspace at `/Users/rohit/evals/<project>/inbox/YYYY-MM-DD-<batch-slug>.md` so the `/judge-video` reviewer can rate the freshly-shipped batch. Walks the 8 required sections (title / what + why / where / what's coming / how to evaluate / open issues / decision shape / gut check), pulls every fact from `<channel>/uploads/*.json` + `<channel>/learnings/`, registers each slug in `<project>/STATUS.md` so it's tracked from minute one. After this lands, the user invokes `/judge-video <handoff-path>` — the reviewer's critiques flow back, and `/ingest-critiques` pulls them in. Use when ≥1 mp4 ships to a tracked project (`mystoriesanimated`, `cosmosdecoded`, `historyrecapped`, `hindutavaanimated`, `sportsrecapped`, `rhymetimejunction`, `scrollpulse`) — auto-invoke after any successful batch render+upload of ≥3 videos.
when_to_use: |
  - User says: "create a handoff", "drop a handoff", "send the batch for review", "hand off to the reviewer", "queue X for /judge-video"
  - After ≥3 mp4s ship to a tracked channel within ~24h (auto-invoke per CLAUDE.md trigger)
  - As an explicit step in batch-render → review → cron-publish workflow
when_not_to_use: |
  - Single-video reviews → use `/critique-video` directly (engineer-mode, frame-by-frame)
  - Audio-only reviews → use `/critique-audio`
  - Skill or pipeline-bug debriefs → use `/update-docs` (different audience)
  - Reading critiques BACK from the reviewer → use `/ingest-critiques` (the reverse skill)
  - Live-render babysitting → use `/loop` with a status command
learnings_consulted:
  - /Users/rohit/evals/AGENT_CONTRACT.md   # contract source-of-truth
  - /Users/rohit/evals/SCHEMA.md           # detailed handoff + critique format
  - <channel>/learnings/channel.md         # audience parallel + format defaults
  - <channel>/learnings/<format>.md        # niche-specific format if applicable
  - <channel>/uploads/*.json               # one per shipped video
  - <channel>/scripts/cron_upload_*.py     # cron schedule, if applicable
  - .claude/skills/make-skill/learnings/heuristics.md
helper_module:
  - pipeline/evals.py                      # write_handoff(), ensure_status_row()
---

# /create-handoff-eval — drop a contract-compliant handoff to the reviewer

> **Cross-cutting skill — not a niche producer.** This skill does not
> author channel narrations or modify per-channel state via the
> website state API. The NicheVideo + state-client contract in
> CLAUDE.md applies only if this skill incidentally needs to read
> channel state as context.

This skill writes a markdown handoff into the agent-to-agent eval
workspace described at `/Users/rohit/evals/AGENT_CONTRACT.md`. The
reviewer (`/judge-video`) reads only that one file to decide what to
judge — no source-code reading. So the doc is **load-bearing**: every
section name, every path, must match the contract.

The output lands at:

```
/Users/rohit/evals/<project>/inbox/YYYY-MM-DD-<batch-slug>.md
```

## Required sections (verbatim, in order)

```markdown
# Content eval handoff — YYYY-MM-DD

## What we made + why
Channel name, audience, goal of the batch. Numbered list of the videos
(one line each: number, slug, one-sentence description, voice/tone cue).

## Where the videos are
### Local mp4 files
Code block listing absolute paths to each mp4 in the batch (with
duration + size from ffprobe + stat).
### YouTube — uploaded private + scheduled to auto-publish
(Optional) Schedule table if the videos are queued for upload.

## What's coming
(Optional) Context on the larger queue / cron / bulk upload. Tells the
reviewer whether this verdict gates a downstream decision.

## How to evaluate
Channel-specific rubric add-ons that the reviewer applies on top of the
standard 50-param rubric (defined in `/Users/rohit/evals/SCHEMA.md`).
Channel hook + closer + visual + thumb-stop convention by name.

## Open quality issues you might still spot
Bugs the authoring agent already knows about. Helps the reviewer
distinguish "new defect" from "tracked".

## Decision the reviewer can give
Choices wanted back (Ship / Block / Mixed) + the launchctl/crontab
pause snippet so the reviewer has an escape hatch.

## Single-question gut check
The one yes/no question the authoring agent most cares about.
```

These are the **8** contract sections — section names are exact
(en-dash `—` in the title, `#` heading levels as shown). The reviewer
parses by section name.

## How to run it

### 1. Determine the batch + project

Ask via `AskUserQuestion` only if ambiguous:
- **Project** — channel slug. Tracked: `mystoriesanimated`,
  `cosmosdecoded`, `historyrecapped`, `hindutavaanimated`,
  `sportsrecapped`, `rhymetimejunction`, `scrollpulse`.
  Default: the channel with the most uploads in the last 24h.
- **Batch slug** — short kebab-case label (`tifu-batch`,
  `cosmos-anchors-v2`, `aita-cooking`).
- **Cutoff** — "last N hours" (default 24h) or explicit slug list.
- **Reviewer name** *(optional)* — personalises the title.

### 2. Read the lay of the land (read-only)

- `<channel>/learnings/channel.md` — audience parallel + format
  defaults (duration, aspect, voice, closer convention). NEVER
  fabricate the audience; if `channel.md` doesn't have one, ask.
- `<channel>/learnings/<format>.md` if a niche format applies
  (e.g. `mystoriesanimated/learnings/aita_cliffhanger.md`).
- `<channel>/uploads/*.json` filtered by mtime within cutoff. Each
  record: `video_id`, `title`, `privacy`, mp4 path, `publish_at`.
- `<channel>/shorts/*.mp4` (or `long_form/*.mp4`) for local paths
  block. Duration via `ffprobe`, size via `stat`.
- `<channel>/scripts/cron_upload_*.py` (or LaunchAgent plist) for
  cron cadence — print `SCHEDULE_HOURS_IST` or the plist
  `StartCalendarInterval`.
- The conversation that just shipped this batch — for "Open quality
  issues". Don't invent issues; only mention what was visibly
  discussed.

### 3. Build the publish table

For each upload record in scope, extract:
- `video_id` → `https://youtu.be/<id>`
- `publish_at` (ISO UTC) → format as both UTC and IST columns
- A short headline derived from `title` (≤40 chars)

If `publish_at` is null (immediate-public), say so in the table.

### 4. Build the queue summary

`<channel>/shorts/*.mp4` count − uploaded count = pending queue.
Group by niche/variant if the channel has them. ETA = `(pending /
slots_per_day)` days.

### 5. Tailor "How to evaluate" to channel format

Pull from `channel.md` + format file. Examples:

- **MyStoriesAnimated AITA** — hook = "Am I wrong for…"; closer =
  "LIKE if YTA / COMMENT if NTA"; visual = hands + character lock.
- **Cosmos Decoded** — hook = "moment of measurement visible in 1.5s";
  pacing = "2.66s/cut sweet spot"; closer = "SUBSCRIBE for more
  decoders, LIKE if this changed how you see physics".
- **HistoryRecapped Shorts** — hook = "place + date + stakes"; closer =
  "LIKE to honor those who served. SUBSCRIBE for more such stories";
  visual = 100% archival.
- **HindutavaAnimated** — hook = "12-beat name reveal"; visuals =
  Amar Chitra Katha; closer = blessing + CTA.
- **HistoryRecapped sleep** — hook = "novelistic 6-beat 90s opener";
  pacing = "viewer falls asleep ~3-5 min in"; closer = 4-stack soft
  asks + sleep-ritual return-to-frame.
- **ScrollPulse** — hook = "subreddit pill + post title in 1.5s";
  closer = LIKE+SUBSCRIBE icons over gameplay.

If the channel has NO matching template, fall back to generic
Shorts/long-form criteria but flag in the doc that the reviewer should
add channel-specific items.

### 6. Mine the open issues

Scan the conversation that produced this batch for:
- Frames flagged as visually off (hands, character drift, captions,
  asset topicality, etc.)
- Pacing / WPM complaints
- Closer / CTA known-broken state
- Length-cap violations (Shorts >60s posting as regular videos)
- Pronunciation issues caught by `/critique-audio`

Each issue gets ONE line + a one-line "by design" or "fix in next
batch" note. Don't speculate about issues that weren't discussed.

### 7. Emit the doc + register slugs

Build the markdown body (8 sections), then commit via:

```python
from pipeline.evals import write_handoff
path = write_handoff(
    project="cosmosdecoded",
    batch_slug="cosmos-anchors-v2",
    content_md=body,
    track_slugs=["pound-rebka-1959-short", "hubble-1929-short", ...],
)
```

`write_handoff()`:
- Writes to `/Users/rohit/evals/<project>/inbox/YYYY-MM-DD-<batch-slug>.md`.
- Auto-suffixes `-v2`, `-v3` if a same-day same-slug handoff exists
  (NEVER overwrites — the contract requires history is preserved).
- Calls `ensure_status_row(project, slug)` for each track_slug so the
  STATUS.md ledger has a row before the reviewer ever scores it.

Print the resulting path to the user with:

> "I've handed off batch `<file>` to the reviewer. Run
> `/judge-video <path>` to get critiques."

### 8. Optional: notify external reviewer

If `<channel>/config.yaml` has `handoff_recipients` (future feature),
NOTE the addresses but DO NOT auto-send. External comms is high
blast-radius — the user approves each send.

## Important rules

- **Path is non-negotiable.** Output MUST land at
  `/Users/rohit/evals/<project>/inbox/YYYY-MM-DD-<batch-slug>.md`.
  The old `docs/eval_handoff_*` location is RETIRED — that path
  violates the agent-to-agent contract.
- **No fabrication.** Every line backed by a file: upload record,
  `channel.md`, ffprobe output, cron config, or a conversation moment.
  If a claim has no source, omit it.
- **Single-page-readable.** Reviewer scans this in 5 min on phone.
  Cut anything > one screen per section. Target 130-180 lines / 800-
  1500 words.
- **The reviewer is non-technical.** No `pipeline/foo.py:42` line
  refs. No "the circuit breaker tripped". Translate to viewer
  language: "the closer panel" not "the LIKE+SUBSCRIBE end-screen
  overlay". Engineer-side details belong in `/update-docs`.
- **Channel-specific tailoring is mandatory.** A handoff that uses
  generic "evaluate the hook" without the channel's specific hook
  convention is low-value. Pull from `<channel>/learnings/`.
- **Always include the launchctl/crontab pause snippet.** The
  reviewer needs an escape hatch. Without it the doc is one-way fire.
- **Always end with the gut-check question.** Single yes/no/maybe
  decision the reviewer can answer in 2 seconds.
- **Never overwrite an existing handoff.** `write_handoff()` enforces
  this — it auto-suffixes `-v2`, `-v3`. The first reviewer's evidence
  stays.
- **Never auto-send.** Even if `handoff_recipients` exists, the user
  approves each send.

## Quality gates (run before reporting done)

1. **Output path matches contract** — must start with
   `/Users/rohit/evals/<project>/inbox/` and match
   `YYYY-MM-DD-<batch-slug>.md` (or `-vN.md`).
2. **Title format match** — `# Content eval handoff — YYYY-MM-DD`
   (en-dash `—`, not hyphen `-`).
3. **All 8 contract sections present** — verbatim names, correct
   order. Block on missing.
4. **Upload table populated** (if scheduled-public) — at least one
   row per uploaded mp4 in scope.
5. **Local mp4 paths real** — every path in the local-files block
   exists on disk. Block on missing files.
6. **Channel-specific evaluation criteria present** — "How to
   evaluate" MUST reference the channel's hook/closer convention by
   name. Generic boilerplate is rejected.
7. **No engineer jargon** — flag if any line contains `pipeline/`,
   `circuit breaker`, `Cloud Run`, `ffmpeg`, `Cloudflare`, etc.
   Translate to viewer language.
8. **Pause snippet present** — section 7 must include a verbatim
   `launchctl unload` or `crontab -r` snippet.
9. **Word count 800-1500** — under 800 = too thin, over 1500 = scroll
   fatigue.
10. **STATUS.md rows added** — `read_status(project)` after the run
    must include a row for every slug in the batch.

## Self-learning loop

Every time `/create-handoff-eval` runs and the user later corrects the
output ("the cron schedule was wrong", "you missed the dentist short",
"format the publish table differently"):

1. Classify per `docs/post_upload_analysis.md` taxonomy.
2. Append a one-liner to `learnings/_index.md`.
3. If CLASS-OF-BUG: update SKILL.md "Important rules" or "Quality
   gates" with the new constraint.
4. If channel-format gap: update the channel-tailoring list in step 5.

## Engineering-efficiency check

This skill is **read-only on the deliverable** — it produces ONE
markdown file from existing artefacts. It deliberately avoids:

- Re-rendering or re-uploading anything (use `/make-*`).
- Engineer-mode debrief (use `/update-docs`).
- Frame-by-frame critique (use `/critique-video`).
- Reading critiques back (use `/ingest-critiques`).

If the user asks for any of those, redirect rather than expand scope.

## See also

- `/Users/rohit/evals/AGENT_CONTRACT.md` — contract source-of-truth.
- `/Users/rohit/evals/SCHEMA.md` — detailed handoff + critique format.
- `pipeline/evals.py` — helper module (`write_handoff`,
  `ensure_status_row`, …).
- `.claude/skills/ingest-critiques/SKILL.md` — the reverse skill that
  reads critiques back into STATUS.md + holds.
- `.claude/skills/critique-video/SKILL.md` — engineer-mode frame
  critique (different audience).
- `.claude/skills/update-docs/SKILL.md` — engineer-side debrief.

## Memory pointer

`skill_create_handoff_eval.md`.
