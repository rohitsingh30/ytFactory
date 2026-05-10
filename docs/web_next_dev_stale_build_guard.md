# `web-next` dev server — stale `next build` guard

**Established 2026-05-09** after the entire Studio UI (Library, Queue,
Channels, Settings, render-detail — every client-loaded page) was
stuck on its skeleton "loading" state in the browser, with the
backend control-plane returning 200 OK on every API call.

## The bug (high symptom-cause distance)

`next dev` and `next build` both write into the same
`web-next/.next/` directory. If a `next build` happens against that
tree (manual local build, `cloud/web-next/deploy.sh` rebuild,
accidental `npm run build`, an editor task, anything) and the dev
server is then started on top, the result is a silent split-brain:

- The dev server happily SSRs and serves the page HTML (200 OK).
- The HTML references dev-mode chunk URLs:
  `/_next/static/chunks/main-app.js`,
  `/_next/static/chunks/app/app/<route>/page.js`,
  `/_next/static/chunks/app-pages-internals.js`, etc.
- Only the **production hashed chunks** (e.g. `layout-4806fea0982d24fd.js`,
  `page-36c0f3fa885cff11.js`) live in `.next/static/chunks/` from the
  prior build.
- Every dev-mode chunk URL → `404`.
- React never hydrates → no `useEffect` ever fires → every
  `setState`-on-mount data loader (Library uses `jobsApi.list`, Queue
  uses `queueApi.get`, Channels uses `channelsApi.list`, etc.) stays
  on its initial `null` → skeleton forever.

The backend looks healthy. `curl /api/jobs` works. `curl /api/queue`
works. Even `curl http://127.0.0.1:3000/api/queue` (through the Next
proxy) works. The dev server log is the only place the truth lives:

```
GET /_next/static/chunks/main-app.js 404 in 21ms
GET /_next/static/chunks/app-pages-internals.js 404 in 20ms
GET /_next/static/chunks/app/app/library/page.js 404 in 23ms
```

## Smoking gun

`.next/BUILD_ID` is written by `next build` and **never** by
`next dev`. If it's present at dev-start time, the directory is
contaminated.

## Fix (landed 2026-05-09)

`web-next/scripts/predev-guard.mjs` — a npm `predev` hook (runs
automatically before `npm run dev`). On each invocation:

1. Check for `.next/BUILD_ID`.
2. If present, print a one-line `[predev-guard]` notice and `rm -rf
   .next/`.
3. If absent, no-op (zero overhead on the steady-state dev loop).

Wired in `web-next/package.json`:

```json
"scripts": {
  "predev": "node scripts/predev-guard.mjs",
  "dev":    "next dev -p 3000",
  ...
}
```

Trade-off: the first `npm run dev` after a `next build` re-compiles
from scratch (loses `.next/cache/{webpack,swc,eslint}` too). That's
the right cost — the alternative is the silent broken-UI we spent
half a debugging session chasing.

## Triage steps if a Studio page still hangs on the skeleton

1. **Check the Next dev log first**, not the API:
   ```bash
   tail -50 /tmp/next-dev.log | grep -E "404|error"
   ```
   If you see `_next/static/chunks/*.js 404`, this is the bug. Stop
   and remediate. Don't waste time on the API or React component.

2. **Confirm with curl**:
   ```bash
   curl -sS -o /dev/null -w "%{http_code}\n" \
     http://127.0.0.1:3000/_next/static/chunks/main-app.js
   ```
   Should be 200. If 404, contaminated.

3. **Verify the smoking gun**:
   ```bash
   ls web-next/.next/BUILD_ID
   ```
   Present? Predev guard didn't run (or was bypassed).

4. **Manual remediation** (if guard was bypassed by a hand-rolled
   launcher):
   ```bash
   pkill -f "next dev"; pkill -f "next-server"
   rm -rf web-next/.next
   cd web-next && npm run dev
   ```

## Why we don't just split the build dir

Two cheaper options were considered and rejected:

- **`distDir: ".next-prod"` in `next.config.mjs`** — works but breaks
  every external assumption (`cloud/web-next/Dockerfile`, deploy
  scripts, the standalone-output path, Cloud Run packaging). High
  blast radius for a problem the predev hook fixes in 30 lines.
- **Refuse to build into a dev-occupied tree** — `next build` doesn't
  expose a flag for this and we don't want to fork it.

The npm hook is invisible on the happy path, surfaces the wipe with a
clear message on the trigger, and adds zero ops to anyone using the
documented `npm run dev` entrypoint.

## Caveat: hand-rolled launchers must use `npm run dev`

The hook fires on the npm `predev` lifecycle. If you launch the dev
server directly — `npx next dev`, `node node_modules/.bin/next dev`, a
custom shell wrapper that bypasses npm — the guard does NOT run. Use
`npm run dev` (or update the launcher to `cd web-next && npm run dev`)
to keep the guarantee.

## Reference

- Fix: `web-next/scripts/predev-guard.mjs`, `web-next/package.json`
- Memory pointer: `feedback_web_next_dev_stale_build.md`
- Local-dev recipe: `web-next/README.md` § "Local dev"
- Production build path (the contaminator): `web-next/README.md` §
  "Production build", `cloud/web-next/deploy.sh`
