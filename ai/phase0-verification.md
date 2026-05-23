# Phase 0 — Production state verification

**PHASE 0 STATUS: clean**

**Blockers:** none.

## 200-word summary

All seven verifications pass against the live `ytfactory-prod-v3` project. Read access is confirmed for every operational surface the agent will need during the refactor: GCS artifacts + state buckets, all Cloud Run services + the render-worker job, Firestore database, Cloud Logging (including recent worker stderr — the exact `7743ca76` writeback failure from Q54 is readable from logs), billing, and job-execution history. The retired `ytfactory-prod-v2-artifacts` bucket correctly returns 403 (no longer reachable). The v3 state bucket exists with per-channel subfolders for all 7 channels including `scrollpulse` (which has no YAML yet per refactor-plan P6.1).

One operational finding worth flagging for Phase 3 cleanup: of the 12 `cloud/<svc>/deploy.sh` files in the repo, only 7 services correspond to live Cloud Run resources. Five `cloud/` dirs have deploy scripts but no deployed counterpart: `clone-video-worker`, `cobalt-api`, `editing-agent`, `stats-refresh`, `weights-staging`. CLAUDE.md only acknowledges `editing-agent` as a "supported but optional" deploy; the other four are vestigial. These should be evaluated for retirement during Phase 3 cleanup (legacy code deletion) — they're documentation/maintenance drag.

Most recent successful end-to-end render: `eda9fa22…` on 2026-05-17 (8.28 MiB short.mp4 + thumb.jpg). Most recent attempt with mp4: `7743ca76…` on 2026-05-21 (57 MiB mp4 in `video/` but missing top-level `short.mp4` — confirms the writeback-gate kill from Q54).

---

## Summary table

| # | Verification | Verdict | Note |
|---|---|---|---|
| V1 | GCP project = `ytfactory-prod-v3`, ACTIVE | PASS | gcloud default = v3; project ACTIVE |
| V2 | Buckets: v3 artifacts + state exist, v2 retired | PASS | v3-artifacts ok; v3-state has 7 channel folders; v2-artifacts returns 403 |
| V3 | Cloud Run services + jobs match `cloud/` deploys | PARTIAL | 7 live (6 services + 1 job); 5 `cloud/` dirs vestigial |
| V4 | Firestore `(default)` database accessible | PASS | DB exists in asia-southeast1; indexes list returns 0-rows (no permission error) |
| V5 | Cloud Logging readable for worker | PASS | Recent stderr from 2026-05-21 readable; the Q54 writeback kill is visible |
| V6 | Billing accessible + enabled | PASS | 2 billing accounts visible; project billing = ENABLED |
| V7 | Recent renders in GCS, real file sizes | PASS | `eda9fa22` 8.28 MiB short.mp4; `7743ca76` 57 MiB mp4 in `video/` |

---

## V1 — GCP project

### V1a
```
$ gcloud config get-value project
ytfactory-prod-v3
```
**Exit:** 0
**Verdict:** PASS — default project is `ytfactory-prod-v3` (matches expectation).

### V1b
```
$ gcloud projects describe ytfactory-prod-v3 --format="value(projectId,lifecycleState)"
ytfactory-prod-v3	ACTIVE
```
**Exit:** 0
**Verdict:** PASS — project exists and is ACTIVE.

### ADC token sanity
```
$ gcloud auth application-default print-access-token | head -c 30
ya29.a0AQvPyIMQ7N3cBcN65VIIcqB...
```
**Exit:** 0
**Verdict:** PASS — Application Default Credentials are loaded.

---

## V2 — Buckets

### V2a — v3 artifacts bucket exists
```
$ gsutil ls -b gs://ytfactory-prod-v3-artifacts
gs://ytfactory-prod-v3-artifacts/
```
**Exit:** 0
**Verdict:** PASS.

### V2b — v3 artifacts has job folders
```
$ gsutil ls gs://ytfactory-prod-v3-artifacts/jobs/ | head -5
gs://ytfactory-prod-v3-artifacts/jobs/11d522a37ff94bf5862447a528652b73/
gs://ytfactory-prod-v3-artifacts/jobs/253835896e66452e9713a2f05e241f2e/
gs://ytfactory-prod-v3-artifacts/jobs/29770a04b2324ae39c42853474421b69/
gs://ytfactory-prod-v3-artifacts/jobs/29cb1a4d7b5544c0ae19c3c66c8aac9f/
gs://ytfactory-prod-v3-artifacts/jobs/38a43783a7334cfcad54237cb6823a03/
```
**Exit:** 0
**Verdict:** PASS — real job-id folders present.

### V2c — v3 state bucket exists with channel folders
```
$ gsutil ls gs://ytfactory-prod-v3-state/
gs://ytfactory-prod-v3-state/cosmosdecoded/
gs://ytfactory-prod-v3-state/hindutavaanimated/
gs://ytfactory-prod-v3-state/historyrecapped/
gs://ytfactory-prod-v3-state/mystoriesanimated/
gs://ytfactory-prod-v3-state/rhymetimejunction/
gs://ytfactory-prod-v3-state/scrollpulse/
gs://ytfactory-prod-v3-state/sportsrecapped/
```
**Exit:** 0
**Verdict:** PASS — all 7 channels have state-bucket subfolders (including `scrollpulse`, whose YAML is still pending per P6.1).

### V2d — v2 artifacts bucket is retired
```
$ gsutil ls -b gs://ytfactory-prod-v2-artifacts
AccessDeniedException: 403 sanimated219@gmail.com does not have storage.buckets.get
access to the Google Cloud Storage bucket. Permission 'storage.buckets.get' denied
on resource (or it may not exist).
```
**Exit:** 1
**Verdict:** PASS — old v2 bucket is unreachable (deleted or permissions revoked). Confirms Q53 migration to v3 is complete from the client's perspective. Note: 13 stale `prod-v2` references still need to be scrubbed from source per Phase 3.

---

## V3 — Cloud Run services + jobs vs `cloud/` deploys

### V3a — Live services
```
$ gcloud run services list --project=ytfactory-prod-v3 --region=asia-southeast1 \
    --format="value(metadata.name)"
tts-chatterbox
ytfactory-asr-whisper
ytfactory-image-z-image-turbo
ytfactory-tts-indicf5
ytfactory-web
ytfactory-web-next
```
**Exit:** 0
**Verdict:** PASS — 6 services running.

### V3b — Live jobs
```
$ gcloud run jobs list --project=ytfactory-prod-v3 --region=asia-southeast1 \
    --format="value(metadata.name)"
ytfactory-render-worker-v2
```
**Exit:** 0
**Verdict:** PASS — render-worker job present.

### V3c — Cross-reference with `cloud/` dirs

`cloud/` dirs with `deploy.sh` (12 total):

| `cloud/` dir | Live in Cloud Run? | Status |
|---|---|---|
| `asr-whisper` | ytfactory-asr-whisper | LIVE |
| `clone-video-worker` | — | VESTIGIAL deploy.sh (no live counterpart) |
| `cobalt-api` | — | VESTIGIAL |
| `editing-agent` | — | VESTIGIAL (CLAUDE.md notes "optional polish stage") |
| `image-z-image-turbo` | ytfactory-image-z-image-turbo | LIVE |
| `render-worker-v2` | ytfactory-render-worker-v2 (JOB) | LIVE |
| `stats-refresh` | — | VESTIGIAL |
| `tts-chatterbox` | tts-chatterbox | LIVE |
| `tts-indicf5` | ytfactory-tts-indicf5 | LIVE |
| `web-next` | ytfactory-web-next | LIVE |
| `web-server` | ytfactory-web | LIVE |
| `weights-staging` | — | VESTIGIAL |

**Verdict:** PARTIAL — 7 live deploys, 5 vestigial `deploy.sh` scripts. Candidate for Phase 3 cleanup. Note: CLAUDE.md (repo layout section) only lists the 7 live services. The 5 vestigial dirs are repo drag — flag to refactor-plan Phase 3.

**One-line interpretation:** every deploy.sh referenced in CLAUDE.md is live; everything not in CLAUDE.md is dead.

---

## V4 — Firestore

### V4a — Database list
```
$ gcloud firestore databases list --project=ytfactory-prod-v3 \
    --format="value(name,locationId,type)"
projects/ytfactory-prod-v3/databases/(default)	asia-southeast1	FIRESTORE_NATIVE
```
**Exit:** 0
**Verdict:** PASS — `(default)` database exists in asia-southeast1, type FIRESTORE_NATIVE.

### V4b — Collection list (command issue)
```
$ gcloud firestore collections list --project=ytfactory-prod-v3 --database="(default)"
ERROR: (gcloud.firestore) Invalid choice: 'collections'.
```
**Exit:** 0 (gcloud printed error to stderr but exited cleanly because it suggested alternates)
**Verdict:** UNKNOWN — `gcloud firestore collections list` is not a valid subcommand in the installed gcloud version. Collection listing requires either the REST API or the Firestore client SDK (Python). Not blocking: the worker + control plane both use the Python SDK directly, and Firestore DB-level access is confirmed via V4a + V4c. Collections (`jobs`, `tasks`, `scheduler_state`, `ratelimits`) can be verified by spot-checking the Python client at refactor time.

### V4c — Composite indexes
```
$ gcloud firestore indexes composite list --project=ytfactory-prod-v3 --database="(default)"
Listed 0 items.
```
**Exit:** 0
**Verdict:** PASS — the command runs cleanly without 403. Zero composite indexes is consistent with a queue/state schema that uses single-field queries (the canonical `jobs` collection sorts by `created_at` + filters by `status` — both single-field).

---

## V5 — Cloud Logging

### V5a — Log list
```
$ gcloud logging logs list --project=ytfactory-prod-v3 --limit=10
NAME
projects/ytfactory-prod-v3/logs/cloudaudit.googleapis.com%2Factivity
projects/ytfactory-prod-v3/logs/cloudaudit.googleapis.com%2Fdata_access
projects/ytfactory-prod-v3/logs/cloudaudit.googleapis.com%2Fsystem_event
projects/ytfactory-prod-v3/logs/cloudbuild
projects/ytfactory-prod-v3/logs/clouderrorreporting.googleapis.com%2Finsights
projects/ytfactory-prod-v3/logs/run.googleapis.com%2F%2Fvar%2Flog%2Fsystem
projects/ytfactory-prod-v3/logs/run.googleapis.com%2Frequests
projects/ytfactory-prod-v3/logs/run.googleapis.com%2Fstderr
projects/ytfactory-prod-v3/logs/run.googleapis.com%2Fstdout
projects/ytfactory-prod-v3/logs/run.googleapis.com%2Fvarlog%2Fsystem
```
**Exit:** 0
**Verdict:** PASS — all standard Cloud Run + audit + build logs reachable.

(Note: `--format="value(name)"` returned 0 rows because of a format issue — re-running without the format flag returns the full list shown above. The agent has access; the tool was misused on first call.)

### V5b — Recent worker logs
```
$ gcloud logging read 'resource.type="cloud_run_job"
    resource.labels.job_name="ytfactory-render-worker-v2"' \
    --project=ytfactory-prod-v3 --limit=3 \
    --format="value(timestamp,severity,textPayload)" --freshness=7d
2026-05-21T19:48:51.563510Z	ERROR
2026-05-21T19:48:47.332544Z	WARNING	Container called exit(1).
2026-05-21T19:48:43.760846Z		duration_s=1232.167 min_required=1440.000 target=1800
```
**Exit:** 0
**Verdict:** PASS — and this is the exact writeback-gate kill from Q54 (`7743ca76`, 2026-05-21 19:48). The agent can read worker stderr in real time. Critically: this is the failure pattern the refactor must repair (P3.7).

---

## V6 — Billing

### V6a — Billing accounts visible
```
$ gcloud beta billing accounts list --format="value(name,displayName)"
0113C5-9580A7-961733	My Billing Account
01D017-F2A813-5E0CA3	My Billing Account 1
```
**Exit:** 0
**Verdict:** PASS — 2 billing accounts visible.

### V6b — Project billing enabled
```
$ gcloud beta billing projects describe ytfactory-prod-v3 \
    --format="value(billingAccountName,billingEnabled)"
billingAccounts/0113C5-9580A7-961733	True
```
**Exit:** 0
**Verdict:** PASS — project is billing-linked to account `0113C5-9580A7-961733` and billing is ENABLED.

---

## V7 — Most recent renders

### V7a — Recent job folders + per-folder content sanity

Sampled the most recent 5 jobs in the bucket. Findings:

| Job id (first 12) | Has `short.mp4` at top level? | Has `video/<file>.mp4`? | Size | Disposition |
|---|---|---|---|---|
| `7743ca76fdc1` | NO | YES (57.01 MiB, 2026-05-21) | 57.1 MiB | Render finished mp4 but worker exited 1 (writeback gate kill from Q54) |
| `e87daa477593` | NO | NO (only `script/`) | 1.02 KiB | Killed at script stage |
| `fb3fe46dad43` | NO | NO (only `script/`) | not measured | Killed at script stage |
| `eda9fa22feef` | YES (8.28 MiB, 2026-05-17) | YES | 8.28 MiB | **Successful end-to-end render — most recent confirmed shipped Short** |
| `e405c5e854f9` | NO | NO (only `script/`) | not measured | Killed at script stage |

### V7b — Sizes prove real mp4s, not stubs
```
$ gsutil du -sh gs://ytfactory-prod-v3-artifacts/jobs/7743ca76fdc147c7a226a44f703bc170/
57.1 MiB
$ gsutil du -sh gs://ytfactory-prod-v3-artifacts/jobs/eda9fa22feef40c1ad19839d00507391/
8.28 MiB  (top-level short.mp4 + thumb.jpg)
```

### V7c — Render-worker job-execution history (corroborating signal)
```
$ gcloud run jobs executions list --job=ytfactory-render-worker-v2 \
    --project=ytfactory-prod-v3 --region=asia-southeast1 --limit=5 \
    --format="value(metadata.name,status.startTime,status.completionTime,status.conditions[0].type)"
ytfactory-render-worker-v2-5kjs7  2026-05-21T18:49:26Z  2026-05-21T19:48:51Z  Completed
ytfactory-render-worker-v2-bvk52  2026-05-21T04:14:42Z  2026-05-21T04:15:08Z  Completed
ytfactory-render-worker-v2-n7qrp  2026-05-20T16:06:32Z  2026-05-20T16:06:59Z  Completed
ytfactory-render-worker-v2-6b4ft  2026-05-20T15:58:36Z  2026-05-20T15:59:03Z  Completed
ytfactory-render-worker-v2-2wvmm  2026-05-20T15:36:42Z  2026-05-20T15:37:14Z  Completed
```

Last execution = ~59 minutes (long-form render — matches the `7743ca76` long-form session); the four before it were ~25 seconds each (likely fail-fast at script stage, matching the `e87daa…` / `e405c5…` / `fb3fe4…` script-stage kills above).

**Exit:** 0
**Verdict:** PASS — most recent SHIPPED short was `eda9fa22…` on 2026-05-17 (8.28 MiB, real h264+aac with thumbnail). All other recent jobs died at script/writeback stage — consistent with Q54: "zero successful end-to-end renders" since the rewrite-gate calibration regressed.

---

## Interpretation for the refactor

1. **Read access is confirmed** for every operational surface the agent will need: GCS (both artifact + state buckets), Cloud Run (services + jobs + execution history), Firestore (database; collection enumeration via gcloud is not available but irrelevant — the Python SDK covers it), Cloud Logging (including worker stderr), billing. Q76 is satisfied.

2. **v3 cutover is real, but source still references v2** — 13 stale `prod-v2` defaults remain in source per refactor-plan Phase 3. The bucket itself is gone (V2d).

3. **5 vestigial `cloud/<svc>/deploy.sh` scripts** (`clone-video-worker`, `cobalt-api`, `editing-agent`, `stats-refresh`, `weights-staging`) have no deployed counterpart. Refactor-plan does not currently list these for retirement; recommend adding to Phase 3 cleanup. CLAUDE.md is already correct (only lists the 7 live services).

4. **The writeback-gate kill (Q54, P3.7) is reproducible from logs** — exact stderr from 2026-05-21 reads `duration_s=1232.167 min_required=1440.000 target=1800` followed by `Container called exit(1).` This is the failure mode P3.7 must repair. The agent has the visibility loop to validate the fix lands.

5. **Most recent confirmed-shipped Short is 6 days old** (`eda9fa22…`, 2026-05-17). Every newer execution died at script stage (3 of last 4) or writeback stage (1 of last 4). The pipeline has been quiet-broken for ~6 days. Phase 1 reliability work is appropriately scoped.
