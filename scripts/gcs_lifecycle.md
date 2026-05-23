# GCS lifecycle policy notes — `gcs_lifecycle.json`

This file lives next to `gcs_lifecycle.json` because GCS lifecycle
JSON is strict (rejects unknown top-level keys), so we can't inline
comments. Apply the policy via:

```bash
gsutil lifecycle set scripts/gcs_lifecycle.json gs://ytfactory-prod-v3-artifacts
```

## Rule order matters

GCS picks the FIRST matching rule whose condition fires; if multiple
rules match the same object, the youngest matching age wins.

## Audit D3.25 fix (2026-05-12)

Pre-fix, rule 4 was:

```json
{
  "action": { "type": "Delete" },
  "condition": {
    "age": 1,
    "matchesPrefix": ["jobs/"],
    "matchesSuffix": [".png"]
  }
}
```

…which shadowed rule 2's `/thumb.png` 30-day retention because the
age-1 condition ALWAYS fires before the age-30 one. Result: every
job's `thumb.png` was deleted on day 1, breaking the dashboard's
render-history thumbnail column with stale 404s.

Fix: scope rule 4 to `/frames/.png` so it only catches intermediate
per-frame PNGs the render-worker writes under `jobs/<id>/frames/`,
not the single `jobs/<id>/thumb.png` at the job root.

If a future render-worker writes intermediate PNGs to a different
path, update rule 4's `matchesSuffix` accordingly.

## Per-rule rationale

| age | suffix(es) | why |
|----:|------------|-----|
| 7d  | `/short.mp4` | The rendered video; uploaded to YouTube within hours, then redundant. |
| 30d | `/thumb.png`, `/proposal.json` | Thumbnail powers the dashboard render-history; proposal.json is the chat record. Keep for a month so users can review past renders. |
| 1d  | `/voice.wav`, `/captions.srt`, `/prompts.json`, `/script.json`, `/cast.json` | Per-render intermediate inputs; rebuildable from the proposal. |
| 1d  | `/frames/.png` | Intermediate render frames; only useful during the render itself. |
