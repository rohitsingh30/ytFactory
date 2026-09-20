# ytFactory on Nikamma

Target URL: `https://ytfactory.nikamma.in` (verify live HTTP responses after deployment).
Next.js and FastAPI run on Nikamma; render jobs, GPU services, Firestore,
Secret Manager and GCS remain in `ytfactory-prod-v3`.

## Frontend-only deployment

The website can be deployed without Google credentials or a cluster login.
GitHub write access to Nikamma is sufficient: push valid manifests to `main`
and ArgoCD deploys automatically. For a first frontend-only release:

```sh
python3 cloud/nikamma/stage.py --frontend-only \
  --nikamma-checkout /path/to/nikamma \
  --web-image ghcr.io/rohitsingh30/ytfactory-web@sha256:REAL_DIGEST
```

This creates only the frontend deployment, service, ingress, monitoring, and
network policy plus namespace and ArgoCD Application. It needs no secrets,
API, or storage. Validate/render, review, commit, and push these resources.
The frontend uses `YTFACTORY_FRONTEND_ONLY=1`: the public website works,
Studio access stays protected, and login explains that the API is pending.
Check `/`, a referenced JS asset, `/login`, and `/healthz` after reconciliation.
For an existing release, change only its image digest. Add the full API and
secrets later without deleting the frontend or any existing persistent data.

## Build and verify

`.github/workflows/nikamma-images.yml` builds the frontend on macOS, packages
Linux/amd64 containers, tests authentication and startup, and publishes
`ghcr.io/rohitsingh30/ytfactory-web` and `ytfactory-api` with the source SHA.
Read the two job summaries for immutable image digests. It does not alter
the live cluster. Publishing only one image is not a deployable release.

For local checks, run the frontend build with
`YTFACTORY_API_BASE=http://ytfactory-api:8080 npm run build` in `web-next`,
then build the existing `cloud/web-next/Dockerfile` and
`cloud/web-server/Dockerfile` from the repo root. Run
`python3 cloud/nikamma/smoke_image.py web IMAGE` and its `api` equivalent.
The API image needs Python 3.12, not the macOS system Python.

## Required access and configuration

- Write access to `rohitsingh30/ytFactory` and `mukul-mehta/nikamma`.
- For the full API deployment, the cluster's Sealed Secrets public certificate
  (or correctly sealed secrets supplied by its operator). A kubectl/ArgoCD
  connection helps inspect rollout status but is not needed for a GitOps push.
- A Google runtime service identity authorized for the existing project.
  Check Firestore read/write, the two GCS buckets, required Secret Manager
  secrets, Cloud Run job execution with overrides, service invocation, and
  the read APIs used by the Cloud/telemetry screens. Grant scoped roles,
  never project Owner just to make deployment work. Verify job execution
  permissions without launching an unsolicited paid render.
- A Google OAuth web client with the additional redirect URI
  `https://ytfactory.nikamma.in/api/auth/google/callback`. Preserve existing
  redirect URIs so the old Cloud Run site remains usable during migration.
- If using YouTube account connection, also register its actual
  `/api/oauth/callback` redirect with the relevant OAuth client.
- Preserve the existing operator allowlist unless the user asks to change it.
- GHCR packages must be pullable from Nikamma. New packages default to private;
  either make these application images public (the source repo is already
  public) or add a scoped, sealed registry pull secret to both deployments
  including the API init container. Verify an anonymous pull if choosing public.

Prepare three **SealedSecret** documents, all bound to namespace `ytfactory`:

| Secret | Keys / contents |
|---|---|
| `ytfactory-session` | `YTFACTORY_SESSION_SECRET`, same strong random value used by both services |
| `ytfactory-api-secrets` | `YTFACTORY_WEB_OAUTH_CLIENT`, `YTFACTORY_AGENT_TOKEN`; copy the verified API runtime settings for Azure OpenAI, YouTube and `CLOUDRUN_*_URL` here too |
| `ytfactory-google-credentials` | `credentials.json`: dedicated service-account credential used by Google ADC and the ID-token SDK |

Keep plaintext inputs outside tracked paths; seal locally using `kubeseal`
and the actual cluster certificate. Never copy ciphertext from another
namespace and rename it: strict Sealed Secrets binding prevents decryption.
The frontend receives only the session secret. It must never receive the
Google credential, API key, OAuth client secret, or agent token.

This initial credential path supports a service-account JSON file. For
keyless Workload Identity Federation, additionally configure the Kubernetes
issuer/provider, projected token, ADC config, and an impersonated ID-token
provider; simply mounting a federation JSON file is insufficient for the
current `fetch_id_token` helper. Do not mount a personal gcloud login in pods.

Firebase public build settings (`NEXT_PUBLIC_FIREBASE_*`) are required for
the optional critique chat feature; set them in the frontend build job
from the verified Firebase app configuration. They are not secrets.

## First deployment

1. Read Nikamma's current `AGENTS.md`. Verify image pulls, runtime credentials,
   OAuth redirects, available CPU/RAM/storage, and that the URL is unclaimed.
2. After both image jobs pass, stage the release with real digests and sealed
   secrets:

   ```sh
   python3 cloud/nikamma/stage.py \
     --nikamma-checkout /path/to/nikamma \
     --web-image ghcr.io/rohitsingh30/ytfactory-web@sha256:REAL_DIGEST \
     --api-image ghcr.io/rohitsingh30/ytfactory-api@sha256:REAL_DIGEST \
     --sealed-secrets /secure/path/ytfactory-sealed.yaml
   ```

3. Run `kubectl kustomize /path/to/nikamma/apps/ytfactory`, validate resource
   schemas, and review the exact diff. The staging script rejects missing
   secrets, plaintext Secret payloads, and mutable image tags. Its checks
   cannot prove the cluster can decrypt the sealed output.
4. Commit only the app, namespace, and ArgoCD Application in Nikamma. Push
   when deployment is authorized; ArgoCD reconciles automatically. No
   `kubectl apply`, cluster-wide RBAC changes, or new tunnel is needed.
5. Verify ArgoCD Synced/Healthy, both deployments available, PVC bound, and
   secrets decrypted. Check public `/`, `/login`, `/healthz`, anonymous
   `/api/jobs` (401) and `/app` (redirect to login). Complete a real Google
   login and approved-account API read before claiming the app is functional.

The API volume persists local uploads/cache and seeds baked channel assets.
It uses Recreate for its single RWO volume, so API updates have a brief
interruption. Only the frontend has public and LAN ingress; its proxy reaches
the internal API. Never set `K_SERVICE` on Nikamma: it bypasses agent checks
under the assumption that Google IAM has already authenticated the request.
`YTFACTORY_REQUIRE_AUTH=1` keeps the API closed even if OAuth is misconfigured.

## Subsequent deployments and rollback

Build and verify new images, update the two digest entries in
`apps/ytfactory/kustomization.yaml`, review, and push to Nikamma. ArgoCD
deploys the new version at the same URL. Preserve secret resources and PVCs.
Rollback by restoring the previous pair of known-good digests, not by deleting
the application or volume. A failed image build leaves the running app alone.

Do not retire the existing Cloud Run website/API or change Cloud Scheduler
targets as part of this initial deployment. Those require a separately
verified cutover; otherwise existing automation could lose access.
