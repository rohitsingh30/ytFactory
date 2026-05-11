# Critique chat — pipeline-fix conversations from the render-detail page

> **What this is:** the user opens a finished render in the website,
> types a critique like *"the closer is robotic, fix the TTS prosody
> module"*, and a real-time chat begins between them and a Claude /
> Copilot agent running ON THEIR LAPTOP. The agent reads the mp4 +
> the render's artifacts + the relevant pipeline code, makes the
> fix, adds tests, runs hard gates, and pushes direct to `main` on
> green. (Built 2026-05-11 in response to "I have critiques for the
> video … give me options to communicate to our orchestrator so it
> can make pipeline level changes".)

## Why laptop-side?

The agent needs:

- Repo write access (it edits `pipeline/`, `<channel>/learnings/`).
- A configured `git` + `gh` so it can `push` and (optionally) PR.
- A Claude / Copilot CLI with sign-in already done.
- Enough disk + CPU to download the mp4 + run the full pytest suite
  on every iteration.

The cloud render-worker can do none of that without granting it write
access to GitHub from a Cloud Run container — a security regression we
deliberately avoid. Inverse of the render flow: cloud queues, laptop
consumes.

## Transport: Firestore real-time

Both ends use the Firestore real-time listener API instead of polling
through `ytfactory-web`. Browser uses `firebase/firestore`'s
`onSnapshot`; laptop uses `google-cloud-firestore`'s
`DocumentReference.on_snapshot`. Sub-500 ms feel both ways, and the
Cloud Run web service is OUT of the per-message hot path — it only
sees two requests per critique session (start + token-mint).

## Doc layout

### `critiques/<critique_id>` (top-level doc)

```jsonc
{
  "critique_id": "ckxnh…",        // = doc id, also slug
  "job_id": "f3947ff48ac24c…",    // the render this critique is about
  "channel": "mystoriesanimated", // copy of job's channel for client display
  "agent": "claude",              // "claude" | "copilot"
  "status": "queued",             // queued | in_progress | done | failed | abandoned
  "claimed_by": null,             // hostname of laptop that picked it up
  "claimed_at": null,             // server timestamp
  "created_by": "rohittomar@docx.co.in",
  "created_at": <serverTimestamp>,
  "updated_at": <serverTimestamp>,
  "mp4_uri": "gs://ytfactory-prod-v2-artifacts/jobs/<job_id>/short.mp4",
  "script_uri": "gs://…/script.json",  // null for from-chat renders
  "summary": null,                // final agent summary on done
  "commit_sha": null,             // populated after gate-pass + push
  "gate_results": null,           // { coverage, pytest, lint } summary
  "error": null                   // populated on failed
}
```

### `critiques/<critique_id>/messages/<message_id>` (subcollection)

```jsonc
{
  "role": "user",                 // "user" | "agent" | "system"
  "text": "the closer is robotic, fix the TTS prosody…",
  "ts": <serverTimestamp>,
  // Agent-only optional fields — render as chips in chat:
  "action": null,                 // "file_read" | "file_edited" |
                                  // "test_added" | "gate_running" |
                                  // "gate_passed" | "gate_failed" |
                                  // "commit_created" | "push_pending" |
                                  // "pushed" | null (= plain text)
  "action_data": null             // { "path": "pipeline/tts/cloudrun.py",
                                  //   "diff_stat": "+12 -3", … }
}
```

Subcollection (not array on parent doc) so:

- Each new message is one Firestore doc-write — atomic, real-time
  delivery to every other listener with no read-modify-write race.
- Pagination comes for free if a critique runs long.
- Browser can render with a tiny `useEffect` + onSnapshot loop.

## Status state machine

```
                        ┌───────────────────────────────────────┐
                        │                                       │
   browser /critique  ┌─▼────────┐     laptop runner ┌─────────┐│
   /start         ───▶│ queued   ├────claims ───────▶│in_progr.││
                      └──────────┘                   └────┬────┘│
                                                          │     │
                            ┌─────────────────────────────┤     │
                            │                             │     │
                            ▼ all gates green             ▼ any │
                      ┌──────────┐                   ┌─────fail-┐
                      │  done    │                   │ failed   │
                      │ pushed   │                   │ no push  │
                      └──────────┘                   └────┬─────┘
                                                          │
                                                          ▼
                                  user retries by sending another
                                  message → status flips back to
                                  in_progress; gates run again.
```

`abandoned` is the runner-side reaper: any `in_progress` doc whose
`claimed_at` is older than 30 min and whose runner process is dead
(no heartbeat) gets moved back to `queued` so another runner can
claim it. (Not in Phase 1 — single-laptop assumption.)

## Cloud endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/jobs/<job_id>/critique/start` | Create the parent critique doc. Returns `{critique_id, agent, mp4_uri}`. Idempotent — if a critique for this job already exists in `queued` or `in_progress`, returns the existing id. |
| `POST` | `/api/jobs/<job_id>/critique/token` | Mint a Firebase Auth custom token from the user's `yt_session` cookie. Browser uses it with `signInWithCustomToken` so direct Firestore reads are auth'd as the same uid that the rules check on `created_by`. |

Both endpoints sit behind the existing Google-OAuth session middleware
in `web/server.py:auth_middleware`. The custom-token endpoint signs the
token with the runtime SA's private key via the IAM signBlob API
(same plumbing the new `signed_url` IAM fallback uses — see
`control/core/storage.py`).

## Hard gates (runner-side, never trusted to the agent)

The agent's "I'm done" claim is meaningless. The runner re-runs every
gate independently before pushing.

| Gate | Tool | Pass criterion |
|---|---|---|
| Diff exists | `git diff --stat HEAD` | At least one .py / .yaml file changed. |
| Tests added | `git diff --name-only HEAD --diff-filter=AM` | ≥ 1 file matching `tests/**`. |
| Diff coverage | `coverage run -m pytest …` + `coverage json` | Every new/modified executable line in non-test files is covered. |
| Full pytest | `pytest -x -q` (no slow markers) | Exit 0. |
| (Optional) lint | `ruff check`, `mypy` | Exit 0 if configured. |

Gate failures stream back into chat as `action="gate_failed"` so the
agent sees them and can iterate. Three consecutive gate failures →
`status="failed"`, runner releases the lock.

## Agent prompt skeleton

```
You are the ytFactory critique-fix agent. The user gave a critique
about a finished YouTube Short. Your job is to find the
class-of-bug fix in the pipeline (NOT a one-off content tweak),
add tests, verify hard gates, and push to main.

Repo root: /Users/rohit/ytFactory
Job context:
  - channel:   {channel}
  - mp4:       {local_mp4_path}
  - script:    {local_script_path}
  - cast:      {local_cast_path}
  - shotlist:  {local_shotlist_path}
  - learnings: <channel>/learnings/

Conversation so far:
  {message_history}

Latest user message:
  {latest_message}

Hard rules:
  1. Make the fix in pipeline/ or <channel>/learnings/ — NOT in
     the per-render artifacts.
  2. Add at least one test that would have caught the bug.
  3. Run `pytest -x -q` and confirm green BEFORE saying "done".
  4. Run `coverage` to confirm 100% diff-coverage on touched lines.
  5. Don't commit yourself — the runner does that on green gates.
  6. End each turn with a structured summary block:
     ```json
     {"type":"agent_summary","action":"done|need_more_info",
      "files_changed":["..."], "tests_added":["..."],
      "rationale":"…", "follow_up_questions":["..."]}
     ```
```

## Phase 1 boundaries

- Single laptop runner (no multi-host claim race).
- No in-flight cancellation — user can `git revert` after the fact.
- Agent gets read-only access to mp4 (no re-render trigger from chat).
- No fancy diff-cover tool; we parse `coverage json` ourselves
  against `git diff --unified=0`.
- Browser chat panel is on the render-detail page only; no global
  inbox view yet.

## Deploying — one-time prerequisites (operator action)

Phase 1b ships the **code** for the laptop runner + browser chat
panel; the cloud + frontend deploy is gated on a one-time Firebase
provisioning step. Two five-minute tasks:

### 1. Add a Firebase web app to the GCP project

The runtime SA already holds
`roles/iam.serviceAccountTokenCreator` on itself (granted 2026-05-11
for the preview-mp4 signing fix; the same grant powers Firebase
Auth custom-token minting). What's missing is the Firebase project
overlay + a "web app" registration so the JS SDK has an apiKey to
authenticate browser reads.

```
1. Open https://console.firebase.google.com/
2. "Add project" -> select existing GCP project: ytfactory-prod-v2
3. Once it loads, gear icon -> Project settings
4. "Your apps" panel -> Add app -> Web (the </> icon)
5. App nickname: ytfactory-web-next   (skip Hosting toggle)
6. Copy the firebaseConfig block — only apiKey + authDomain
   + projectId are needed.
```

### 2. Enable Identity Toolkit + bake the public keys

```bash
gcloud services enable identitytoolkit.googleapis.com \
  --project=ytfactory-prod-v2

# Then, in cloud/web-next/deploy.sh, add to the `npm run build`
# environment so they end up in the prerendered bundle:
#
#   export NEXT_PUBLIC_FIREBASE_API_KEY=<copied apiKey>
#   export NEXT_PUBLIC_FIREBASE_PROJECT_ID=ytfactory-prod-v2
#   export NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=ytfactory-prod-v2.firebaseapp.com
```

(The web app's `apiKey` is *not* a secret — Firebase API keys are
public identifiers; real auth is enforced by Firestore security
rules + the custom token's uid. We bake them into the JS bundle.)

### 3. Deploy

```bash
bash cloud/web-server/deploy.sh   # picks up critique_routes + firebase-admin
bash cloud/web-next/deploy.sh     # picks up CritiqueChatPanel + JS SDK
```

### 4. Run the laptop daemon

```bash
make critique-runner
# leave it running in a tmux pane / Activity Monitor
```

That's it — open any finished render in the dashboard, type a
critique, watch the agent fix it.
