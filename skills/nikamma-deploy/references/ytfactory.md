# ytFactory deployment

Source: `https://github.com/rohitsingh30/ytFactory`.
Deployment repo: `https://github.com/mukul-mehta/nikamma`.
Hostname: `ytfactory.nikamma.in` (verify live state, not just this file).

Read `cloud/nikamma/README.md` from the current source checkout for exact
commands and credential requirements. The reusable implementation is:

- `.github/workflows/nikamma-images.yml`: macOS Next.js build (existing
  workaround for malformed Linux prerender output), Linux/amd64 container
  build, auth tests, startup checks, then GHCR publish.
- `cloud/nikamma/manifests/`: template resources. These are intentionally
  incomplete until real images and sealed secrets are supplied.
- `cloud/nikamma/stage.py`: validates digest-pinned images and three named
  SealedSecrets and stages the first release in Nikamma. Requires PyYAML.
  It never commits/pushes; rejects existing app directories to avoid drift.
  `--frontend-only --web-image IMAGE@sha256:DIGEST` stages only the website,
  with no Google credentials, session secret, API deployment, or PVC needed.
- `cloud/nikamma/smoke_image.py`: exercises health and anonymous access in
  a disposable Docker container; does not prove Google credentials/login.

Services: `ytfactory-web` (Next.js), `ytfactory-api` (FastAPI), both port 8080.
Only the frontend is exposed; `YTFACTORY_API_BASE=http://ytfactory-api:8080`
and `YTFACTORY_API_AUTH_MODE=passthrough` connect it to the API. This mode
passes the caller's bearer; it never adds an agent credential to an anonymous
request. Browser approval is enforced in the API using Firestore.

Set `YTFACTORY_REQUIRE_AUTH=1` on API, `YT_AUTH_ENABLED=1` on frontend,
and use the same stable session secret on both. Canonical host and Google
OAuth callback must agree. Do not set `K_SERVICE` on the homelab: several
routes interpret it as trusted Google IAM and skip agent authentication.

Keep render jobs, GPU services, Firestore, GCS and Secret Manager on GCP.
The current code's off-cloud ID token path accepts a mounted service-account
JSON credential; federation needs additional impersonated ID-token support.
Do not imply a personal GitHub login grants GCP access. Do not move/delete
the existing cloud services, change scheduling, or trigger a paid render
as a website smoke test.

The API uses a 5Gi RWO volume for local state/cache, with an init container
seeding channel assets. API updates use Recreate and briefly interrupt it.
The frontend uses rolling updates. Subsequent releases update both image
digests in Nikamma's `apps/ytfactory/kustomization.yaml`.

For a frontend-only release, keep `YT_AUTH_ENABLED=1` and set
`YTFACTORY_FRONTEND_ONLY=1`. Leave session secrets unset: the Studio remains
protected. `/login` explains that the API is not connected, `/api/*` returns
503, and `/healthz` reports frontend-only mode. Upgrade to the full app by
adding the API resources and secrets and removing the frontend-only flag.
Do not overwrite an existing app or delete its resources during that upgrade.

GitHub write access is sufficient to publish valid Nikamma manifests; ArgoCD
automatically deploys them. Cluster credentials are not required to push.
Google access is needed for the cloud-connected API, not for the frontend.
Verify the public homepage and assets after pushing before reporting success.
