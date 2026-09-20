---
name: nikamma-deploy
description: Deploy or update containerized websites and APIs on the mukul-mehta/nikamma Kubernetes cluster using its GitOps repository, ArgoCD, and stable nikamma.in hostnames. Use for Nikamma deployments, deployment debugging, and rollback.
---

# Deploy to Nikamma

Nikamma is a single-node k3s homelab, not a GitHub hosting service. Application
code stays in its source repository; deployment manifests live in
`https://github.com/mukul-mehta/nikamma`. Follow the current checkout's
`AGENTS.md` and `CLUSTER.md`; inspect an existing similar application before
editing. User authorization to deploy includes the necessary scoped push and
reconciliation; a request to prepare or explain does not.

## Establish the target

- Identify the source repo, app name, hostname, existing ArgoCD Application,
  image registry, runtime dependencies, and current release. Preserve an
  existing hostname on updates. Do not claim a proposed hostname is live.
- Check GitHub identity and repository permissions. They are distinct from
  kubectl/ArgoCD permissions and any external cloud credentials.
- Keep unrelated local edits intact. Use an isolated checkout when needed,
  and follow the repository's reconciliation rules before publishing.
- Check credentials by names/permissions, without printing secret values.
  If access is missing, ask for the access method or credential file location,
  continue build/validation work, and do not publish a broken release to
  substitute for a functioning deployment.

## Build and configure

Build immutable images for the live node architecture (the documented main
node is Intel/amd64). Put image CI in the application source repo. Verify
both build success and runtime startup, then record full image digests.
ArgoCD does not build source code. Confirm that Kubernetes can pull the
images; new GHCR packages are private by default.

New apps conventionally need:

- `apps/<app>/`: deployments, ClusterIP services, ingress, probes, explicit
  resources, optional persistent volume, sealed secrets, and Gatus checks.
- `cluster/namespaces/<app>.yaml` and `argocd/apps/<app>.yaml` referencing
  `project: nikamma` and the application directory.
- `nodeSelector: {node-type: main-server}` and, when needed,
  `storageClassName: nikamma-local-config`.

Use public ingress class `traefik-public`. Add a separate `traefik-internal`
route for the same hostname when it must work on LAN/Tailscale: local DNS
resolves to the internal proxy. The documented wildcard `*.nikamma.in`
already routes through Cloudflare Tunnel to public Traefik. Verify current
configuration; don't add a second tunnel or port-forward through CGNAT.

Encrypt secrets with the target cluster's Sealed Secrets certificate, bound
to the exact namespace and Secret name. Commit only SealedSecret ciphertext.
Reuse existing secrets on updates; never create copies of unrelated apps'
credentials. Keep persistent volumes and app data through updates/rollback.

For cloud-connected apps, verify workload identity, endpoint URLs, session
keys, OAuth redirects, data stores, and service invocation permissions.
Runtime flags such as `K_SERVICE` are not generic production flags; never
set them to imitate a cloud environment or bypass application authentication.

For ytFactory specifically, read [references/ytfactory.md](references/ytfactory.md).

## Publish and verify

Validate rendered manifests (Kustomize/Helm if used), auth behavior, probes,
and image availability. Review the exact scoped diff before committing.
Pushing the watched branch is a deployment: ArgoCD automatically reconciles
it, including pruning deleted resources. Do not push incomplete manifests
with placeholder images or missing required secrets. Do not change shared
cluster infrastructure or other apps to work around a local app failure.

When authorized, commit/push the validated release and check ArgoCD
Synced/Healthy, available pods, bound storage, decrypted secrets, and the
actual public URL. Check anonymous rejection on protected endpoints and a
real authenticated API read. A 200 from the landing page is not proof the
API or login works. Distinguish built, published, deployed, and functional
in the handoff, and identify any remaining access blocker precisely.

Stop deployment retries when credentials, DNS ownership, external dependency
access, or cluster capacity requires user/operator intervention. Fix a build
failure locally before another publication. Resume from already-published
digests instead of rebuilding unchanged source.

Rollback by restoring the previous known-good image digests/config and
letting ArgoCD reconcile. Never roll back by deleting PVCs or an entire app.
Report the unchanged URL, verified release, and material limitations.
