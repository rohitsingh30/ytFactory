# Auto-invoked skills: skill-as-body, CLAUDE.md-as-trigger

**2026-05-08 — class-of-bug surfaced building `/update-docs`.**

## The pattern

When a user asks for a skill that should fire **automatically** ("every
time X happens, do Y", "auto-call this whenever …"), the natural
instinct is to redirect to `/update-config` (Claude Code hooks). That's
correct for pure automation (run a shell command on Stop, on
PreToolUse, etc.) but **wrong for the skill-as-body case**.

Claude Code hooks run shell commands. **Hooks cannot invoke a skill.**
A skill is loaded into Claude's prompt — only Claude can decide to
call it.

So when the body of the work is *Claude doing something* (reading
context, classifying, dual-saving docs, writing prose) and the user
wants it auto-fired, the architecture is:

| component | location | role |
|---|---|---|
| **skill body** | `.claude/skills/<name>/SKILL.md` | the actual workflow Claude runs when invoked |
| **auto-trigger rule** | `CLAUDE.md` (durable instructions) | tells Claude *when* to invoke the skill without being asked |
| **(optional) reminder hook** | `settings.json` Stop / SubagentStop hook | shell echo of a system-reminder so Claude is more likely to comply |

The CLAUDE.md rule is **instruction-following** based — Claude reads
it on every session start and is supposed to obey. Compliance isn't
hook-level guaranteed, but it's the only mechanism that works for
"Claude does X."

## How to recognize this case

The user says one of:
- "this skill should be auto-called whenever …"
- "every time X, run /this"
- "I shouldn't have to type the command"
- "fire this automatically"

AND the body of the work is Claude-driven, not shell-driven (reads
context, writes docs, classifies, talks to the user).

→ Build the skill normally, then add a CLAUDE.md trigger rule.

## How to recognize the *non* case (still redirect to /update-config)

If the body is purely shell:
- "every time I commit, run prettier"
- "after every render, gzip the mp4"
- "block the bash tool for `rm -rf`"

→ That's a hook. `/update-config` is the right answer. Skill is
overkill.

## What to write where

For the auto-invoked-skill case:

1. **Skill body** — author normally per the rest of the
   /make-skill heuristics (channel inheritance, quality gates,
   self-learning hook, etc.).
2. **CLAUDE.md auto-invocation rule** — a top-level section
   listing trigger conditions in plain English. Title format:
   `## Auto-invocation rule: /<skill-name> after <trigger summary>`.
3. **Skill SKILL.md "When to run it" section** — mirror the
   trigger conditions so the skill itself is self-describing. The
   skill should also include an `auto-invocation rule` section that
   points back to CLAUDE.md.
4. **Memory pointer** under the cross-channel section of MEMORY.md.

If the skill needs to *also* operationalize an existing project doc
(like `/update-docs` does for `docs/post_upload_analysis.md`), add a
header note to that doc saying "executable form is now
`/<skill-name>`" — don't fork the taxonomy.

## Anti-patterns

- **Don't try to wire it via a hook alone.** A Stop hook printing
  "you should run /this" is a *reminder* — it doesn't actually
  invoke. Useful as belt-and-braces, not as the primary mechanism.
- **Don't put trigger conditions only in the SKILL.md.** SKILL.md is
  loaded only when Claude decides to invoke. CLAUDE.md is loaded
  every session — that's where the auto-invocation rule lives.
- **Don't make the skill require manual invocation while *also* writing
  "this is auto-called" in the description.** That's a contract
  violation users notice. Either auto-fire or don't.

## /make-skill heuristic update (informally)

The existing heuristic — "if it's 'every time X happens, do Y', stop
and invoke `/update-config`" — should be qualified:

> If the body of the work is Claude-driven (reads context, writes
> docs, talks to the user), build the skill and add an
> auto-invocation rule to CLAUDE.md. Hooks alone can't invoke skills.

This file is the authoritative version of that qualification. Future
/make-skill runs should consult it before redirecting to
/update-config.

## Reference

- Primary case: `/update-docs` skill at
  `.claude/skills/update-docs/SKILL.md`, paired with CLAUDE.md
  "Auto-invocation rule" section.
- Memory pointer: `feedback_make_skill_auto_invoked_pattern.md`.
- /make-skill heuristics: `.claude/skills/make-skill/learnings/heuristics.md`.
