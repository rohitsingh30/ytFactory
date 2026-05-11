# Cloud Run startup probes — TCP probe lies for lazy-init apps

**Established 2026-05-11** after `ytfactory-web-next` revision 00018
deployed cleanly, passed Cloud Run's startup probe, then 503'd on the
very first request because `next start` only checks for a valid build
on the first incoming HTTP request, not at process start.

## The default-probe pitfall

Cloud Run's default startup probe is a **TCP** probe against the
container port (8080 by default). It succeeds the moment the listener
opens. For any app whose validity check runs after the listener opens,
the probe is a lie:

- **Next.js (`next start`)** — `setupFsCheck` reads
  `.next/BUILD_ID` and `.next/required-server-files.json` only on
  the first HTTP request, not at startup. A broken `.next/` (missing
  files, wrong CSS hash, etc.) sails past the TCP probe.
- **FastAPI / uvicorn with lazy DB connection** — Postgres pool, Redis
  pool, Firestore client are typically constructed at startup but
  CONNECTED on first use. A misconfigured DSN passes the TCP probe.
- **Anything that does `RUN_AT_FIRST_REQUEST = True`** — graphql
  schemas, ORM lazy imports, Cloud Storage clients with bad creds.

The 2026-05-11 web-next outage hid behind exactly this gap: the image
was missing 99% of its `.next/` files (a separate
`cloud/web-next/deploy.sh` race — see
[`cloudrun_web_next_prebuild.md`](./cloudrun_web_next_prebuild.md) §
"What broke (2026-05-11) — concurrent-deploy race"), but Cloud Run
reported the revision as `Ready: True` and started serving 503s.

## The rule

**Every Cloud Run service whose entrypoint can crash on the FIRST
request (not at startup) MUST configure an HTTP startup probe pointing
at a real readiness endpoint.** TCP-probe defaults are acceptable
ONLY for services that fail at startup or have no first-request
validity check.

Decision matrix:

| service kind                                    | probe required           |
|-------------------------------------------------|--------------------------|
| Next.js `next start`                            | HTTP `/healthz` or `/`   |
| FastAPI/uvicorn with on-demand DB/storage init  | HTTP `/healthz`          |
| Worker JOB (no HTTP listener)                   | n/a — no startup probe   |
| Service that crashes at `import` if misconfigured | TCP probe is fine        |

## How to configure the HTTP startup probe

In `gcloud run deploy`:

```bash
gcloud run deploy <SERVICE> \
  --image=<IMAGE> \
  --region=asia-southeast1 --project=ytfactory-prod-v2 \
  --port=8080 \
  --use-http2=false \
  --startup-probe="httpGet.path=/healthz,timeoutSeconds=3,periodSeconds=5,failureThreshold=10,initialDelaySeconds=2" \
  ...
```

Knob guide:

- **`httpGet.path`** — must hit a route that exercises the same code
  path the first user request would. Reading `.next/BUILD_ID`,
  pinging the DB pool, etc.
- **`timeoutSeconds=3`** — short. The probe runs every period; the
  first slow probe shouldn't stall the whole deploy.
- **`periodSeconds=5`** — every 5 s.
- **`failureThreshold=10`** — gives the container 50 s of warm-up
  before Cloud Run gives up and rolls back. Bump for image services
  with cold-load weights (FLUX2-klein, Z-Image-Turbo: try 60×).
- **`initialDelaySeconds=2`** — small head-start so the container can
  bind the port.

## How to add the rule to an existing service

Audit current probe state:

```bash
gcloud run services describe <SERVICE> \
  --region=asia-southeast1 --project=ytfactory-prod-v2 \
  --format='yaml(spec.template.spec.containers[].startupProbe)'
```

If the field is empty, the service is on the default TCP probe. Sweep
for siblings:

```bash
for s in $(gcloud run services list --project=ytfactory-prod-v2 \
              --region=asia-southeast1 --format='value(name)'); do
  has=$(gcloud run services describe "$s" --region=asia-southeast1 \
          --project=ytfactory-prod-v2 \
          --format='value(spec.template.spec.containers[].startupProbe)' 2>/dev/null)
  [[ -z "$has" ]] && echo "  TCP-default: $s"
done
```

Every name in the output is a candidate for the same gotcha if it has
a first-request validity check.

## What about readiness probes / liveness probes?

Cloud Run only supports a **startup** probe; readiness + liveness are
Kubernetes-shaped and don't apply. The startup probe runs only at
container start; once it passes, Cloud Run hands traffic over and
doesn't re-probe until the next deploy.

This means: **if your service degrades AFTER the startup probe passes
(e.g. DB pool exhausted at 10 min uptime), Cloud Run will keep
sending it traffic.** The right fix is a sidecar healthcheck cron
hitting `/healthz` from outside the service, OR a min-instances=0
config so Cloud Run replaces the instance on every cold-start. Don't
try to bend Cloud Run's startup probe into a liveness probe.

## Anti-patterns observed

- **Pointing the HTTP startup probe at a static route that doesn't
  touch the broken-on-first-request code path.** A `/healthz` that
  returns `{"ok": true}` from a global handler tells you the listener
  is up and routing works — same signal as TCP. Make it actually
  exercise the validity check (read a manifest, ping the DB, etc.).
- **Bumping `failureThreshold` to mask a slow startup instead of
  fixing it.** 50 s is the budget for normal deps; 5+ min means the
  container is doing work that should happen at build time
  (downloading model weights, compiling templates, etc.). See
  `docs/cloudrun_image.md` for the FLUX2-klein cold-load pattern —
  it's deliberately slow because GPU weight load is irreducible, but
  most services don't have that excuse.
- **Forgetting to update the probe path when the route changes.**
  Rename `/healthz` → `/api/health` and the probe silently still
  points at `/healthz` → 404 → probe fails → deploy rolls back. Make
  the probe path live in the same place as the route definition; for
  ytfactory-web that's `web/server.py` (single source of truth).

## Reference

- `cloud/web-next/deploy.sh` — TODO: add `--startup-probe httpGet.path=/`
  (Next.js home page exercises `setupFsCheck` on first request).
- `cloud/web-server/deploy.sh` — TODO: add `--startup-probe
  httpGet.path=/healthz` (FastAPI handler that runs the upload-records
  scan, exercising GCS auth + Firestore connectivity).
- Memory pointer: `feedback_cloudrun_tcp_probe_lies.md`
- Related: `cloudrun_web_next_prebuild.md` § "What broke (2026-05-11)"
  — the incident that surfaced this rule.
