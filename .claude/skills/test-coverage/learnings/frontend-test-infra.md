# Frontend test infrastructure — standing TODO

The `/test-coverage` skill currently soft-passes React components
(`.tsx` files in `web-next/components/` and `web-next/app/`) because
web-next has no React testing framework installed.

## Current state (2026-05-12)

- ✅ Pure TS helpers in `web-next/lib/*.ts` — testable via
  Node 20+'s built-in `node --test`. Zero deps. See
  `web-next/tests/render-display.test.mjs` for the pattern.
- ⚠️  React components — no test infra. The skill marks them as
  soft-pass with a warning so a JSX tweak doesn't block the
  /update-docs commit.

## What's needed

Install one of:

1. **vitest + @testing-library/react** (recommended — vitest is
   the modern Next.js choice, and @testing-library is the
   industry-standard component-testing API).

   ```bash
   cd web-next
   npm install -D vitest @testing-library/react @testing-library/jest-dom \
     @testing-library/user-event @vitejs/plugin-react jsdom
   ```

2. **jest + react-testing-library** (older, more battle-tested,
   but jest's ESM story is rougher than vitest).

## Once installed

Update `scripts/coverage_gate.py::measure_typescript`:

- For React components: check for sibling test file at
  `web-next/tests/<component>.test.tsx` and run via vitest.
- Vitest has built-in line-level coverage (no extra plugin) —
  enable `--coverage` flag and parse the JSON output the same way
  the Python `coverage` integration does.

Update `.claude/skills/test-coverage/SKILL.md` Stage 4:

- Drop the "React/JSX components are soft-passed" branch.
- Real coverage with line-level diff math.

## Why we haven't done it yet

Trade-off:

- Adding vitest: ~50 MB node_modules, ~2-3s slower `npm install`,
  one more thing to keep up to date.
- Not adding vitest: every UI tweak ships untested.

The right time to flip the trade-off is when (a) the next
session burns time on a React component bug that a test would
have caught, OR (b) the team explicitly schedules a UI-test
infrastructure milestone.

## In the meantime

The pragmatic pattern that works without vitest:

1. Extract pure logic into `web-next/lib/*.ts` (no JSX, no React
   hooks, no DOM).
2. Test the helper with `node --test`.
3. The component file becomes a thin "render the helper's output
   into JSX" wrapper. A type-check (`npx tsc --noEmit`) catches
   the most common breaks.

The 2026-05-12 PlayerCard fix used this pattern: the
aspect/kind derivation moved to `lib/render-display.ts` (12
test cases, 100% covered) and the component reduced to plumbing.
