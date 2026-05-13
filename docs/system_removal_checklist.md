# System-removal checklist

> **Cross-channel rule.** Established 2026-05-13 after the
> burner-channel system rip exercised every category below. Without
> this checklist, removals strand state in 5+ places and the next
> render half-revives whatever was supposedly retired.

When you delete a feature or system that crossed multiple layers
(pipeline + control + frontend + cloud + launchd), the tree-side `rm`
is necessary but not sufficient. The full removal must touch every
column below.

| Layer                      | What to delete                                                    | Verify with                                                                    |
|----------------------------|-------------------------------------------------------------------|--------------------------------------------------------------------------------|
| **pipeline modules**       | every `pipeline/<sys>/` file the feature owns                     | `grep -rn "from pipeline.<sys>" --include='*.py'` returns 0                    |
| **YAML / config**          | per-feature manifests in `pipeline/`                              | `git status --short` shows `D pipeline/<sys>.yaml`                             |
| **control routes**         | `control/routes/<sys>_routes.py`                                  | `grep -n "<sys>_router" web/server.py` returns 0                               |
| **schema enums**           | `TaskKind.<SYS>_*` in `control/core/schema.py`                    | inspection                                                                     |
| **frontend page**          | `web-next/app/app/<sys>/`                                         | `ls web-next/app/app/<sys>/` returns "No such file"                            |
| **frontend client**        | `<sys>Api` block in `web-next/lib/api.ts`                         | `grep -n "<sys>Api" web-next/lib/api.ts` returns 0                             |
| **frontend types**         | `<Sys>*` interfaces in `web-next/lib/types.ts`                    | `grep -n "<Sys>" web-next/lib/types.ts` returns 0                              |
| **frontend cache keys**    | `<sys>` entry in `web-next/lib/cache-keys.ts`                     | inspection                                                                     |
| **frontend nav**           | sidebar entry + any unused icon imports                           | `grep -n "/<sys>" web-next/components/nav/sidebar.tsx` returns 0               |
| **tests**                  | every `tests/test_*<sys>*.py`; surgical edit of shared tests      | `pytest --collect-only -q 2>&1 \| tail -3` clean                              |
| **launchd (laptop)**       | `bootout` + `disable` + `rm` the `~/Library/LaunchAgents/<id>.plist` | `launchctl print gui/$(id -u)/<id>` returns "Could not find service"           |
| **process sweep (laptop)** | `kill <PID>` for any in-flight workers                            | `ps -ef \| grep <module>` empty                                                |
| **local state dirs**       | `data/<sys>/`, `/tmp/<sys>_work_*`, `/tmp/<sys>_*.{log,err,stop}` | `ls data/ /tmp \| grep <sys>` empty                                            |
| **local config / tokens**  | `~/.config/ytfactory/<sys>_*.json`, OAuth tokens for retired identities | `ls ~/.config/ytfactory/ \| grep <sys>` empty                                  |
| **Firestore tasks**        | every doc in `tasks/` (and any other collection) with the kinds   | Python script — see "Firestore cleanup" below                                  |
| **GCS state blobs**        | `gs://<bucket>/<sys>/**`                                          | `gcloud storage ls gs://<bucket>/<sys>/` returns "No such object"              |
| **Secret Manager**         | per-feature secrets (`<feature>-key`, `<sys>-token-*`)            | `gcloud secrets list --filter="name:<sys>"` returns empty                      |
| **IAM bindings**           | run-invoker / secretAccessor bindings for retired SAs             | normally garbage-collected when the SA itself is deleted; spot-check           |
| **scheduler jobs**         | Cloud Scheduler jobs that triggered the feature                   | `gcloud scheduler jobs list --filter="name:<sys>"` returns empty               |
| **Cloud Run services**     | retired `<sys>-worker` services                                   | `gcloud run services list --filter="metadata.name:<sys>"` returns empty        |
| **Cloud Run jobs**         | retired `<sys>-job` jobs                                          | `gcloud run jobs list --filter="metadata.name:<sys>"` returns empty            |
| **docs (project-side)**    | every `docs/<sys>*.md` AND every cross-link FROM other docs       | `grep -rn "<sys>" docs/ \| grep -v <new-knowledge-doc>` returns 0              |
| **inline comments**        | references to deleted modules in surviving code's docstrings      | `grep -rn "<sys>\|<deleted-module>" pipeline/ control/ web/ cloud/`            |
| **memory entries**         | mark deprecated, don't delete (preserve history)                  | `MEMORY.md` updated with one ✗ DEPRECATED line per retired pointer             |
| **CLAUDE.md**              | references in the durable-rules file                              | `grep -n "<sys>" CLAUDE.md` returns 0 (or only intentional history blocks)     |
| **architecture.html**      | KPI counts, SVG nodes, NODE_INFO entries, `<details>` blocks      | inspection                                                                     |
| **redeploy**               | every Cloud Run service whose source code changed                 | `gcloud run services describe <svc> --format='value(...latestReadyRevisionName)'` shows the new revision |

## Firestore cleanup recipe

```python
"""Delete every doc in `tasks/` (and any other queue collection) whose kind
is in the retired set. Run from a venv with google-cloud-firestore installed."""
from google.cloud import firestore
fs = firestore.Client(project='ytfactory-prod-v2')

RETIRED_KINDS = {'<kind1>', '<kind2>'}

deleted = 0
for kind in RETIRED_KINDS:
    docs = fs.collection('tasks').where('kind', '==', kind).stream()
    batch = fs.batch()
    n = 0
    for doc in docs:
        batch.delete(doc.reference)
        n += 1
        if n % 400 == 0:        # Firestore batch cap is 500
            batch.commit()
            batch = fs.batch()
    if n % 400 != 0:
        batch.commit()
    deleted += n
    print(f"  {kind}: {n} docs deleted")
print(f"Total: {deleted}")
```

## GCS cleanup recipe

```bash
gcloud storage rm -r "gs://${BUCKET}/<sys>/" 2>&1 | tail -3
gcloud storage ls "gs://${BUCKET}/" | grep -i "<sys>"   # verify empty
```

## Secret Manager cleanup recipe

```bash
for s in $(gcloud secrets list --project="${PROJECT}" \
              --filter="name:<sys>" --format="value(name)"); do
  gcloud secrets delete "$s" --project="${PROJECT}" --quiet
done
```

## Launchd cleanup recipe (modern macOS)

```bash
U=$(id -u)
launchctl bootout gui/$U/com.ytfactory.<sys>-agent
launchctl disable gui/$U/com.ytfactory.<sys>-agent
rm -fv ~/Library/LaunchAgents/com.ytfactory.<sys>-agent.plist
# Sweep any orphaned children
ps -ef | grep -E "<module>" | grep -v grep | awk '{print $2}' | xargs -I {} kill {}
```

## Verification at the end

After all of the above, two final checks:

1. **`pytest --collect-only -q`** — proves no surviving test imports a
   deleted module. Should report the same total minus the deleted
   tests, with no errors.
2. **Redeploy every Cloud Run service whose code changed.** The deploy
   itself is the strongest verification — if any import was missed,
   the container will crash at startup and the deploy will fail.
   `gcloud run services describe <svc> --format='value(status.latestReadyRevisionName)'`
   should show the new revision number, and the Cloud Run console
   should show "100% traffic" on it.
3. **`grep -rn "<sys>" pipeline/ control/ web/ web-next/ tests/ cloud/ docs/`**
   — final residue sweep. Anything left should be either intentional
   history (a "Removed:" tombstone comment, a deprecated memory pointer)
   or a sweep miss to fix.

## Memory entries: deprecate, don't delete

Per CLAUDE.md, memory entries are immutable knowledge. When a system is
retired:

- Add ONE new memory file `feedback_<sys>_retired_<date>.md` describing
  what was removed and pointing at any surviving knowledge doc.
- Update `MEMORY.md` to add ONE new index line for the retirement, AND
  optionally annotate prior `<sys>`-related entries with `(DEPRECATED
  YYYY-MM-DD — see X)`.
- Do NOT delete the prior memory files. They're history; some future
  agent may need to understand "why was X tried in the first place"
  before re-implementing something similar.

## Related docs

- `docs/laptop_nuclear_cleanup_2026_05_09.md` — historical example
  (the 2026-05-09 channel-data nuke; smaller scope, same pattern).
- `docs/chrome_signed_in_automation.md` — knowledge captured BEFORE
  the burner-rip so the engineering value didn't go with the code.
- The CLAUDE.md "Persistence rule" — every deletion that surfaces a
  durable pattern goes through this checklist + dual-saves the
  surviving knowledge.

## Memory

`feedback_system_removal_checklist.md` (cross-link).
