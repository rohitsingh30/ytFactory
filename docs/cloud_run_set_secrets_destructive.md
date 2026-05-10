# Cloud Run `--set-secrets` is destructive (cross-channel rule)

**Established 2026-05-10** during the YouTube-stats-permanent-fix work,
after the third redeploy of `ytfactory-web` blanked the dashboard
cards. Root cause: `gcloud run services update --set-secrets="..."`
**replaces** the entire secret-mount list. So if a developer adds a
runtime secret via `--update-secrets` (the surgical add-one form), but
never amends the `--set-secrets` line in `cloud/<service>/deploy.sh`,
the next `bash deploy.sh` run silently drops the addition and prod
goes back to broken.

## The rule

- **`--set-secrets` is destructive — `--update-secrets` is additive.**
- Every secret a Cloud Run service depends on at runtime MUST be in
  the service's `cloud/<service>/deploy.sh` `--set-secrets` line. No
  exceptions.
- When a session adds a runtime secret via `gcloud run services
  update --update-secrets=…`, the SAME session MUST amend the
  matching `deploy.sh` `--set-secrets` line and commit. Otherwise the
  next deploy regresses prod.
- Cloud Run **JOBs** use `--set-secrets` identically — same rule
  applies to `cloud/<job>/deploy.sh`.

## Concrete history

The bug fired during the 2026-05-10 prod fix:

1. We discovered prod had no `YOUTUBE_API_KEY` env var — dashboard
   cards were blank.
2. Fixed by `gcloud run services update ytfactory-web
   --update-secrets=YOUTUBE_API_KEY=youtube-api-key:latest`.
3. Cards lit up. Worked.
4. Then we shipped the rest of the code via `bash
   cloud/web-server/deploy.sh`. Cards went blank again because the
   script's `--set-secrets` didn't include `YOUTUBE_API_KEY`.
5. Re-ran `--update-secrets` to restore. Cards lit up again.
6. **Now amended `cloud/web-server/deploy.sh`** to include the key
   permanently (and added the `youtube-channel-ids` mount discovered
   in the same fix pass).

Without writing this rule down and the audit recipe below, the next
session that adds a secret will trip the same wire.

## Audit recipe — find every Cloud Run service whose deploy.sh might
## be drifting from the live mount list

```bash
for svc in ytfactory-web ytfactory-render-worker-v2 ytfactory-stats-refresh \
           ytfactory-clone-video-worker ; do
  region=asia-southeast1
  project=ytfactory-prod-v2
  echo "=== $svc ==="
  echo "  live mounts (gcloud):"
  gcloud run services describe "$svc" --project=$project --region=$region \
    --format='value(spec.template.spec.containers[0].env)' 2>/dev/null \
    | tr ',' '\n' | grep -i secret | head
  echo "  deploy.sh declared mounts:"
  grep -h "set-secrets" cloud/*/deploy.sh 2>/dev/null \
    | grep -i "$svc\|.*$svc" | head
done
```

A clean state: every secret in the "live mounts" output appears in
the matching `cloud/<service>/deploy.sh` `--set-secrets` line.

## Cross-references

- All `cloud/*/deploy.sh` files are governed by this rule.
- `docs/youtube_stats_refresh.md` — the work that surfaced it.
- `docs/cloud_service_dep_playbook.md` — Cloud-Run service-creation
  playbook; this rule is now cited there too.
- Memory: `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_cloud_run_set_secrets_destructive.md`
