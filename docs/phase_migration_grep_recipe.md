# Phase migration grep recipe

**Established 2026-05-11** after Phase 4 catch-up: 8 production
deploy.sh files were still hard-coding `gs://ytfactory-model-weights`
under the retired `ytfactory-prod` project. The doc had been
updated; the live config files had not. We discovered it the first
time we forced cold-starts on every GPU service after the migration.

This doc encodes the rule so the next migration doesn't repeat it.

---

## The rule

Every Phase migration that retires SOMETHING — a GCP project, a
GCS bucket, a Cloud Run service URL, a model alias, a Secret
Manager entry, a deprecated env var name — MUST end with two gates
before declaring the migration complete:

### Gate 1 — Repo-wide grep

For every retired name:

```bash
RETIRED_NAMES=(
  "ytfactory-prod\b"
  "ytfactory-model-weights\b"
  # add any other retired identifier
)

for n in "${RETIRED_NAMES[@]}"; do
  echo "=== $n ==="
  grep -rnE "$n" cloud/ docs/ scripts/ pipeline/ control/ web/ web-next/ \
    --include='*.sh' --include='*.py' --include='*.md' \
    --include='*.yaml' --include='Dockerfile' \
    --exclude-dir=__pycache__ --exclude-dir=.next \
    --exclude-dir=_bench --exclude-dir=deploy_logs
done
```

For every hit, classify and patch:

| state of the hit | action |
|---|---|
| `cloud/<svc>/deploy.sh` / `Dockerfile` / `server.py` / `entrypoint.py` (live config) | **PATCH** — service won't roll forward without it |
| `pipeline/<module>.py` referencing the old name in code | **PATCH** |
| `docs/<topic>.md` describing CURRENT behaviour | **PATCH** |
| `docs/<topic>.md` describing HISTORY (migration doc itself) | leave + add `**2026-MM-DD update:**` banner if not already present |
| `scripts/<one-shot>.py` that was used during the migration | **DELETE** if migration is one-shot complete; mark `# RETIRED` if kept for archaeology |
| `cloud/_bench/` copy diverging from prod | sync OR delete the bench copy |

### Gate 2 — Cold-start probe

After patching, force a fresh revision on at least one canary
service that uses the affected resource. **Warm revisions can mask
mount / IAM / billing failures** — see
`feedback_warm_revisions_mask_infra.md`.

```bash
# Trigger a fresh revision via a no-op label change.
gcloud run services update <canary-svc> \
  --region=asia-southeast1 --project=ytfactory-prod-v2 \
  --update-labels=migration-cold-start=$(date +%s)

# Wait ~3 min, then verify latestReady == latestCreated == serving.
gcloud run services describe <canary-svc> \
  --region=asia-southeast1 --project=ytfactory-prod-v2 \
  --format='value(status.traffic[0].revisionName,
                  status.latestCreatedRevisionName,
                  status.latestReadyRevisionName)'
```

If `latestCreated != latestReady` for >5 min, the migration is
incomplete — debug the cold-start error before declaring done.

## Concrete recipes by retired-name kind

### Retired GCP project name (e.g. `ytfactory-prod` → `ytfactory-prod-v2`)

```bash
grep -rn 'ytfactory-prod[^-v]\|projects/ytfactory-prod\b' \
  --include='*.{sh,py,md,yaml}' --include='Dockerfile' .
```

`[^-v]` excludes the new name `ytfactory-prod-v2`.

### Retired GCS bucket

```bash
grep -rn 'gs://<old-bucket>\b\|bucket=<old-bucket>\|BUCKET="<old-bucket>"\|/buckets/<old-bucket>\b' \
  --include='*.{sh,py,md,yaml}' --include='Dockerfile' .
```

### Retired Cloud Run service URL

```bash
grep -rn '<old-service>-<projectnum>\..*run\.app\|<old-service>.*7hwnzw7lya' \
  --include='*.{sh,py,md,yaml,ts,tsx}' .
```

### Retired model alias / TTS provider

```bash
grep -rn '"<old-provider>"\|tts_provider:.*<old-provider>\|image_provider:.*<old-provider>' \
  --include='*.{sh,py,md,yaml}' .
```

## Why this rule exists

Phase 4 (2026-05-09 / 2026-05-10) consolidated `ytfactory-prod` and
`ytfactory-control` into `ytfactory-prod-v2` + `ytfactory-web`. The
migration doc (`docs/full_cloud_cutover_2026_05_09.md`) was
updated. `cloud/cloudrun_persistent_weights.md` was updated.
Memory entry `feedback_cross_account_gcs_egress_billing_closed.md`
captured the gsutil egress side. **But the 8 production deploy.sh
files kept the old bucket name** — and warm revisions hid it for
24+ hours until today's OTel rollout forced cold-starts.

The doc said the migration was done. The cold-start said it
wasn't.

## Auto-invoke this rule when

- Any commit message contains `Phase`, `cutover`, `migration`,
  `retire`, `deprecate` together with a config file change.
- A new entry is added to `docs/full_cloud_cutover_*.md`.
- Any GCP resource (project / bucket / SA / service) is deleted
  via the console.

## Related

- **Memory:** `feedback_phase_migration_repo_grep.md` (this rule's
  agent-side mirror).
- **Memory:** `feedback_warm_revisions_mask_infra.md` (the
  cold-start probe rationale).
- **Memory:** `feedback_cross_account_gcs_egress_billing_closed.md`
  (the egress-side manifestation from the same Phase 4).
- **Doc:** `docs/full_cloud_cutover_2026_05_09.md` (the canonical
  Phase 4 reference).
- **Commit (today's catch-up):** `ffafae4`.
