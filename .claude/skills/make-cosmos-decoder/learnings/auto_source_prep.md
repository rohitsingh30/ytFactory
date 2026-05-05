# Section-7 auto-prep — never leave source fetching to the curator

**Class-of-bug fix, 2026-05-05.** Skill mirror of `cosmosdecoded/learnings/auto_source_prep.md` (per CLAUDE.md dual-save rule).

After Section 6 quality gates pass, the skill MUST Bash-execute the prep tool for both the long-form and the Short shotlist:

```bash
.venv/bin/python -u -m pipeline.cosmos_footage_prep --channel cosmosdecoded --slug <long-slug>
.venv/bin/python -u -m pipeline.cosmos_footage_prep --channel cosmosdecoded --slug <short-slug>
```

The tool resolves Wikimedia / NASA / archive.org URLs automatically, downloads stills, and runs ffmpeg zoompan to produce the right-aspect Ken-Burns mp4s. Manual fallback is only triggered for paywalled hosts (royalsocietypublishing, nature.com, NYT TimesMachine, Pexels) — typically 2–4 entries per video.

**Why this matters:** First eddington-1919-eclipse run shipped four JSONs and stopped. The renderer immediately hard-failed at `FileNotFoundError`. User feedback: "source should run in the skill by default". The skill — not the curator — owns the prep. Run it before reporting back.

**Update propagation:** When you finish a /make-cosmos-decoder run, append a regression note here if the prep tool's resolver list missed any new host. The resolver dispatch lives at `_resolve_url()` in `pipeline/cosmos_footage_prep.py`.

**Engineering follow-ups (heuristic #46):**
- The prep tool is misnamed — it's channel-agnostic. Promote `pipeline/cosmos_footage_prep.py` → `pipeline/footage_prep.py` next time another footage-only skill (top10, katha, sleep-history) needs the same dispatch.
- Consider a `--dry-run` flag that just prints what would be fetched + what would fall back to manual, so the user can pre-validate the shotlist URLs before the slow fetch loop.
