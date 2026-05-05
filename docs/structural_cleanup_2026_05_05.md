# Structural cleanup — 2026-05-05

User flagged the project layout as "bloated and not properly structured"
after the Tier 1 efficiency overhaul. Audit found that **most of the
bloat was disk weight in `.git`, not code structure** — Phase 3 (commits
682e52b, 2e3aba6) had already established the canonical layout via
`pipeline/paths.py` + [`docs/channel_layout.md`](channel_layout.md).
Remaining issues were tackled in four sub-tiers (S1-S4); a fifth (S5)
was deliberately deferred.

## Outcome

| Metric | Before | After | Reduction |
|---|---|---|---|
| `.git/` size | **11 GB** | **48 MB** | **99.6%** |
| `pipeline/` top-level entries | 47 | 28 | 40% |
| Top-level dirs at repo root | 22 (with cruft) | 17 | 23% |
| Channel-specific scripts in canonical location | 0/2 channels | 2/2 channels | full |
| Tests passing | 349 | 349 | unchanged |

**Total time:** ~1.5 hours (S1-S4 implementation + tests + commits).
**Branch history:** unchanged (no force-push needed — `git gc`
preserved all reachable commits).

## Tier S1 — Cruft removal

Deleted (none were tracked, so no commit needed):

* `.venv-recover/` (253 MB stale recovery venv from May-4)
* `config/{cast,narrations,shorts}/` (3 empty subdirs accidentally
  created during Phase 3 layout work)
* Empty channel subdirs: `cosmosdecoded/{music,long_form}`,
  `sportstoriesanimated/music`, `airecap/{branding,uploads}`,
  `historyrecapped/cache/_kokoro_samples`, `data/_bench/tts/<old-stamp>`
* Stale cache subdirs from killed renders

All caches under `<channel>/cache/<slug>/` are recreated on demand by
the renderers (`mkdir(parents=True, exist_ok=True)`), so deleting empty
ones is safe.

## Tier S2 — Layout convergence (commit `84b44f3`)

Per [`docs/channel_layout.md`](channel_layout.md), channel-specific
scripts live in `<channel>/scripts/`. Two channels still had their
tooling at the top-level `scripts/<channel>/`:

* `scripts/historyrecapped/*.py` (6 files) → `historyrecapped/scripts/`
* `scripts/sportstoriesanimated/*.py` (5 files) →
  `sportstoriesanimated/scripts/`

All moved via `git mv` (history preserved). 45 docstring / comment /
config / learning / skill / test references updated. CLI shims use
`Path(__file__).resolve().parent.parent.parent` which still resolves
to repo root from the new location, so all entry points (cron,
`/make-katha`, `/make-sleep-history`, `/make-cosmos-decoder`,
`/make-sports-doc`) work unchanged.

The top-level `scripts/` dir is now reserved for cross-channel
infrastructure tooling only (`laptop_cleanup.py`, `bulk_upload.py`,
`make_shorts.py`, `pull_stories.py`, etc.).

## Tier S3 — `pipeline/` sub-packaging (commit `d8bb5d7`)

Phase 3 had already split out `pipeline/{render,sources,tts}/`. This
tier continued the pattern by grouping the remaining flat modules:

* **`pipeline/llm/`** — 14 modules (LLM-driven authoring + critique):
  `cli.py` (was `llm.py`), `cast.py`, `cast_router.py`, `critic.py`,
  `audio_critic.py`, `quality_gate.py`, `script_check.py`,
  `script_lint.py`, `visualizability.py`, `imitate.py`,
  `airecap_rewrite.py`, `rewrite.py`, `prompts.py`, `segment.py`.
* **`pipeline/research/`** — 4 modules (research dashboard +
  cross-engagement): `aggregator.py` (was `research.py`), `wiki.py`
  (was `wiki_research.py`), `youtube.py` (was `youtube_stats.py`),
  `cross_engage.py`.

Backward compatibility preserved via:

1. `__init__.py` re-exports of public symbols — existing
   `from pipeline.llm import call_claude_cli` and
   `from pipeline.research import build_videos` keep working.
2. Alias imports inside moved files —
   `from . import cli as llm` keeps `llm.X` call sites unchanged.
3. `..` parent-package imports for refs to non-moved siblings.

**The non-obvious trap fixed:** test patchers that mutate
`pipeline.research.PROJECT_ROOT` were silently broken by the package
façade pattern (`__init__.py` re-exports create new bindings —
mutating them doesn't affect what the FUNCTIONS see). Test patchers
were rewritten to target `pipeline.research.aggregator.PROJECT_ROOT`
directly. See
[`feedback_pipeline_subpackages_split.md`](../../.claude/projects/-Users-rohit-ytFactory/memory/feedback_pipeline_subpackages_split.md)
for the full pattern + rule.

## Tier S4 — Git history cleanup

`.git` was 11 GB. Top-10 largest blobs: 12 `assets/cooking_loops/*.mp4`
(38 MB total committed). All other reachable objects: ~20 MB. **Total
reachable: ~58 MB.**

The 11 GB lived in:

1. **Two stray loose blobs** (333 MB + 103 MB) — orphans from May-4
   recovery work; never reachable from any ref.
2. **The cooking_loops mp4s** — `git add`-ed once but never committed;
   unreachable orphan blobs.
3. **The May-4 recovery pack** (`pack-2f67f08...11G`) — packed all
   the above into a single monolithic file.

**Solution:** `git gc --aggressive --prune=now` (after
`git reflog expire --expire=now --expire-unreachable=now --all`)
swept all unreachable objects + repacked tightly. **No `git filter-repo`
needed, no force-push, no history rewrite.**

```bash
# Backup (always — destructive to unreachable history)
tar -cf ~/.git-ytFactory-backup-2026-05-05.tar .git/    # 11 GB

# Expire reflog so unreachable-via-reflog objects become eligible
git reflog expire --expire=now --expire-unreachable=now --all

# Aggressive gc + prune everything unreachable
git gc --aggressive --prune=now --quiet

# Verify
git rev-list --all --objects | wc -l   # 1449 → 1449 (unchanged)
git fsck --no-reflogs                  # clean
```

Result: **48 MB** (.git). All 1449 reachable objects intact. Test suite
green. Branch history unchanged — no force-push, no teammate re-clone.

See [`feedback_git_gc_reclaim_orphan_blobs.md`](../../.claude/projects/-Users-rohit-ytFactory/memory/feedback_git_gc_reclaim_orphan_blobs.md)
for the full pattern + when to escalate to `filter-repo`.

## Tier S5 — Server consolidation (deferred)

Considered: collapse `control/` (300 K) + `workers/` (148 K) +
`web/` (7 M) + `cloud/` (40 K) into a single `server/{api,workers,
web,cloud}/` hierarchy.

**Decided not to ship.** Each top-level dir has a clear role today:

* `control/` — FastAPI routes for the operator dashboard (auth,
  jobs, queue, scheduler, chat, render, niche, agent endpoints)
* `workers/` — background job runners split by weight
  (heavy/render, light/research+upload, agent/runner)
* `web/` — static dashboard frontend + the FastAPI app entrypoint
* `cloud/` — cloud-deployable services (`cloud/tts/` for the
  remote F5-TTS-MLX worker)

Consolidating would require rewriting ~50-100 import references and
updating the Dockerfile + GitHub Actions YAMLs for marginal benefit.
The current split has clean role boundaries; the dirs are not
bloated individually. Skip unless the ops surface visibly fragments
further.

## Files touched (S1-S4)

* `pipeline/preflight.py` (Tier 1, kept)
* `pipeline/llm/` (NEW package — 15 files)
* `pipeline/research/` (NEW package — 5 files)
* `historyrecapped/scripts/*.py` (NEW location — 6 files moved from `scripts/`)
* `sportstoriesanimated/scripts/*.py` (NEW location — 5 files moved)
* `pipeline/render/*.py` — docstring updates + import fixes
* `pipeline/{tts,audio}/__init__.py` — caller-example doc updates
* `pipeline/upload.py` — moved-module imports
* `web/server.py` — 5 lazy-import sites updated
* `workers/{heavy,light}/*.py` — moved-module imports
* `scripts/pull_stories.py` — moved-module imports
* `tests/test_pipeline_*.py` (10 files) — import paths +
  patcher-pattern fixes
* `tests/test_web_research_routes.py` — patcher pattern fix
* `historyrecapped/learnings/` (8 files) — usage docs updated
* `cosmosdecoded/{config.yaml,learnings/channel.md}` — doc updates
* `hindutavaanimated/{config.yaml,learnings/}` (3 files) — doc updates
* `historyrecapped/critiques/top10-alien-abductions-202605.md`
* `docs/long_form_model_inventory.md` — Tier 1 caption + preflight docs
* `.claude/skills/make-{cosmos-decoder,katha,rivalry-recap,skill,
  sleep-history,top10}/SKILL.md` (6 files) — path refs

## Memory entries (dual-save)

* `feedback_pipeline_subpackages_split.md` — package façade trap
  + alias-import pattern + test patcher rule
* `feedback_git_gc_reclaim_orphan_blobs.md` — `git gc --aggressive
  --prune=now` as the default-first-move for `.git` bloat
* `feedback_long_form_captions_ass_path.md` (Tier 1)
* `feedback_f5_reset_at_renderer_boundary.md` (Tier 1)
* `feedback_trim_aspect_match_extended.md` (Tier 1)

## Backup retained

`~/.git-ytFactory-backup-2026-05-05.tar` (11 GB). Safe to delete after
one cycle (e.g. one week) if no issues surface.
