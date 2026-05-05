# ytFactory — agent instructions

This file is loaded into every Claude Code session that runs in this repo.
It collects durable, project-wide rules. Channel-specific rules live under
`<channel>/learnings/`, cross-channel rules under `docs/`.

---

## Persistence rule: save every learning to BOTH memory and project docs

Whenever the user gives a durable rule, correction, preference, or finding
worth remembering, persist it in **BOTH** of these places — never just one:

1. **Central memory** — `~/.claude/projects/-Users-rohit-ytFactory/memory/`
   (private to the agent across sessions), with a pointer line in
   `MEMORY.md`.
2. **Project documentation** — an on-disk file inside this repo so the
   rule is visible to the user, teammates, and any other tool/agent.
   Pick the right home:
   - **Cross-channel rule** → `docs/<topic>.md` and link from each
     affected channel's `learnings/channel.md`.
   - **Channel-specific rule** → `<channel>/learnings/<topic>.md`,
     linked from that channel's `learnings/channel.md`.
   - **Pipeline-internal mechanic** → inline comment / docstring in
     the owning module, or a section in `docs/architecture.md`.

The project file is the source of truth (editable, reviewable). The
memory entry can be terser and link out to the project file by path.
Don't skip the project write — rules buried in private memory aren't
discoverable to anyone but the agent, which defeats half the point.

Exceptions (memory-only): ephemeral session state, user-profile facts,
raw activity logs — things that don't belong in the repo.

---

## Layout reference

Per-channel layout is the canonical 2026-05-05 spec. **Use
`pipeline.paths.RenderPaths` — never derive paths by string — see
[`docs/channel_layout.md`](./docs/channel_layout.md) for the full spec.**

Quick recap:

- Every YouTube channel: `/Users/rohit/ytFactory/<slug>/` with its own
  `config.yaml`, channel-wide subdirs (`scripts/`, `learnings/`,
  `branding/`, `music/`, `footage/`, …) and per-slug subdirs (`raw/`,
  `narrations/`, `cast/`, `shotlist/`, `uploads/`, `shorts/`,
  `long_form/`, `cache/`, `scratch/`, `critiques/`).
- **Niche rule:** a channel uses niches **everywhere or nowhere**.
  Per-slug subdirs nest under the niche when present
  (`mystoriesanimated/reddit_amitheasshole/narrations/<slug>.json`);
  channel-wide subdirs never do. Source of truth for variant→niche
  mapping is `pipeline/niches.py:NICHE_CHANNEL`.
- **Cross-channel state under `data/`** is reserved for genuinely
  cross-channel things only: `_bench/`, `cache/` (ML model weights),
  `research/`, `telemetry/`. Anything per-render lives under its
  channel root.
- Cross-cutting docs: `docs/`.
- Pipeline code: `pipeline/` (shared) + `<channel>/scripts/`
  (channel-specific entrypoints).
- Renderer entry points: `pipeline/render/{shorts,long_form,
  footage_only,sports_doc}.py` — channel-agnostic. Thin CLI shims at
  `scripts/<old-path>` preserve the legacy `python scripts/...`
  invocations.
