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

## Sibling pattern: API client orphans (2026-05-11)

The same anti-pattern hit `web-next/lib/api.ts` for the burner-channels
page. `burnerApi.subscribeAllBurners()` was added to the API client
in commit `d1f592b` (2026-05-11) — a stray addition inside a discover-
feed commit that bore no relation to burners. **No UI button ever
called it. No backend route ever served it.** The user thought the
"Subscribe All" + "Create 50 channels" buttons were on the page
because the API helper existed; they were not.

This is the same shape as the wizard-orphan bug, one layer deeper:

| layer            | wizard-orphan         | API-client orphan                                    |
|------------------|-----------------------|------------------------------------------------------|
| where defined    | page.tsx              | lib/api.ts                                           |
| how it's silent  | tsc/eslint don't flag | tsc/eslint don't flag; helper has a real type        |
| what's missing   | JSX call site         | JSX call site **AND** backend route handler         |

### Audit recipe — API helpers

After any `web-next/lib/api.ts` edit (and as a periodic full-file
sweep — the helper file gets stray additions during cross-cutting
commits), run:

```bash
# 1. List every helper exported by api.ts
grep -nE '^[[:space:]]+[a-z][A-Za-z0-9]*:[[:space:]]*\(' web-next/lib/api.ts

# 2. For each helper name, check it's actually called from a page/component
for fn in $(grep -oE '^[[:space:]]+[a-z][A-Za-z0-9]*:' web-next/lib/api.ts | tr -d ' :'); do
  count=$(grep -rEc "\\b${fn}\\(" web-next/app/ web-next/components/ 2>/dev/null \
            | awk -F: '{s+=$2} END {print s+0}')
  if [[ "$count" -eq 0 ]]; then
    echo "ORPHAN api helper: ${fn}"
  fi
done

# 3. For each helper that POSTs/GETs a path, verify the backend route exists
#    Extract the URL literal from the helper, then grep control/routes/ for it.
grep -nE '`/api/[^`]+`' web-next/lib/api.ts | while IFS=: read -r line _ url_line; do
  path=$(echo "$url_line" | grep -oE '/api/[a-z_/{}-]+' | head -1)
  prefix=$(echo "$path" | awk -F/ '{print "/"$2"/"$3}')   # e.g. /api/burner_channels
  if ! grep -rq "prefix=\"${prefix}\"" control/routes/ 2>/dev/null; then
    echo "api.ts L${line}: POSTs ${path} — no router with prefix ${prefix} in control/routes/"
  fi
done
```

### When the audit fires

* Right before committing any change to `web-next/lib/api.ts`
  (especially when the change rides along inside a commit that's
  about something else — those are the 99% of orphans).
* As a quarterly hygiene sweep regardless of commits.
* Whenever the user says "where's the X button?" and it sounds
  feature-shaped — most of the time the helper was added but the
  button + route never followed.

### Today's instance

`subscribeAllBurners` shipped as a stray addition in `d1f592b`
(2026-05-11 02:18) — a commit titled `feat(discover): every channel
auto-generate w/ form-context-aware LLM blend`. Zero callers anywhere
in `web-next/app/`. Zero handlers in `control/routes/burner_routes.py`.
Closed in commit `e4bcaae` by adding the `BulkActions` component +
`subscribe_all_burners` route + corresponding `create_bulk` companion.

The lesson is the same as the wizard-orphan one: **stray scaffolding
in a polyglot frontend/backend repo only leaks when somebody asks
about the feature it implies.** Every orphan in `lib/api.ts` is a
ghost feature waiting to confuse the user.

## Related

- **Memory:** `feedback_wizard_step_orphans.md`
- **Commit:** `689c23d` (the fix + dead-code removal)
- **Related lint rule (not enforced):** `eslint-plugin-react/no-unused-prop-types`
  / `tsc --strict` does not catch this — would need a custom AST
  rule to block at PR time.
