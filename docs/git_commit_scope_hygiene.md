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

## Cross-references

- `~/.claude/projects/.../memory/feedback_commit_scope_clarification.md`
  — terse memory pointer.
- `~/.claude/projects/.../memory/project_repo_hygiene_2026_05_10.md`
  — the broader repo-cleanup pass; this rule extends it to commit
  cadence specifically.
- `docs/admin_panel_first.md` — sibling cross-channel rule from the
  same 2026-05-10 cluster of "ask scope, don't assume".
