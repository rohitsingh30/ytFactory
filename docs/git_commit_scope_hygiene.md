# Git commit scope hygiene (cross-channel)

> **Established 2026-05-10** after a multi-session working tree
> (159 dirty files, ~57 from the active session) almost got lumped
> into one `feat(cloud):` commit because the user said *"commit
> everything in one tidy commit."* The agent paused, ran
> `git status --short | wc -l`, saw the gap, and asked one focused
> scope question before staging.

## The rule

When the user asks "commit the work" / "commit everything" / "ship
it", do **not** `git add .` or `git add -A` blindly. Run this
2-step gate first:

1. `git status --short | wc -l` — count of dirty files in the tree.
2. Compare to the count of files **this session** touched (you can
   reconstruct from your own edit history; or `git diff --stat HEAD
   | tail -1` after a deliberate `git add` is also fine).

If the two counts differ by ≥10 files OR the dirty tree contains
files in directories you never touched (e.g. `pipeline/audio/` was
refactored when your session was on `pipeline/cloud/`), **stop and
ask the user**:

```
git status shows N dirty files. My session touched M of them. Want
to commit (a) just my session's M files, (b) the whole tree as one
mixed commit, or (c) show buckets first?
```

Only then `git add <explicit-list>` and commit.

## Why

`git add .` lumps unrelated work into one commit. Three failure
modes:

- **Hard to revert.** A bug surfaces in week N+1; bisect lands on
  the mixed commit; reverting it backs out 5 unrelated features.
- **Hard to review.** Reviewer can't tell which file belongs to
  which logical change.
- **Hard to write a good message.** The commit message either
  lies (claims one feature when 5 shipped) or becomes a 50-line
  changelog nobody reads.

The 2026-05-10 case: working tree had 159 dirty files; the active
session was the cloud admin tab (~57 files). The other 100 were
genuinely unrelated work from prior sessions
(`pipeline/audio/` refactor, jobs-snapshot doc,
customize-form-to-render contract, render-worker Dockerfile, etc.).
Lumping all 159 under "feat(cloud)" would have been a lie.

## How to apply

When committing on the agent's own initiative (no user "commit"
ask), **always** stage explicit paths from this session — never
`git add .`. List them in the commit so future archeology can map
files-to-rationale.

When the user asks to commit:

1. Run `git status --short | wc -l`.
2. If it matches what you expect (you've been working on a clean
   baseline) → stage explicit paths and commit.
3. If it doesn't match → ask the scope question above. Don't
   assume the user wants the whole tree.

## Recipes

- Show counts:
  ```bash
  echo "tree dirty: $(git status --short | wc -l)"
  echo "your session: <count from your own working memory>"
  ```
- Show file buckets by directory (when the user picks "show
  buckets first"):
  ```bash
  git status --short | awk '{print $NF}' | xargs -n1 dirname | sort | uniq -c | sort -rn | head -20
  ```
- Stage explicit paths only:
  ```bash
  git add path/a path/b path/c…
  git diff --cached --stat | tail -3   # confirm before committing
  ```
- The commit message should follow the project trailer rule
  (`Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`).

## 2026-05-11 update — `git checkout HEAD -- <file>` LOSES uncommitted WIP

**The hazard.** When you need to "reset" a file mid-workflow (e.g.
to re-apply your edit on a clean base before committing),
`git checkout HEAD -- pipeline/render/shorts.py` will overwrite
the working tree with HEAD's version — including any uncommitted
WIP from previous sessions or other agents. The user's
`_make_short_impl` envelope-extraction WIP for shorts.py was
silently wiped this way during the per-render image fan-out ship
(commit `a3c4e47`), discovered only when a post-checkout
`grep _make_short_impl` returned 0.

**Don't do this in a dirty tree without a backup:**

```bash
# ❌ Destroys uncommitted WIP for the file
git checkout HEAD -- pipeline/render/shorts.py
```

**Do this instead** — stash everything first, then surgically
restore + commit only your isolated subset:

```bash
# 1. Snapshot everything (the user's WIP + your edits) into a stash.
#    -u captures untracked files too.
git stash push -u -m "WIP+ship-<topic>"

# 2. Working tree is now clean (matches HEAD). Restore ONLY YOUR
#    target files from the stash. `git checkout stash@{0} -- <files>`
#    pulls those files' stash content into both index AND working tree.
git checkout stash@{0} -- \
  cloud/foo/bar.sh \
  pipeline/your/module.py \
  docs/your_doc.md

# 3. Verify the stage looks correct (only YOUR diff vs HEAD).
git diff --cached --stat

# 4. Commit.
git commit -m "your message" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"

# 5. Pop the stash to restore the user's WIP back on top.
#    Conflicts on YOUR files (since they're now committed) need a
#    3-way merge — usually trivial because the changes overlap, but
#    LOOK at the diff before declaring done.
git stash pop
```

**If you DID accidentally lose WIP:** uncommitted changes that
existed in the working tree are usually still in `.git/objects/`
as dangling blobs (git auto-snapshots before destructive
operations). Recovery recipe:

```bash
# 1. List dangling blobs by content shape (size range you expect).
git fsck --lost-found 2>&1 | grep "dangling blob" | awk '{print $3}' > /tmp/blobs.txt

# 2. For each, peek at line 1 to fingerprint which file it was.
while read blob; do
  if git cat-file -p $blob 2>/dev/null | head -1 | grep -q "End-to-end orchestrator"; then
    impl=$(git cat-file -p $blob 2>/dev/null | grep -c "def _make_short_impl")
    lines=$(git cat-file -p $blob 2>/dev/null | wc -l)
    echo "$blob lines=$lines _make_short_impl=$impl"
  fi
done < /tmp/blobs.txt

# 3. Restore the matching blob to disk.
git cat-file -p <blob-sha> > pipeline/render/shorts.py
```

This recovered the lost `_make_short_impl` WIP in the 2026-05-11
session (blob `46d4ef2313a5170b97e4ee41d169ac8f6fbc348f`,
3128 lines, contained both the WIP and my refactor).

**The wider rule:** When working in a tree with substantial
pre-existing WIP AND another agent might be committing in
parallel (notice: critique-runner committed `c105397`, `5fe93f0`,
`857f2ba`, `b5ecf1f` mid-session 2026-05-11), assume your
working-tree state is fragile. Snapshot via `git stash` BEFORE
any destructive operation (`git checkout HEAD --`, `git restore --`,
`git reset --hard`, `git clean`).

## Cross-references

- `~/.claude/projects/.../memory/feedback_commit_scope_clarification.md`
  — terse memory pointer.
- `~/.claude/projects/.../memory/project_repo_hygiene_2026_05_10.md`
  — the broader repo-cleanup pass; this rule extends it to commit
  cadence specifically.
- `~/.claude/projects/.../memory/feedback_git_checkout_head_loses_wip.md`
  — terse memory pointer for the 2026-05-11 update.
- `docs/admin_panel_first.md` — sibling cross-channel rule from the
  same 2026-05-10 cluster of "ask scope, don't assume".
- `.claude/skills/update-docs/learnings/_index.md` § 2026-05-10
  late "Stash-pop after parallel-agent commit" — sibling
  observation that escalated this update to a documented rule.

## 2026-05-11 update — `.gitignore` audit before declaring scope

Sub-rule of the scope-clarification flow. When `git status` shows
≫ session-touched count (e.g. 273 unstaged files when you only
worked on 6), don't just clarify scope — **also audit `.gitignore`
for runtime-output dirs that were never excluded**.

Today's catch: 110 transient files (97 `data/burner_engage/<slug>.json`
worker-state files + 13 `cloud/deploy_logs/*.{log,rc}` build logs)
were tracked-as-untracked because `.gitignore` had no rules for
them. They're regenerable, mirrored to GCS in prod, and pure
runtime noise. Adding two `.gitignore` lines (`data/burner_engage/`
and `cloud/deploy_logs/`) reduced the unstaged count by 108 — about
40% of the total — without committing anything.

### Audit recipe

```bash
# 1. After noticing >50 untracked files, group by top-level dir:
git ls-files --others --exclude-standard \
  | awk -F/ '{if (NF>=2) print $1"/"$2; else print $1}' \
  | sort | uniq -c | sort -rn | head -20

# 2. For each cluster ≥10 files, decide:
#    - Pure runtime / regenerable / mirrored elsewhere → add to .gitignore
#    - Genuine source code that should ship → commit it (separate scope)

# 3. Confirm the gitignore additions worked:
git status --short --untracked-files=all | wc -l
# Should drop by the size of the gitignored cluster.
```

### Common candidates for `.gitignore` (caught so far)

| dir | rationale | added |
|---|---|---|
| `data/burner_engage/` | per-slug worker state, mirrored to GCS via `pipeline/cross_engage/burner_engage.py` | 2026-05-11 |
| `cloud/deploy_logs/` | per-service Cloud Build/Run output captured by `cloud/<svc>/deploy.sh` wrappers | 2026-05-11 |
| `data/research/youtube/` + `data/research/analytics/` | YouTube stats cache; refresh via Cloud Scheduler JOB | 2026-05-10 |

### Why this rule lives next to "ask scope first"

The original 2026-05-10 lesson was "don't `git add .` on a multi-
session dirty tree". The 2026-05-11 corollary is **"if a single
runtime tool generates 50+ files in a single dir, that dir
probably belongs in `.gitignore` regardless of session scope"** —
otherwise every future scope-clarification conversation re-asks
whether to commit those files.

### See also

- `feedback_commit_scope_clarification.md` (parent memory entry)
- Today's `.gitignore` diff in commit `689c23d`
