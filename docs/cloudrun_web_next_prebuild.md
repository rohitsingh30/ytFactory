# Cloud Run web-next: pre-build .next/ on the host

**TL;DR:** never run `next build` inside the `cloud/web-next/` Docker image. Build
`.next/` on macOS via `npm run build`, ship the prebuilt artifacts, install
**only runtime** `node_modules` in Docker. `cloud/web-next/deploy.sh` does this
automatically.

## What broke (2026-05-10)

The `ytfactory-web-next` Cloud Run service started serving HTML missing the
`<!DOCTYPE>`, `<html>`, `<head>`, `<body>` opening tags — but with
`</body></html>` at the end. Browsers responded with `HierarchyRequestError:
Only one element on document allowed`, React hydration errors #418/#423,
blank pages, 404s on the referenced CSS file (the CSS hash baked into the HTML
didn't match what was on disk).

After exporting the deployed image's filesystem with `crane export`, the
prerendered files on disk (`.next/server/app/index.html`,
`.next/server/app/login.html`, etc.) were themselves broken — same first 200
bytes as the served response. So the bug was in **build**, not in serving.

The same source code building on the laptop (macOS arm64) produced valid HTML
(`<!DOCTYPE html><html lang="en" class="dark __variable_245d8d ...">...`).
Switching base image (alpine ↔ slim), Next.js mode (standalone ↔ `next start`),
and adding `dynamic = "force-dynamic"` did not fix the build-side bug.

The webpack runtime hash differed between local (`webpack-689e5ca5a56fbe5f.js`)
and Cloud Build (`webpack-87d71e3c0688ecde.js`) builds, which suggested
non-determinism even with `package-lock.json` present.

## The fix

`cloud/web-next/Dockerfile` no longer runs `npm run build`. It:

1. `npm ci --omit=dev` — installs only runtime deps (Linux x64 SWC etc.) from
   the pinned `package-lock.json`.
2. `COPY web-next/.next ./.next` — ships the prebuilt artifacts produced on the
   laptop.
3. `CMD next start` — pure runtime.

`cloud/web-next/deploy.sh`:

1. `cd web-next && npm run build` — fresh local build before every deploy.
2. Sanity check: `head -c 64 web-next/.next/server/app/index.html` must contain
   `<!DOCTYPE html>` or the script aborts (refuses to ship a broken build).
3. `gcloud builds submit` + `gcloud run deploy` as before.

`.gcloudignore` excludes `**/.next/cache/` (115 MB of webpack/swc cache that
isn't needed at runtime).

## Why this works

The build-side bug is environment-specific (some interaction between Next.js
14.2.18, React 18.3.1, `geist@1.7.0`'s `next/font/local` invocation, and the
Linux x64 SWC binary). The serving side (`next start`) is just an HTTP wrapper
around prerendered files — there's no rebuild. So as long as the prerendered
files are valid, the served response is valid.

The artifacts under `.next/server/`, `.next/static/`, `.next/types/` are
plain JSON / HTML / JS — platform-agnostic. The Linux node_modules installed
inside the container only contain the runtime (`next`, `react-dom`, etc.) and
its native bindings. They're a clean separation.

## Required env on the Cloud Run service

```
YTFACTORY_API_BASE=https://ytfactory-web-7hwnzw7lya-as.a.run.app
NEXT_TELEMETRY_DISABLED=1
YT_AUTH_ENABLED=1                  # enables edge middleware /app/* gate
```

`deploy.sh` sets all three.

## Auth flow wiring

The web-next service IS the public face. FastAPI sits behind the proxy:

- `/api/*` → web-next route handler (`app/api/[...path]/route.ts`) → injects
  Cloud Run ID token → forwards to `ytfactory-web` (FastAPI).
- `/agent/*`, `/healthz` → plain rewrites in `next.config.mjs`.

For the OAuth round-trip to land the `yt_session` cookie on the **web-next**
domain (so the edge middleware sees it), FastAPI's
`YTFACTORY_AUTH_REDIRECT_URI` must point at the web-next URL:

```bash
gcloud run services update ytfactory-web \
  --project=ytfactory-prod-v2 --region=asia-southeast1 \
  --update-env-vars=YTFACTORY_AUTH_REDIRECT_URI=https://ytfactory-web-next-7hwnzw7lya-as.a.run.app/api/auth/google/callback
```

The OAuth Web client
(`283470729204-bu6kpl9bim33shlh2elh1qmhj3hbdoft`) must have that URL in its
authorized redirect URIs (already added 2026-05-10).
