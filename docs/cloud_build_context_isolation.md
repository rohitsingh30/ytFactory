# Cloud Build context isolation for parallel deploys

**TL;DR:** every `cloud/<service>/deploy.sh` should ship a
service-scoped `.gcloudignore` and pass `--ignore-file=...` to
`gcloud builds submit`. Without it, two services that touch the same
source tree race each other when deployed in parallel — the one whose
build context tarball walks into the other's mid-build artifacts
crashes with `FileNotFoundError`. Even when neither needs the other's
files.

## What broke (2026-05-11)

While shipping the dashboard fixes, I kicked off `cloud/web-server/deploy.sh`
and `cloud/web-next/deploy.sh` in parallel (different services,
different directories — should be independent). web-next ran
`npm run build` which deletes-and-rewrites `web-next/.next/` from
scratch. web-server ran `gcloud builds submit .` which tar-gzipped
the **whole repo** including `web-next/.next/`. The walks collided:

```
ERROR: gcloud crashed (FileNotFoundError):
[Errno 2] No such file or directory: 'web-next/.next/images-manifest.json'
RuntimeError: lost gzip_file
```

`web-server` deploy crashed outright. Worse, `web-next` deploy
"succeeded" (Cloud Build returned SUCCESS, Cloud Run revision said
`status.conditions[0].status = True`) but the resulting image had a
**hollow `.next/` directory** — the runtime container immediately
exited with `Could not find a production build in the '.next'
directory`. Service went 503. The only way to know was to curl the
URL post-deploy (see `feedback_cloud_run_post_deploy_curl_gate.md`).

## Why it happened

Both services use the same source root. `gcloud builds submit .` from
the repo root respects the root `.gcloudignore`, which had no exclusion
for `web-next/.next/` (the file is part of the deliverable for
web-next, so it can't be in the root ignore). The web-server
Dockerfile only `COPY`s `web/`, `pipeline/`, `control/`,
`requirements-control.txt` — it doesn't need anything in `web-next/`,
but the build context still uploads everything not in `.gcloudignore`.

Result: web-server uploads ~150 MiB of web-next state it doesn't use,
and that 150 MiB is exactly the state web-next is rewriting in
parallel.

## The fix

Per-service ignore files scope each service's build context to what it
actually needs:

### `cloud/web-server/.gcloudignore`

Mirrors the root `.gcloudignore` patterns AND adds `web-server`-only
exclusions on top:

```
# (root .gcloudignore patterns: __pycache__/, *.pyc, .git/, node_modules/,
#  data/_bench/, footage/, …)

# Cloud-web-server-only additions:
# The FastAPI image's Dockerfile only COPYs web/, pipeline/, control/,
# requirements-control.txt and a tiny channel-assets subdir. web-next/
# (the Next.js build output) is unrelated and ~150 MiB. Exclude wholesale.
web-next/
```

### `cloud/web-server/deploy.sh`

```bash
gcloud builds submit . \
  --config=cloud/web-server/cloudbuild.yaml \
  --ignore-file=cloud/web-server/.gcloudignore \    # <-- the gate
  --substitutions="_IMAGE=${IMAGE}" \
  --project="${PROJECT}" \
  --timeout=1800s
```

After the fix the web-server upload drops from ~3,000 files to ~1,500
(verified via `gcloud meta list-files-for-upload`). Web-next can run
in parallel — the two builds never touch each other's source paths.

## Generalisation

Every service in `cloud/<service>/` should have:

1. A scoped `.gcloudignore` listing the root patterns +
   service-specific additions for sibling-service paths it doesn't
   need.
2. `gcloud builds submit ... --ignore-file=cloud/<service>/.gcloudignore`
   in the deploy.sh.

The audit recipe to find services missing this:

```bash
for d in cloud/*/; do
  svc=$(basename "$d")
  ds="$d/deploy.sh"
  [[ -f "$ds" ]] || continue
  has_ignore=$(grep -c "\-\-ignore-file" "$ds" || echo 0)
  has_file=$([[ -f "$d/.gcloudignore" ]] && echo yes || echo no)
  echo "$svc: --ignore-file=$has_ignore  scoped-file=$has_file"
done
```

As of 2026-05-11 only `cloud/web-server/` has both. The others either
upload the whole repo (cloud/render-worker-v2/, cloud/clone-video-worker/)
or use `gcloud builds submit . --tag=...` without a config
(cloud/image-{hidream,qwen}/) which is harder to scope. Migrate as
they hit the same race; not retrofitting all of them today.

## Audit recipe for sibling services that should adopt this

```bash
# Services whose Dockerfile doesn't COPY web-next/ but currently
# upload it on every build — prime candidates for a scoped ignore file:
for f in cloud/*/Dockerfile; do
  if ! grep -q "web-next" "$f"; then
    echo "$(basename "$(dirname "$f")"): no web-next/ COPY — can exclude"
  fi
done

# Result on 2026-05-11: clone-video-worker, render-worker-v2,
# stats-refresh, web-server (already done), all GPU services. All
# would benefit from scoped ignore files; web-server is the first
# instance because it's the most-deployed.
```

## Don't conflate this with the host-prebuild rule

`docs/cloudrun_web_next_prebuild.md` covers a different problem
(Linux Cloud Build produces broken HTML for Next.js prerender). That
doc keeps the host build pattern; this doc adds parallel-safety on
top. Both apply: host-prebuild for correctness, scoped ignore for
parallel safety.

## Tests

There's no automated test for this — the failure mode is a
build-context race, not a code bug. The audit recipes above are the
manual gates. If a third service ever hits the same race, escalate to
a CLASS-OF-BUG with a "all cloud/* deploys must use scoped ignore"
rule; today's evidence is one instance.

## Cross-references

- Memory: `feedback_cloud_build_parallel_race.md`
- Adjacent: `feedback_cloudrun_web_next_prebuild.md` (host-prebuild
  rule for Next.js build correctness — different concern, same family)
- Adjacent: `feedback_cloud_run_post_deploy_curl_gate.md` (the
  curl-after-deploy gate that caught the hollow-`.next/` revision)
- Files touched: `cloud/web-server/.gcloudignore`,
  `cloud/web-server/deploy.sh`
- Commit: `dab8c38 fix(dashboard): cloud routes 404 + favicon +
  niche-by-key 404 noise`
