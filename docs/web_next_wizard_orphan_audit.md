# Web-next wizard: orphan-component audit

**Established 2026-05-11** after the user flagged "why is the niche
picker gone — Source isn't a niche picker, that's `source_kind`".

The picker had never been deleted. It had been **defined but never
called** in `web-next/app/app/create/page.tsx` since commit
`dcb6d07` (the wizard rebuild on 2026-05-10). A comment promised
step 2 would surface it; the wiring was never written.

This doc encodes the audit discipline so the next wizard rewrite
doesn't silently regress the same way.

---

## What "orphan component" means here

A React component in a wizard file that is:
- defined (`function NicheBubbleRow(...) { ... }`), AND
- never invoked anywhere in the same file
   (no `<NicheBubbleRow ... />` JSX usage)

For wizards, "the same file" is sufficient because every wizard
component is co-located in `page.tsx`. For broader codebases, the
audit would also need to grep imports.

## Why it's silent

- `tsc --noEmit` does NOT flag unused functions (unlike unused
  variables — tsc's "unused" check covers `import`/`const` only).
- ESLint's `no-unused-vars` rule covers vars but not function
  declarations at the module scope by default.
- `next build` happily compiles. The dead code lives in the bundle
  but never executes.

## The audit (run after every wizard step rewrite)

### Step 1 — Enumerate every component defined in the file

```bash
grep -nE '^function [A-Z][A-Za-z0-9]*' web-next/app/app/create/page.tsx
```

This catches every PascalCase top-level function declaration —
React's component naming convention.

### Step 2 — For each, count how many times the name appears

```bash
for fn in $(grep -oE '^function [A-Z][A-Za-z0-9]*' \
              web-next/app/app/create/page.tsx | awk '{print $2}'); do
  # Match either JSX usage <Foo or bare reference Foo(...) anywhere
  count=$(grep -cE "<$fn[ />]|\\b$fn[ \\(]" \
            web-next/app/app/create/page.tsx)
  # 1 = only the definition line; 2+ = invoked at least once
  if [[ "$count" -le 1 ]]; then
    echo "ORPHAN: $fn"
  fi
done
```

### Step 3 — For each ORPHAN, decide

| state of the orphan | action |
|---|---|
| comment says "moved to step N" but step N's render path doesn't include it | wire it into step N; verify the user sees it |
| superseded by a newer component (e.g. `NicheBubbleRow` replaces `VariantPicker`) | delete the orphan + any sibling helpers ONLY IT used |
| genuine dead code from a half-shipped feature | delete the orphan + open an issue if the feature was needed |

### Step 4 — Also grep "the user no longer sees X" comments

```bash
grep -nE 'the user no longer sees|moved to step|will surface in step|surfaced in step' \
  web-next/app/app/create/page.tsx
```

For every match, verify the destination renders the moved
component. If the comment is a lie, either fix the destination or
remove the comment + the orphan.

### Step 5 — After any "replace function" edit, scan for duplicate top-level symbols

The `edit` tool is line-precise: it replaces the matched
`old_str` and nothing else. When a refactor swaps "the second of
two adjacent helpers" (e.g. replacing `VariantCard` with a combined
`function VariantList { ... } function VariantCard { ... }` block),
the **older** sibling above the matched region survives, silently
producing a duplicate top-level declaration. TypeScript's compiler
tolerates module-scope redeclaration (last definition wins) so
`tsc --noEmit` won't catch it; ESLint also typically doesn't
flag. Caught only by visual review or this audit.

```bash
# .ts / .tsx / .js / .jsx
grep -nE '^function [A-Z][A-Za-z]+\(' web-next/app/app/create/page.tsx \
  | awk -F'function ' '{print $2}' | awk '{print $1}' | sed 's/(//' \
  | sort | uniq -c | awk '$1 > 1 {print "DUPLICATE: "$2" ("$1"x)"}'

# Python (adjust the anchor)
grep -nE '^def [a-z_][a-zA-Z0-9_]*\(' pipeline/<file>.py \
  | awk -F'def ' '{print $2}' | awk '{print $1}' | sed 's/(//' \
  | sort | uniq -c | awk '$1 > 1 {print "DUPLICATE: "$2" ("$1"x)"}'
```

Any non-empty output means a refactor edit created a redeclaration —
delete the older copy. Run this AFTER every "replace `<Foo>` with
new implementation" edit on a file with multiple top-level helpers.

## Today's instance

Comment at `page.tsx:124-127` (added in `dcb6d07`):

```tsx
// Auto-pick the variant: prefer the user-saved niche from the
// channel /defaults editor, then the channel YAML's
// declared default_format, then the first variant. The user no
// longer sees a variant picker in step 1 — they can shuffle
// defaults in step 2 if needed.
```

Step 2's render path (`CustomizeReviewStep`) had a `Form` card +
a `Source` card (which was actually `source_kind`, not niche).
NO `<VariantPicker />` anywhere. The "shuffle in step 2" wiring
was never written. User went 1 day without a niche picker.

Fixed in commit `689c23d`: built `NicheBubbleRow` that reads
`nichesApi.list(channel)`, filters by `length_kind`, and renders
inside `CustomizeReviewStep`. Reuses the existing
`VariantList` + `VariantCard` so the visual language is
consistent. Deleted the orphan `VariantPicker`.

**2026-05-11 update (`531dd2e`):** the manual `Source` card was
removed entirely (the auto-generate Topic flow owns
`source_kind`/`source_ref` now), and the `NicheBubbleRow` was moved
to render **after** the Form CardShell, not above it. `VariantCard`
was also converted from a 2-line tile to a `rounded-full` chip pill
(single-line label, hover-tooltip for description). The current
order is: Form → Niche (chips) → Topic → Audio → Advanced. See
[`docs/web_next_create_wizard.md`](./web_next_create_wizard.md) for
the canonical layout rules and rationale.

## Auto-invoke this audit when

- Any commit message mentions "wizard", "step", "/create", or
  "rebuild" together with a frontend file change.
- Any comment in the form `the user no longer sees X — they can
  ... in step Y` is added or modified.
- After every rebase/merge that touches `web-next/app/app/<wizard>/page.tsx`.

## Related

- **Memory:** `feedback_wizard_step_orphans.md`
- **Commit:** `689c23d` (the fix + dead-code removal)
- **Related lint rule (not enforced):** `eslint-plugin-react/no-unused-prop-types`
  / `tsc --strict` does not catch this — would need a custom AST
  rule to block at PR time.
