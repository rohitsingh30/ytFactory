# ytFactory web-next

Next.js 14 (App Router) + Tailwind + shadcn/ui. The customer-facing
surface for ytFactory. Replaces the four loose vanilla-JS pages in
`web/static/`.

Design language: monochrome / Geist / restrained accent — same room as
Linear, Vercel, v0, Cursor. No emoji chrome, no decorative gradients.
The signature gradient is used exactly once (hero display type).

## Stack

- Next.js 14 (App Router, RSC, standalone output)
- TypeScript (strict)
- Tailwind CSS + shadcn/ui design tokens
- Radix UI primitives
- Geist Sans + Geist Mono (Vercel)
- Framer Motion (subtle, purposeful)
- Lucide icons (single-line, monochrome)

## Local dev

```bash
# Terminal 1 — FastAPI control plane
cd /Users/rohit/ytFactory && \
  YTFACTORY_AGENT_TOKEN=$(cat .agent-token 2>/dev/null || echo dev-token) \
  YTFACTORY_QUEUE_BACKEND=memory \
  .venv/bin/uvicorn control.server_dev:app --host 127.0.0.1 --port 8765

# Terminal 2 — Next.js
cd /Users/rohit/ytFactory/web-next && npm install && npm run dev
# → http://localhost:3000
```

Override the API base if the control plane runs elsewhere:

```bash
YTFACTORY_API_BASE=<your-control-plane-url> npm run dev
```

## Production build

```bash
npm run build       # emits .next/standalone
npm start           # serves the standalone output on :3000
```

The Cloud Run image (built in the `deploy-cloudrun` todo) packages the
standalone build behind path-routed LB rules; same domain as the FastAPI
service.

> **Stale-build guard.** `next dev` and `next build` share the same
> `.next/` directory. If `next build` runs into the same tree the dev
> server uses, every dev-mode chunk URL 404s and the React app never
> hydrates — every Studio page (Queue, Channels, …) stays on
> its skeleton "loading" state forever. The `predev` npm hook
> (`scripts/predev-guard.mjs`) detects this (`.next/BUILD_ID`
> present) and wipes `.next/` automatically before `npm run dev`.
> If you launch the dev server outside npm (`npx next dev`, custom
> shell wrapper), the guard does NOT run — keep launchers on
> `npm run dev`. Full write-up:
> [`docs/web_next_dev_stale_build_guard.md`](../docs/web_next_dev_stale_build_guard.md).

## Design tokens

`styles/globals.css` defines the token ladder:

- `--background` near-black (`hsl(0 0% 4%)`)
- `--surface` / `--surface-2` two elevation steps
- `--border` / `--border-strong` two divider weights
- `--foreground` text, `--muted-foreground` supporting text
- `--accent` the *one* color (violet 70%) — used sparingly on focus and
  active moments; **never** for fills
- `display-gradient` utility — the one signature gradient, used in hero
  display type only

## Layout

```
app/
  layout.tsx                    Root: Geist fonts + tooltip provider
  page.tsx                      Landing
  app/
    layout.tsx                  Studio shell (sidebar + topbar)
    page.tsx                    Dashboard placeholder
components/
  ui/                           shadcn/ui primitives (mono, restrained)
  nav/                          Sidebar + Topbar
  marketing/                    Landing-only sections (mockup, bento, …)
lib/
  api.ts                        Typed fetch + SSE
  types.ts                      Mirror of FastAPI schema
  utils.ts                      cn(), formatters
styles/globals.css              Tokens + utilities
```
