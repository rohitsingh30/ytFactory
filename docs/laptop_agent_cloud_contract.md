# Laptop agent ↔ cloud control-plane contract (2026-05-11)

> **TL;DR:** the agent and the cloud share a `caps` enum, an
> `AckRequest` enum, and a fire-and-forget ACK semantics. **All three
> are silent failure surfaces** — a typo or a WIP cap added on the
> laptop before the cloud rolls out leaves the lease loop wedged
> with no surface error. This doc captures the three flavours of
> drift we hit on 2026-05-11 and the three mitigations now in code.

The laptop agent (`pipeline/laptop_agent.py`) long-polls
`POST /agent/lease`, executes Chrome-bound work locally, and ACKs
back via `POST /agent/ack/{id}`. The cloud side
(`control/routes/agent_routes.py`) validates every request body
through `control.core.schema` (Pydantic). When the laptop ships a
shape the cloud doesn't accept, the request 422s, the agent retries
forever, and **nothing else gets leased** — playwright_upload tasks
queue up too. The user sees "Subscribe-all enqueued 50 tasks" and
no Chrome opens for any of them.

## Drift flavour 1 — TaskKind cap drift

**Symptom:** every `/agent/lease` returns 422 with
``Input should be 'render_short', 'script', ..., 'burner_engage' or 'noop'``.
The agent retries every 5 s, never claiming any task.

**Root cause (2026-05-11):** the laptop's `CAPS` list grew a
`burner_create` entry for an in-flight feature; the cloud's
`TaskKind` enum hadn't been redeployed with the new value, so
Pydantic rejected the entire lease request body — even though the
other two caps (`playwright_upload`, `burner_engage`) were valid.
Pydantic enum validation is all-or-nothing on a list field.

> **2026-05-11 ext.** This in-flight cap landed as `create_burner`
> (renamed from the hypothetical `burner_create`). The
> `BulkActions` UI on `/app/burner-channels` enqueues `CREATE_BURNER`
> tasks via `POST /api/burner_channels/create_bulk` (commit
> `e4bcaae`). The pre-deploy ritual below was followed: `TaskKind`
> bump shipped in `ytfactory-web` first, then the `CAPS` extension
> in the laptop_agent commit. Current cap roster:
> `["playwright_upload", "burner_engage", "create_burner"]`.

**Fix:** `_claim_task` now parses the 422 body, finds the offending
cap (`detail[*].loc == ["body","caps",N]`, `detail[*].input ==
"<cap>"`), removes it from the in-process `CAPS` list, and retries
on the next tick. The pruned cap stays out for the agent's lifetime
(self-heals on launchd restart once the cloud catches up).

**Detection on prod:**

```bash
gcloud run services logs read ytfactory-web \
  --project=ytfactory-prod-v2 --region=asia-southeast1 --limit=200 \
  | grep "/agent/lease HTTP/1.1\" 422"
```

A burst of 422s on `/agent/lease` in the minute after a laptop deploy
is the canonical signature.

**Code:**
- Pruning: `pipeline/laptop_agent.py::_claim_task` 422 handler.
- Test: `tests/test_laptop_agent.py::TestClaimTask::test_422_prunes_unknown_cap_from_caps`.
- Schema: `control/core/schema.py::TaskKind`.

**Pre-deploy ritual:** when adding a new cap on the laptop side,
ALSO add it to `control/core/schema.py::TaskKind` AND deploy
`ytfactory-web` BEFORE the laptop. The auto-prune is a backstop, not
the workflow.

## Drift flavour 2 — AckRequest status enum drift

**Symptom:** every `/agent/ack/{id}` for a failed task returns 422.
Tasks stay in `LEASED` until `lease_ttl_s` (10 min default) expires,
then re-lease and fail again. Looks like an infinite retry loop.

**Root cause (2026-05-11):** the agent sent
`{"status": "failed", ...}`. The cloud's `AckRequest` schema is
`status: Literal["ok", "error"]`. Pydantic 422'd the body; the
ACK never landed; the task stayed leased. Pre 2026-05-11 the typo
silently kept failing tasks in LEASED state and re-spawned them
every 10 min — looked like the worker was crash-looping rather than
the ACK being broken.

**Fix:** `_ack` now sends `"ok"` or `"error"`. Test pinned in
`tests/test_laptop_agent.py::TestAck::test_ack_failed`.

> **2026-05-12 correction.** The "Fix" block above was aspirational
> when first written on 2026-05-11 — the actual code in
> `pipeline/laptop_agent.py::_ack` still sent `"failed"` until commit
> `610461a` on 2026-05-12. The pinned test
> (`TestAck::test_ack_failed`) was *also* still asserting
> `args[1]["status"] == "failed"`, so it green-lit the bug rather than
> catching it. By the time we noticed, **158 stuck-LEASED
> `burner_engage` tasks had piled up** (every failure ack 422-d → task
> leaked LEASED, no ack arrived → no `failed` status set → task lingered
> until lease_expires_at then re-leased and re-failed). One-shot
> Firestore script reaped them; permanent fix is the periodic reaper
> below + the actual code/test landing in `610461a`. **Lesson** about
> "doc claims fix that isn't in code" lives in its own subsection
> below — see "Drift flavour 7 — aspirational doc claims".

**Detection on prod:**

```bash
gcloud run services logs read ytfactory-web \
  --project=ytfactory-prod-v2 --region=asia-southeast1 --limit=200 \
  | grep "/agent/ack/.*\" 422"
```

**Pre-deploy ritual:** when modifying any `LeaseRequest` /
`AckRequest` / `HeartbeatRequest` field on either side, search for
the field name in `pipeline/laptop_agent.py` and `control/routes/agent_routes.py`
to confirm both speak the same dialect.

## Drift flavour 3 — Fire-and-forget ACK on subprocess spawn

**Symptom:** the agent ACKs `ok` immediately after spawning a
worker subprocess. If the subprocess dies in 1 s with `rc=2`
(catalog empty / missing token / ADC dead), the cloud queue marks
the task DONE and moves on. The user clicks "Subscribe all" and
sees 50 happy `done` rows in the dashboard — with zero actual
work done.

**Root cause (2026-05-11):** `_exec_burner_engage` was a pure
`subprocess.Popen(start_new_session=True)` then immediate
`return (True, None, None)`. No subprocess inspection.

**Fix:** after spawn, the agent does
`proc.wait(timeout=BURNER_EARLY_EXIT_PROBE_S)` (5 s — well under
the time it takes Chrome to attach + cookies to bridge, so a
healthy worker will TimeoutExpired and the agent ACKs `ok` as
before; a dying worker will surface `rc != 0` and the agent ACKs
`error` with the log tail).

**Generalisation:** any agent task handler that spawns a subprocess
and returns immediately MUST poll the subprocess for ≥ 5 s before
ACKing `ok`. The spawn cost of any real Chrome / GPU / ML workload
is well over 5 s; that gives us a near-zero false-positive budget
to detect immediate-exit failures.

**Code:**
- `pipeline/laptop_agent.py::_exec_burner_engage` (probe).
- `pipeline/laptop_agent.py::BURNER_EARLY_EXIT_PROBE_S` (knob).
- Test: `tests/test_laptop_agent.py::TestExecute::test_burner_engage_early_exit_acks_failed`.

**Generalised rule for new task handlers:**

```python
proc = subprocess.Popen(...)
try:
    rc = proc.wait(timeout=EARLY_EXIT_PROBE_S)
except subprocess.TimeoutExpired:
    return True, None, None  # happy path: still running
if rc != 0:
    return False, None, f"<task>[{slug}] exited rc={rc} within {EARLY_EXIT_PROBE_S}s — ..."
return True, None, None  # rare clean fast-exit; warn-log it
```

## Drift flavour 4 — Single-threaded agent → bulk fan-out is hours not seconds (2026-05-12)

**Symptom:** the user clicks `/app/burner-channels` "Subscribe All",
49 burners enqueue, and the dashboard shows them draining at **~1
task per 13 minutes** instead of the seconds-per-task the fire-and-
forget Popen path implies. Bulk-create has the same shape (10
`create_burner` queued, draining one per ~13-15 min).

**Root cause (2026-05-12):** the agent's `run()` loop was strictly
serial — `lease → execute → ack → sleep`, one at a time. Each
iteration could absorb a 30-35 s lease long-poll plus a 5 s back-off
on every read timeout (frequent: 35 s urllib timeout sat right on
the cloud's 30 s long-poll deadline + Cloud Run cold-start variance).
A 49-task burst extrapolates to **~10 hours**.

**Fix:** `run()` now spawns `NUM_WORKERS` (default `5`) worker
threads, each running its own `lease → execute → ack → wait-on-child`
loop. Per-kind `BoundedSemaphore`s enforce real concurrency caps so
the parallelism doesn't burst-spawn 168 Chrome instances when the
queue is deep. Worker holds its slot for the FULL spawned-subprocess
lifetime — `proc.wait()` after the ack — which is what bounds Chrome
instance count, not just the lease-call rate. (The rubber-duck pass
caught this: parallelizing leases without holding the slot would have
drained the queue into hundreds of concurrent Chrome workers and
torched the laptop.)

Default caps:

| kind             | cap | rationale                                         |
|------------------|-----|---------------------------------------------------|
| `burner_engage`  | 4   | subscribe_only mode is ~1-3 min/burner; 4× M2 Max Chrome instances comfortable |
| `create_burner`  | 1   | parallel attaches against same Google account confuse the create flow (refusal screens, OTP prompts) |

Tunable via env (set in launchd plist or shell):

- `YTFACTORY_AGENT_WORKERS` (default 5)
- `YTFACTORY_AGENT_CAP_BURNER_ENGAGE` (default 4)
- `YTFACTORY_AGENT_CAP_CREATE_BURNER` (default 1)

**Verified live (2026-05-12 01:26:35):** restarted agent spawned 4
`burner_engage` workers + 1 `create_burner` concurrently within 7 s
(= caps), processed 6 `burner_engage` tasks in the first ~3 min vs
the prior steady state of 1 task per 13 min.

**Code:**
- `pipeline/laptop_agent.py::_worker_loop` (per-kind semaphore acquire-before-lease)
- `pipeline/laptop_agent.py::_exec_burner_engage`/`_exec_create_burner` (return `Popen` so worker can `proc.wait()`)
- `pipeline/laptop_agent.py::run` (thread launcher + heartbeat-only main thread + SIGTERM `_SHUTDOWN`)
- Tests: `tests/test_laptop_agent.py::TestWorkerLoop::*`

## Drift flavour 5 — TaskKind rename leaves zombie tasks in the queue (2026-05-12)

**Symptom:** Firestore `tasks` collection accumulated **50 queued
`burner_create` tasks** that no agent ever picked up. The agent's
caps list was `["burner_engage", "create_burner"]` (the new name);
the old `burner_create` tasks matched no agent → dead in the queue
forever, taking up `attempts` budget when re-leased by humans, and
making queue-status counts misleading.

**Root cause:** `TaskKind` was renamed (`burner_create` →
`create_burner`) on the cloud schema side, and the `BulkActions` UI
+ `/api/burner_channels/create_bulk` route were updated. But there
was no **queue migration** — the 50 in-flight `burner_create` tasks
that had been enqueued from the old code path stayed with their old
kind name. They sat in `status=queued, kind=burner_create` for
days.

**Fix on 2026-05-12:** one-shot Firestore script deleted the 50
zombies. `pipeline/laptop_agent.py::CAPS` was already on the new
name — no code change needed.

**Generalisation (rule for next rename):** when renaming any
`TaskKind` value, ALSO drain the queue:

```python
# After the rename ships:
from google.cloud import firestore
db = firestore.Client(project='ytfactory-prod-v2')
col = db.collection('tasks')
n = 0
for snap in col.where("kind", "==", "<old_name>").where("status", "==", "queued").stream():
    snap.reference.update({"kind": "<new_name>", "updated_at": _now_iso()})
    n += 1
print(f"migrated {n}")
```

(or `delete()` if the old tasks are no longer wanted). Pattern is the
same for ANY enum-typed field stored in Firestore where rename
happens on one side but live data isn't reshaped.

**Detection on prod:**

```bash
.venv/bin/python -c "
from google.cloud import firestore
from collections import Counter
db = firestore.Client(project='ytfactory-prod-v2')
sk = Counter()
for snap in db.collection('tasks').stream():
    d = snap.to_dict()
    sk[(d.get('status'), d.get('kind'))] += 1
for k, v in sorted(sk.items()): print(' ', k, v)
"
```

Any `(queued, <kind>)` whose `<kind>` doesn't match the live
`TaskKind` enum is a zombie.

## Drift flavour 6 — No periodic reap_expired() → leases pile up forever (2026-05-12)

**Symptom:** 158 stuck-LEASED `burner_engage` tasks at audit time on
2026-05-12. Each one held a lease that had long since expired
(`lease_expires_at < now`) but no caller had reset them back to
`queued` or marked them `done`.

**Root cause:** `Queue.reap_expired()` exists in
`control/core/queue.py` but **nothing called it on a schedule**. It
was wired into the test path and a manual scripts dir, but no
cron/scheduled task ran it against prod. Combined with the Drift
flavour 2 ack bug, every failure ack 422-d → no status change → task
sat in LEASED forever.

**Fix:** added `web/server.py::_periodic_queue_reaper` to the FastAPI
lifespan. Runs every `YTFACTORY_QUEUE_REAPER_INTERVAL_S` seconds
(default `300` = 5 min), calls `q.reap_expired()` via
`asyncio.to_thread` so a slow Firestore scan can't stall request
handling. Belt-and-braces guard against future agent crashes.

**Code:**
- `web/server.py::_periodic_queue_reaper` (the loop)
- `web/server.py::lifespan` (`asyncio.create_task(_periodic_queue_reaper())` + `cancel()` on shutdown)
- `control/core/queue.py::FirestoreQueue.reap_expired` (no change — was already correct)

**Detection on prod:**

```bash
gcloud run services logs read ytfactory-web \
  --project=ytfactory-prod-v2 --region=asia-southeast1 --limit=200 \
  | grep "queue-reaper"
```

A non-zero `reset N stuck-LEASED tasks back to QUEUED` line every
5 min is the canonical signature when the queue is healthy. If you
see it firing with N>0 every cycle, agents are crashing mid-task —
investigate further.

## Drift flavour 7 — Aspirational doc claims (META, 2026-05-12)

**Symptom:** `docs/laptop_agent_cloud_contract.md` (this doc) said
"Drift flavour 2 — Fix: `_ack` now sends `"ok"` or `"error"`. Test
pinned in `tests/test_laptop_agent.py::TestAck::test_ack_failed`."
Both claims were false:

1. The actual code in `pipeline/laptop_agent.py::_ack` still sent
   `"failed"` on 2026-05-12 (commit `610461a` is the real fix).
2. The pinned test asserted `args[1]["status"] == "failed"` — it
   *passed* because the test agreed with the buggy code, not because
   the bug was caught.

The doc was written as part of `/update-docs` commit `4a31e85`
on 2026-05-11. The fix it claimed was *intended* but was never
actually shipped to code or test.

**Why this is CLASS-OF-BUG, not ONE-OFF:** `/update-docs` runs are
the only durable record of "what we learned and shipped". A doc
that says "Fix: code now does X" creates the illusion of safety —
the next reader trusts it, doesn't verify, and the bug ships another
quarter. In this case the bug spent **24 hours** post-claim accruing
zombies before being noticed.

**Generalised rule (added to `.claude/skills/update-docs/SKILL.md`
self-learnings):** when a `/update-docs` finding documents a fix:

1. The save-doc MUST cite the **commit SHA** that landed the fix
   (`commit abc1234 (2026-MM-DD)`), not just "now does X".
2. The mentioned **test MUST actually fail on the buggy state**.
   Verify by reverting the production code change locally and
   running the test once — it should go red. If the test passes
   against the bug, the test asserts the bug, not the fix.
3. If the run is documenting an *intended* fix that hasn't landed
   yet (e.g. write-up before code review), explicitly use the word
   **"Planned"** instead of "Fix" so the next reader doesn't trust
   it as shipped.

**Mitigations in this commit:** Quality gate 10 added to
`.claude/skills/update-docs/SKILL.md` — "Fix-claim verifier".
Memory entry: `feedback_doc_aspirational_claims.md`.

## Diagnostic recipes

When the bulk-subscribe / bulk-create queue feels slow, **don't
read the agent log first** — query Firestore directly. The agent log
shows what the agent did; Firestore shows what the queue *contains*
(zombies, kind mismatches, ratios), which is the actually useful
signal.

```bash
.venv/bin/python -c "
import os
os.environ.setdefault('GOOGLE_CLOUD_PROJECT', 'ytfactory-prod-v2')
from google.cloud import firestore
from collections import Counter
db = firestore.Client(project='ytfactory-prod-v2')
sk = Counter()
for snap in db.collection('tasks').stream():
    d = snap.to_dict()
    sk[(d.get('status'), d.get('kind'))] += 1
for k, v in sorted(sk.items()): print(' ', k, v)
"
```

Read the output as:

| pattern                                   | meaning                                        | next step                                      |
|-------------------------------------------|------------------------------------------------|------------------------------------------------|
| `(queued, <unknown_kind>)` >> 0           | TaskKind rename without queue migration        | drain or rewrite — see Drift flavour 5         |
| `(leased, <kind>)` >> # of running agents | zombie LEASED — agents acked-error 422'd or crashed | reaper should clear; see Drift flavour 6       |
| `(queued, <known_kind>)` deep but agent log shows lease cycle ~13 min | agent serial bottleneck                        | parallelize — see Drift flavour 4              |
| `(done, ...)` count steady, queued not draining | agent isn't running OR auth broken             | `launchctl list \| grep ytfactory`; `gcloud auth print-identity-token` |

**Direct curl as a control-plane health probe** (verifies the cloud
isn't the bottleneck — if curl returns in <2 s, the bottleneck is
the laptop):

```bash
TOK=$(gcloud auth print-identity-token)
time curl -sS -X POST -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" --max-time 60 \
  -d '{"agent_id":"diag","caps":["burner_engage"],"lease_ttl_s":60}' \
  https://ytfactory-web-7hwnzw7lya-as.a.run.app/agent/lease
```

**Caveat:** this leases a real task to `agent_id=diag`. The lease
expires after 60 s (the `lease_ttl_s` you set) and `_periodic_queue_reaper`
returns it to QUEUED on its next tick. Don't run this dozens of
times in a row during a real burst — each call costs one task ~5
minutes of unavailability.

## Operator kill-switch — how to stop the agent (2026-05-12)

The agent is a `KeepAlive=true` LaunchAgent. **Killing the parent
PID alone does nothing** — launchd respawns it within `ThrottleInterval`
(10 s) and you watch the burner_engage children come back. The only
way to actually quiet the laptop is the two-step
`unload + disable` ritual; without `disable` the agent comes back at
the next login even if you reboot or close the terminal.

### Stop the agent now AND across reboots

```bash
# 1. Unload the live job (stops the parent, sends SIGTERM to children).
launchctl unload ~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist

# 2. Persistently disable so login/reboot doesn't reload it.
launchctl disable gui/$(id -u)/com.ytfactory.laptop-agent

# 3. Verify both: the job is gone from the user-domain list, AND the
#    persistent state shows "disabled".
launchctl list | grep ytfactory                          # expect: only com.ytfactory.cloud-snapshot
launchctl print-disabled gui/$(id -u) | grep ytfactory   # expect: "com.ytfactory.laptop-agent" => disabled

# 4. Sweep any orphan children that survived the unload SIGTERM. Per
#    CLAUDE.md don't pkill/killall — find PIDs and `kill <PID>` them.
ps -ef | grep -E "pipeline\.(laptop_agent|cross_engage\.(burner_engage|create_burner_channel))" | grep -v grep
ps -ef | grep "playwright/driver/node" | grep -v grep
# Then: kill <PID> <PID> ...

# 5. Final sanity: nothing burner-related left.
ps -ef | grep -E "burner_engage|create_burner_channel|laptop_agent|playwright" | grep -v grep
```

### Re-enable later

```bash
launchctl enable gui/$(id -u)/com.ytfactory.laptop-agent
launchctl load   ~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist
```

### Why both `unload` AND `disable`

`unload` is **session-scoped** — it removes the running job from
launchd's in-memory job table. The persistent `Disabled` plist key
still says `false`, so the next time the user-domain launchd seeds
itself (login, reboot) it re-loads the job and KeepAlive resumes.

`disable` writes the persistent override
(`~/Library/LaunchAgents/disabled.plist` under the hood). After it
runs, `launchctl print-disabled gui/$UID` shows the label as
`disabled` and launchd won't reload the agent on next session even
though the plist file still sits in `~/Library/LaunchAgents/`.

The `launchctl unload -w …` flag from older docs (e.g.
`docs/cloudrun_admin_panel.md` for the cloud-snapshot agent) is the
deprecated combined form — it does both in one call but only on
older macOS. Modern macOS (Catalina+) wants the explicit `enable` /
`disable` subcommands.

### What this DOESN'T do

- **Doesn't delete the plist file.** To fully remove:
  `rm ~/Library/LaunchAgents/com.ytfactory.laptop-agent.plist` after
  the disable. Most users want the disable-not-delete state because
  re-enabling later is one command, vs. re-installing the plist.
- **Doesn't touch the cloud-side queue.** Tasks already in Firestore
  with `status=queued, kind=burner_engage` will stay queued. They
  re-lease on `_periodic_queue_reaper` ticks (5 min) and pile attempts
  but never run because no agent has the cap. Drain via the recipe in
  Drift flavour 5 if the queue is already deep when you stop the
  agent.
- **Doesn't stop `com.ytfactory.cloud-snapshot`** (daily 02:00 cloud
  cost snapshot). Benign. If you want it gone:
  `launchctl disable gui/$(id -u)/com.ytfactory.cloud-snapshot`.

### Why this is in the contract doc, not just the runbook

The contract doc holds the seven drift flavours. The kill-switch
ritual belongs here too because every drift-flavour debug session
ends with "and to make it stop respawning while I investigate, do
THIS." Burying the unload/disable pair in only `docs/burner_channels.md`
(as it was pre-2026-05-12 — see "Doc sweep" at the bottom of this
doc) means anyone landing here for contract drift had to find the
stop ritual elsewhere. Co-locating cuts that hop.

### Cross-references — every doc that names "stop the agent"

This is the canonical kill-switch recipe. Every other doc that
mentions stopping the laptop agent should link here, not duplicate.
Audited 2026-05-12:

- `docs/burner_channels.md` § Failure modes table — "Chrome windows
  opening unexpectedly" row. Updated to include `launchctl disable`
  and to point at this section.
- `docs/cross_engage_cloud_v2.md` § Smoke test recipe — was the
  *restart* recipe (`unload + load`); kept as-is for restart, with a
  sidebar note pointing at this section for permanent-stop.
- `docs/cloudrun_admin_panel.md` § cloud-snapshot — different agent,
  uses the Apple-deprecated `launchctl unload -w` shorthand. Left
  unchanged; flagged here as something the next /update-docs run
  could modernise to `unload` + `disable gui/$UID/...` for
  consistency.

## Where each finding lives

| flavour                         | doc                                          | memory                                          | code                                                  |
|---------------------------------|----------------------------------------------|-------------------------------------------------|-------------------------------------------------------|
| TaskKind cap drift              | this doc                                     | `feedback_laptop_agent_cloud_contract.md`       | `pipeline/laptop_agent.py::_claim_task`               |
| AckRequest enum                 | this doc                                     | `feedback_laptop_agent_cloud_contract.md`       | `pipeline/laptop_agent.py::_ack` (real fix `610461a`) |
| Subprocess liveness             | this doc + `docs/burner_channels.md`         | `feedback_laptop_agent_cloud_contract.md`       | `pipeline/laptop_agent.py::_exec_burner_engage`       |
| Single-threaded bottleneck      | this doc                                     | `feedback_laptop_agent_cloud_contract.md`       | `pipeline/laptop_agent.py::_worker_loop` + `run`      |
| TaskKind zombie tasks           | this doc                                     | `feedback_taskkind_rename_queue_zombies.md`     | one-shot Firestore script (no permanent code)         |
| No periodic reaper              | this doc                                     | `feedback_laptop_agent_cloud_contract.md`       | `web/server.py::_periodic_queue_reaper`               |
| Aspirational doc claims (META)  | this doc + `.claude/skills/update-docs/SKILL.md` | `feedback_doc_aspirational_claims.md`           | (none — process change)                               |
| Operator kill-switch (2026-05-12) | this doc § "Operator kill-switch"          | `feedback_laptop_agent_cloud_contract.md` (2026-05-12 ext) | (none — operator runbook only) |

## See also

- `docs/burner_channels.md` — concrete: how the burner-engage worker
  uses the new agent-authed cloud endpoints to bypass laptop ADC.
- `docs/cloudrun_render_worker.md` — the cloud-side worker that
  consumes the same lease/ack protocol.
- `docs/data_flows.md` § "Laptop agent lease loop" — sequence
  diagram of the protocol.
