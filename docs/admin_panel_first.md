# Admin-panel-first for ops & observability (cross-channel)

> **Established 2026-05-10** after 4 stillborn `.claude/skills/`
> (`cloud-cost`, `cloud-health`, `deploy-cloud-service`, `warm-cloud`)
> were retired the day they were authored. Trigger quote from the user:
> *"I want tab for seeing all this info similar to admin panel."*

## The rule

When the work surface is **operations, status, observability, or
infrastructure** (cost, health, deploys, queue state, render history,
user/auth admin, burn-rate, model registry, …), the canonical home is
the **`web-next` admin panel** (`web-next/app/app/<topic>/page.tsx`),
not a CLI skill under `.claude/skills/`.

CLI skills are for **content authoring** (`/make-*`) and
**post-render review** (`/critique-*`, `/ingest-critiques`,
`/clone-video-format`). Anything that's a recurring "is X up? what
did X cost? when was X last deployed?" question belongs in the
panel, where the operator can see all of it on one screen instead of
shell-hopping.

## Why

The 2026-05-10 audit found four skills (`cloud-cost`, `cloud-health`,
`deploy-cloud-service`, `warm-cloud`) whose **own descriptions
self-incriminated** as harness automation:

| skill | self-description quote | should be |
|---|---|---|
| `cloud-cost` | "Auto-invoked weekly via cron and after every `/deploy-cloud-service`" | cron + admin tab |
| `cloud-health` | "Auto-invoked by render entrypoints right before stage 1, by the GitHub Actions hourly health cron" | renderer hook + admin tab |
| `warm-cloud` | "Auto-invoked by every `make-*` skill at script finalization, the GitHub Actions cron, the web-server when a user clicks Generate" | renderer hook + admin tab + button |
| `deploy-cloud-service` | "Refuses to invoke `gcloud builds submit` until all 6 prep steps are checked off" | bash wrapper + checklist tracked in admin tab |

None of them did Claude-driven body work (writing prose, classifying
critiques, talking to the user). All four were thin shell wrappers
around existing infra that the operator wanted to **see** — which
is what an admin panel does natively.

The migration outcome:

- 4 skills (~30 KB SKILL.md) → 1 admin tab (`/app/cloud`) + 1 cron
  (`scripts/cloud_daily_snapshot.py`) + 1 importable module
  (`pipeline/cloud/`).
- Operator now sees Health / Cost / Deploys on one screen at
  `web-next/app/app/cloud/page.tsx`.
- CLI / cron / web all read from the same `pipeline/cloud/` core.

See `docs/cloudrun_admin_panel.md` for the panel runbook.

## How to apply

Before authoring a new `/cloud-*` / `/admin-*` / `/status-*` /
`/cost-*` / `/deploy-*` / `/health-*` skill, run this gate:

1. **Is the body Claude-driven?** (writing prose, classifying outputs,
   talking to user) → maybe a skill is right; check
   `feedback_make_skill_auto_invoked_pattern.md` for the auto-fired
   variant.
2. **Is the body shell-driven?** (gcloud, curl, jq, bash) → it's
   either a script (`scripts/<name>.sh`), a cron (launchd plist), or
   a panel tab. NOT a skill.
3. **Does the operator want to *see* this info recurrently?** → the
   answer is an admin tab + API route + cron snapshot. Pattern:
   - `pipeline/<topic>/` (one importable Python module — health,
     cost, snapshot, etc).
   - `control/routes/<topic>_routes.py` (FastAPI endpoints).
   - `web-next/app/app/<topic>/page.tsx` + sibling section files.
   - `scripts/<topic>_daily_snapshot.py` + launchd plist if the data
     is expensive to compute live.
   - Sidebar entry in `web-next/components/nav/sidebar.tsx`
     (`adminOnly: true` for sensitive data).
4. **Does it auto-fire on a condition the renderer / cron knows
   about?** → wire it directly into the renderer entrypoint or a
   launchd plist. See
   `feedback_renderer_owns_universal_pre_steps.md` for the renderer
   hook pattern.

## Existing precedents in `web-next/app/app/`

- `/app` — Dashboard
- `/app/queue` — Queue state (job status)
- `/app/channels` — Channel registry + per-channel state
- `/app/burner-channels` — Burner channel registry
- `/app/admin` — User/access admin
- `/app/cloud` — Cloud Run health / cost / deploys (this rule's
  reference implementation, 2026-05-10)
- `/app/settings` — App settings
- `/app/render/[jobId]` — Render detail + live timeline

When adding a new tab:

- Match the visual language: `PageHeader` + stacked `section`s with
  `font-mono [10px] uppercase tracking-[0.18em]` headers; tone via
  Tailwind tokens (no decorative gradients except the hero).
- Use `useVisiblePoll` for live data; daily snapshot for expensive
  data (BigQuery, gcloud builds list, etc).
- Add to `NAV_ITEMS` in `web-next/components/nav/sidebar.tsx` with
  the right `adminOnly` flag and a 2-letter `G <X>` shortcut.

## Cross-references

- `docs/cloudrun_admin_panel.md` — the 2026-05-10 reference implementation
- `.claude/skills/make-skill/learnings/heuristics.md` § Heuristic 4 —
  "every time X, redirect to /update-config" (shell hooks)
- `~/.claude/projects/.../memory/feedback_make_skill_auto_invoked_pattern.md`
  — Claude-driven auto-fired skill pattern (the inverse case)
- `~/.claude/projects/.../memory/feedback_renderer_owns_universal_pre_steps.md`
  — when "every make-* must do X" → put X in the renderer
- `~/.claude/projects/.../memory/feedback_skill_harness_drift_audit.md`
  — periodic audit recipe to catch drift before it ships
