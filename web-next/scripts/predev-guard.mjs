#!/usr/bin/env node
/**
 * predev-guard — wipe a stale production .next/ before `next dev`.
 *
 * Why: if `next build` has run into the same .next/ that `next dev`
 * uses, dev mode happily SSRs the page HTML but every dev-mode chunk
 * URL it references (main-app.js, app/app/<route>/page.js, …) 404s
 * because only the production hashed chunks exist on disk. The page
 * loads, the React app never hydrates, and useEffect-driven data
 * fetchers never fire — so Queue / Channels (any client
 * component that loads its data on mount) is stuck on the skeleton
 * forever. Backend is fine; the symptom is purely frontend.
 *
 * Smoking gun: `.next/BUILD_ID` only exists after `next build` —
 * `next dev` does not write it. If it's there at dev-start time, the
 * directory is contaminated. Nuke it.
 *
 * Trade-off: this also drops .next/cache/ (webpack/swc/eslint), so
 * the first dev compile after a `build` is slower. That's fine and
 * exactly what we want — the alternative is a broken UI.
 */
import { existsSync, rmSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(process.cwd());
const buildIdPath = resolve(root, ".next", "BUILD_ID");

if (existsSync(buildIdPath)) {
  console.log(
    "[predev-guard] .next/BUILD_ID present — production build artifacts " +
      "would conflict with `next dev` (chunks 404, app never hydrates). " +
      "Wiping .next/ for a clean dev start.",
  );
  rmSync(resolve(root, ".next"), { recursive: true, force: true });
}
