# Per-service Cloud Run runtime SAs (audit S1.21)

Pre-fix every Cloud Run service in the cluster ran as the single
`tts-runner@` SA. Least-privilege violation: a compromised
image-flux2-klein container walks away with full write access to the
TTS bucket, the editing agent's Firestore data, the web-server's
secret-mount permissions, etc.

Post-fix each service category gets its own SA. This doc enumerates
the mapping + the role grants each SA needs.

## Mapping

| SA                       | Cloud Run services                              | Notes |
| ------------------------ | ----------------------------------------------- | ----- |
| `tts-runner`             | tts-chatterbox, tts-cosyvoice, tts-f5,           | name kept; existing role bindings still apply |
|                          | tts-higgs, tts-indicf5, tts-indicparler           | |
| `image-runner`           | image-flux2-klein, image-hidream, image-qwen,    | needs read on the model-weights bucket |
|                          | image-z-image-turbo                               | |
| `render-runner`          | render-worker-v2, editing-agent                   | needs write on render-output bucket + Firestore `jobs/*` |
| `web-runner`             | web-server, clone-video-worker                    | needs Secret Manager read + Cloud Run jobs.run.invoker |
| `web-next-runner`        | web-next                                          | pre-existing; static-asset Next.js shell |
| `weights-runner`         | weights-staging                                   | one-shot init; HF_HOME bucket write |
| `cobalt-runner`          | cobalt-api                                        | yt-dlp proxy; needs network egress only |
| `stats-refresh-runner`   | stats-refresh (Cloud Run JOB)                     | YouTube Data API key + GCS write |

## Provisioning workflow

```bash
# 1. Create the SAs (idempotent).
bash cloud/iam/create_per_service_sas.sh

# 2. Grant OTel telemetry roles to every per-service SA.
bash cloud/iam/grant_per_service_telemetry.sh

# 3. Grant per-service-specific roles (see "Roles per SA" below).
#    These are NOT yet scripted — operator runs each `gcloud projects
#    add-iam-policy-binding` manually because the role list per SA
#    is small (1-3 grants) and the prod state needs eyeballs.
#
# 3b. (2026-05-13 add-on) Grant the deploying user `roles/iam.serviceAccountUser`
#     on each new SA — without this, `gcloud run jobs deploy` fails with
#     `Permission 'iam.serviceAccounts.actAs' denied on service account
#     <sa>@<project>.iam.gserviceaccount.com`. The error message does
#     NOT distinguish "SA doesn't exist" from "you can't actAs it"
#     so it's worth running step 1 first to rule out the former.
#
#       gcloud iam service-accounts add-iam-policy-binding \
#         render-runner@ytfactory-prod-v2.iam.gserviceaccount.com \
#         --member="user:rohittomar@docx.co.in" \
#         --role="roles/iam.serviceAccountUser" \
#         --project=ytfactory-prod-v2

# 4. Redeploy each service with bash cloud/<svc>/deploy.sh —
#    every deploy script already pins the right per-service SA
#    per audit S1.21.
```

## Provisioning state (2026-05-13)

| SA                       | Created | OTel roles | Specific roles | Notes |
|--------------------------|---------|------------|----------------|-------|
| `tts-runner`             | ✅      | ✅         | ✅ (pre-existing) | legacy SA — full role set inherited |
| `image-runner`           | ✅ 05-13 | ✅        | ⚠️ pending      | needs storage.objectViewer on weights bucket |
| `render-runner`          | ✅ 05-13 | ✅        | ✅ 05-13       | secretAccessor + datastore.user + storage.objectAdmin + run.invoker + iam.serviceAccountTokenCreator + cloudtasks.enqueuer |
| `web-runner`             | ✅ 05-13 | ✅        | ⚠️ pending      | needs secretAccessor + secretVersionAdder + datastore.user + run.invoker |
| `weights-runner`         | ✅ 05-13 | ✅        | ⚠️ pending      | needs HF_HOME bucket write |
| `cobalt-runner`          | ✅ 05-13 | ✅        | ⚠️ none needed | network-egress only; no GCS/Firestore |
| `stats-refresh-runner`   | ✅ 05-13 | ✅        | ⚠️ pending      | YouTube API key secret access + GCS write |
| `web-next-runner`        | ✅ pre-S1.21 | ✅    | ✅ (pre-existing) | static-asset Next.js shell |

## Roles per SA (beyond the universal OTel grant from step 2)

### tts-runner

```
roles/secretmanager.secretAccessor               # YTFACTORY_TOKEN, voice clones
roles/storage.objectViewer    on gs://ytfactory-model-weights-v2
roles/storage.objectAdmin     on gs://ytfactory-tts-output       # if present
```

### image-runner

```
roles/storage.objectViewer    on gs://ytfactory-model-weights-v2
roles/secretmanager.secretAccessor               # rare; HF_TOKEN if used
```

### render-runner

```
roles/secretmanager.secretAccessor               # YOUTUBE_*, ANTHROPIC_API_KEY,
                                                  # CARTESIA_API_KEY, etc
roles/datastore.user                              # Firestore jobs/* + render state
roles/storage.objectAdmin    on gs://ytfactory-state-v2
roles/run.invoker                                 # callouts to cloud/* TTS+image
roles/cloudtasks.enqueuer                         # render queue
```

### web-runner

```
roles/secretmanager.secretAccessor               # OAuth client secret, session secret,
                                                  # admin pin, every API key
roles/secretmanager.secretVersionAdder            # OAuth refresh-token writeback (T1.20)
roles/datastore.user                              # Firestore jobs/* + scheduler state
roles/storage.objectAdmin    on gs://ytfactory-state-v2
roles/run.invoker                                 # callouts to render-worker-v2 + cloud/*
roles/cloudtasks.enqueuer                         # /api/agent/lease task creation
```

### weights-runner

```
roles/storage.objectAdmin    on gs://ytfactory-model-weights-v2
roles/secretmanager.secretAccessor               # HF_TOKEN
```

### cobalt-runner

```
# (none — needs no GCP-side resources, just network egress)
```

### stats-refresh-runner

```
roles/secretmanager.secretAccessor               # YOUTUBE_API_KEY
roles/storage.objectAdmin    on gs://ytfactory-state-v2  # for data/research/youtube/
```

## Verification

```bash
# Show every project-level binding that points at a per-service SA:
for sa in tts-runner image-runner render-runner web-runner \
         weights-runner cobalt-runner stats-refresh-runner web-next-runner; do
  echo "=== ${sa} ==="
  gcloud projects get-iam-policy ytfactory-prod-v2 \
    --filter="bindings.members:serviceAccount:${sa}@*" \
    --format='table(bindings.role)'
done
```

## Regression test

`tests/test_cloud_deploy_hardening.py::TestServiceAccountIsolation`
asserts that the SA category each `cloud/<svc>/deploy.sh` pins
matches the documented mapping above. If a new service is added,
either:

  1. Add it to the mapping in this doc + the test's expected dict, OR
  2. Pin one of the existing per-service SAs that fits its needs.

A free-for-all `tts-runner@` for a non-TTS service is now a test
failure.
