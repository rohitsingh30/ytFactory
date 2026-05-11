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

## Where each finding lives

| flavour              | doc                                  | memory                                      | code                                    |
|----------------------|--------------------------------------|---------------------------------------------|-----------------------------------------|
| TaskKind cap drift   | this doc                             | `feedback_laptop_agent_cloud_contract.md`   | `pipeline/laptop_agent.py::_claim_task` |
| AckRequest enum      | this doc                             | `feedback_laptop_agent_cloud_contract.md`   | `pipeline/laptop_agent.py::_ack`        |
| Subprocess liveness  | this doc + `docs/burner_channels.md` | `feedback_laptop_agent_cloud_contract.md`   | `pipeline/laptop_agent.py::_exec_burner_engage` |

## See also

- `docs/burner_channels.md` — concrete: how the burner-engage worker
  uses the new agent-authed cloud endpoints to bypass laptop ADC.
- `docs/cloudrun_render_worker.md` — the cloud-side worker that
  consumes the same lease/ack protocol.
- `docs/data_flows.md` § "Laptop agent lease loop" — sequence
  diagram of the protocol.
