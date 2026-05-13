# Cloud deploy script safety — silent failures kill operator trust

> **Cross-channel rule.** Established 2026-05-13 after `cloud/web-next/deploy.sh`
> failed silently three times in a row during the burner-system rip,
> appearing to "succeed with exit 0" while in fact the build never
> ran.

## Two recurring trap classes

### Trap 1 — Build output suppression masks pre-build failures

`cloud/web-next/deploy.sh` runs:

```bash
(cd web-next && npm run build >/dev/null)
```

`npm run build` in this project triggers `prebuild` → `npm run typecheck`
→ `tsc --noEmit`. If TypeScript errors exist (as they did for `middleware.ts`
missing `async` on a function using `await`, AND for `tests/api.test.ts`
having pre-existing strictness errors), `tsc` exits non-zero and the
subshell exits non-zero. `set -euo pipefail` SHOULD propagate that,
and it does — the script exits.

But the operator wrapping with `bash deploy.sh 2>&1 | tee /tmp/log | tail -5`
sees exit code 0 (the `tail` exit code is what bubbles up without
`set -o pipefail` on the OUTER shell), and only "==> Pre-building
.next/ on host" in the captured log. The build error never surfaces.

**Fix (short-term, in the script):** drop the `>/dev/null` redirect
on any `npm run build` / `gcloud builds submit` / `gcloud run deploy`
call. Cloud-build output is verbose; ship it to a log file in
`work_dir` instead so failures are still visible:

```bash
# OLD — output hidden, caller sees only "==> Pre-building" then silence
(cd web-next && npm run build >/dev/null)

# NEW — output streamed; if you want it terse, file-redirect with `tee`
(cd web-next && npm run build) 2>&1 | tee /tmp/web-next-prebuild.log
```

**Fix (operator-side):** ALWAYS wrap deploy invocations with
`set -o pipefail` so a non-zero exit ANYWHERE in the pipe surfaces:

```bash
set -o pipefail
bash cloud/web-next/deploy.sh 2>&1 | tee /tmp/deploy.log
echo "deploy exit code: $?"
```

### Trap 2 — Hardcoded service-account references that don't exist in IAM

Multiple `cloud/<svc>/deploy.sh` scripts hardcode a per-service runtime
SA:

```bash
--service-account="web-next-runner@${PROJECT}.iam.gserviceaccount.com"
```

If that SA was never created in IAM (or was deleted), `gcloud run deploy`
fails with:

```
ERROR: PERMISSION_DENIED: Permission 'iam.serviceaccounts.actAs' denied
on service account web-next-runner@ytfactory-prod-v2.iam.gserviceaccount.com
(or it may not exist).
```

The "(or it may not exist)" disclaimer is the actual problem — gcloud
can't tell the operator whether the SA is missing or whether they just
lack `actAs`. Both cases need different fixes.

**Fix (short-term, in the script):** describe the existing service
first; if it exists, use whatever SA it was deployed with. The
deploy.sh comment block already documents how to create the SA but
doesn't actually verify it exists before invoking deploy:

```bash
# Preflight: discover the SA the live service uses, fall back to
# the documented per-service runner SA only if no service exists yet.
LIVE_SA=$(gcloud run services describe "${SERVICE}" \
    --project="${PROJECT}" --region="${REGION}" \
    --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null \
    || true)
if [[ -n "${LIVE_SA}" ]]; then
    RUNTIME_SA="${LIVE_SA}"
    echo "==> Using live SA: ${RUNTIME_SA}"
else
    RUNTIME_SA="web-next-runner@${PROJECT}.iam.gserviceaccount.com"
    echo "==> First deploy — using documented per-service SA: ${RUNTIME_SA}"
    # Verify it exists before passing to gcloud run deploy
    if ! gcloud iam service-accounts describe "${RUNTIME_SA}" \
        --project="${PROJECT}" >/dev/null 2>&1; then
        echo "ERROR: Service account ${RUNTIME_SA} does not exist."
        echo "       Create it first:"
        echo "         gcloud iam service-accounts create web-next-runner \\"
        echo "           --project=${PROJECT}"
        exit 1
    fi
fi

gcloud run deploy "${SERVICE}" \
    --service-account="${RUNTIME_SA}" \
    ...
```

**Fix (operator-side):** when this fires, check whether the SA actually
exists:

```bash
gcloud iam service-accounts describe \
  web-next-runner@ytfactory-prod-v2.iam.gserviceaccount.com \
  --project=ytfactory-prod-v2

# If "NOT_FOUND": create it.
# If "Permission denied": it exists but you lack ResourceManager.
# If the SA exists but you can't actAs it: grant
#   roles/iam.serviceAccountUser on the SA itself.
```

The 2026-05-13 burner-rip workaround was to deploy with the default
compute SA (`283470729204-compute@developer.gserviceaccount.com`),
which is what the live service had been running with — the
`web-next-runner@` SA in deploy.sh was aspirational, never created.

## Sweep recipe

For every deploy.sh in the repo, verify the per-service SA actually
exists:

```bash
for d in cloud/*/deploy.sh; do
  sa=$(grep -E "^\s*--service-account=" "$d" 2>/dev/null \
       | head -1 \
       | sed -E 's/.*service-account="?([^"\\\s]+).*/\1/' \
       | sed 's|\${PROJECT}|ytfactory-prod-v2|g')
  if [[ -n "$sa" && "$sa" != *'${'* ]]; then
    if ! gcloud iam service-accounts describe "$sa" \
         --project=ytfactory-prod-v2 >/dev/null 2>&1; then
      echo "MISSING: $d → $sa"
    fi
  fi
done
```

Every "MISSING" line is a deploy that will fail next time someone
runs it. Either create the SA (preferred — keeps per-service IAM
isolation) or fall back to the default compute SA inline.

## Related docs

- `docs/iam_per_service.md` — the per-service SA scheme this trap
  exposes.
- `docs/deploy.md` — top-level deploy walkthrough; cross-link here.
- `docs/cloud_service_dep_playbook.md` — adding a new Cloud Run service.

## Memory

`feedback_cloud_deploy_silent_failures.md` (cross-link).
