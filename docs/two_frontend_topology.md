# Two-frontend topology — web-next vs web/static

ytFactory has **two HTML front-ends** that look superficially similar
but serve different paths in production. Editing the wrong one is the
single most expensive UX mistake in this repo: 4 useless deploy
cycles in the 2026-05-11 niche-bubble session, ~25 minutes of "why
isn't my change live".

## Topology

```
                   user browser
                         │
                         ▼
            ┌───────────────────────────┐
            │   ytfactory-web-next      │   Cloud Run SERVICE
            │   (Next.js, web-next/)    │   asia-southeast1
            └────────────┬──────────────┘
                         │
              path-based proxy (next.config.mjs + app/api/[...path])
                         │
   ┌─────────────────────┼─────────────────────┐
   │                     │                     │
   ▼                     ▼                     ▼
/app/*              /api/*  /agent/*  /healthz
(rendered          (proxied to ytfactory-web)
by web-next)
                                                          │
                                                          ▼
                                        ┌──────────────────────────┐
                                        │   ytfactory-web          │   Cloud Run SERVICE
                                        │   (FastAPI, web/server.py)│   asia-southeast1
                                        │                           │
                                        │   ALSO serves a LEGACY    │
                                        │   HTML site:              │
                                        │   /            → landing  │
                                        │   /create      → niche    │
                                        │                  picker   │
                                        │   /static/*    → assets   │
                                        └──────────────────────────┘
```

## What each frontend looks like

| layer                          | served at                  | source files                           | status        |
|--------------------------------|----------------------------|----------------------------------------|---------------|
| **web-next** (canonical)       | `/`, `/app/*` on prod URL  | `web-next/app/**`, `web-next/components/**` | active prod  |
| web/static (legacy)            | the legacy domain only; on the prod web-next domain it's effectively unreachable | `web/static/index.html`, `web/static/landing.html`, etc. | dead-but-served |

The legacy site is still served by `ytfactory-web` because the FastAPI
app mounts `web/static/` as a static dir. **It is reachable directly**
via `https://ytfactory-web-…run.app/` — but the user-facing URL the
user is on is the `ytfactory-web-next-…run.app` domain, which renders
web-next. The legacy site has not been migrated to a `/legacy/*`
prefix yet (see `docs/deploy.md` § "Cutover from legacy" for the
half-finished plan).

## Decision tree — which file do I edit?

When the user reports a UI bug or asks for a UI change:

1. **Ask: are they on `ytfactory-web-next-…` or `ytfactory-web-…`?**
   - URL contains `-web-next-` → web-next. Edit `web-next/app/**`.
   - URL is `-web-` only → legacy. Edit `web/static/**`.
2. **If they paste UI text from the page**, look for tells:
   - "Step 03 / 03" / "Customize & review" / "Form / Short form / Long form" /
     numeric step indicators / shadcn-style cards → web-next.
   - "Make a Short" picker page with niche cards in a horizontal reel /
     Tailwind CDN script tag in the source / `id="picker"` → legacy.
3. **Default to web-next.** That's the canonical user-facing
   experience. Only touch `web/static/**` if the user is explicitly
   debugging the legacy site.

## How edits become visible

| change                          | required to see live                                                                |
|---------------------------------|--------------------------------------------------------------------------------------|
| `web-next/**` TS/TSX            | `npm run build` THEN `./cloud/web-next/deploy.sh`. Hard reload.                     |
| `web/static/**` HTML/JS         | `./cloud/web-server/deploy.sh`. Hard reload. (Only visible on the `-web-` URL!)     |
| `web/server.py`                 | `./cloud/web-server/deploy.sh`. Both frontends consume the API the same way.        |
| `pipeline/**` (libs)            | redeploys whichever service uses them; usually `ytfactory-web`.                     |
| GCS state (e.g. niches/* JSON)  | **no redeploy** — re-run the seeder, then hard-reload `/app/create`.                |

## Symptoms of wrong-frontend edits

If the user is on `-web-next-` and you edited `web/static/index.html`:
- Build + deploy will succeed.
- The change will be invisible on the user's URL.
- The user will be (rightly) furious.

The 2026-05-11 niche-bubble session hit this 4 times before the user
caught it ("don't see niche picker" + screenshot). The fix is the
decision tree above + `Always check the URL the user is screenshotting
or talking about before editing.`

## Cross-references

- `cloud/web-server/deploy.sh` — deploys the FastAPI / legacy-static service
- `cloud/web-next/deploy.sh` — deploys the Next.js service
- `docs/deploy.md` § "Cutover from legacy" — the half-finished plan to
  retire `web/static/**` behind `/legacy/*`
- `docs/architecture.md` — overall service topology
- `web-next/next.config.mjs` + `web-next/app/api/[...path]/route.ts` —
  the proxy that routes `/api/*` from web-next to ytfactory-web
