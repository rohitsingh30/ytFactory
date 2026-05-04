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

- Each YouTube channel: `/Users/rohit/ytFactory/<slug>/` with its own
  `config.yaml`, `scripts/`, `learnings/`, and state subdirs (`raw/`,
  `narrations/`, `cache/`, `shorts/`, `uploads/`, `branding/`, …).
- Cross-cutting docs: `docs/`.
- Pipeline code: `pipeline/` (shared) + `<channel>/scripts/`
  (channel-specific entrypoints).
