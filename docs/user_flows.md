# User Flows

Three user types interact with ytFactory. Each has a distinct journey
through the product. Diagrams below render in any Markdown viewer that
understands Mermaid (GitHub, VS Code with the Mermaid extension, GitLab).
ASCII fallbacks for raw-text views are included.

---

## 1. Visitor — fills the form per niche (primary path)

The default flow. A first-time visitor sees the niche reel, picks a
channel, fills a small form, and gets a finished mp4.

```mermaid
sequenceDiagram
    autonumber
    actor V as Visitor
    participant UI as Browser (index.html)
    participant CR as Cloud Run /api/*
    participant FS as Firestore
    participant AG as Laptop agent
    participant MS as make_shorts.py
    participant GCS as Cloud Storage
    participant YT as YouTube

    V->>UI: opens https://...run.app/
    UI->>CR: GET /api/niches
    CR-->>UI: 4 channel cards
    V->>UI: clicks "SportsStoriesAnimated"
    UI->>UI: opens edit page (form)
    V->>UI: fills topic + notes + length, hits Generate
    UI->>CR: POST /api/render {channel, topic, ...}
    CR->>FS: create jobs/<id> (status=pending)
    CR->>FS: enqueue tasks/<id> (kind=RENDER_SHORT, status=queued)
    CR-->>UI: {job_id, task_id}

    UI->>CR: GET /api/jobs/{id} (every 3s)
    CR-->>UI: status=pending, stage=queued

    AG->>CR: POST /agent/lease (long-poll)
    CR->>FS: claim queued task
    CR-->>AG: {task, payload}
    AG->>FS: mark stage=rewrite_cast (status=rendering)
    AG->>AG: claude rewrite + cast (in-process)
    AG->>FS: mark stage=render
    AG->>MS: subprocess make_shorts.py
    MS-->>AG: mp4 in data/shorts/
    AG->>FS: mark stage=gcs_upload (status=uploading)
    AG->>GCS: upload short.mp4 + thumb
    AG->>FS: mark status=done, short_uri=gs://...
    AG->>FS: enqueue tasks/<id+1> (kind=YOUTUBE_UPLOAD)
    AG->>CR: POST /agent/ack/{task_id} ok

    UI->>CR: GET /api/jobs/{id}
    CR->>GCS: signed_url(short_uri, 10min)
    CR-->>UI: status=done, short_signed_url, youtube_url

    AG->>CR: POST /agent/lease (next poll)
    CR-->>AG: YOUTUBE_UPLOAD task
    AG->>GCS: download short.mp4
    AG->>YT: insert video
    YT-->>AG: video_id
    AG->>FS: write youtube_url onto jobs/<id>
    AG->>GCS: gc_heavy_artifacts (delete beats/, voice.wav, etc.)
    AG->>FS: enqueue RESEARCH_HANDOFF
```

ASCII summary:

```
visitor ─► reel ─► card click ─► edit form ─► Generate
                                                  │
                                                  ▼
                          /api/render ─► Firestore (job+task)
                                                  │
                                          ┌───────┴────────┐
                                          ▼                ▼
                              UI polls /api/jobs    laptop leases task
                                          │                │
                                          │     rewrite → cast → make_short
                                          │     → upload to GCS → ack DONE
                                          │                │
                                          └─────► UI sees status=done
                                                           │
                                                           ▼
                                                  YOUTUBE_UPLOAD leases
                                                           │
                                                  publishes to YT,
                                                  GCs intermediates,
                                                  hands off to research
```

### Key UX guarantees

- **<200 ms first paint** — niche reel is static JSON, no LLM call.
- **Form survives refresh** — `localStorage` could be added later; today the
  status panel persists if the user navigates back to the same job id.
- **Failures surface in the same panel** — if any stage fails, the
  status card shows the stage + error string; no silent hangs.
- **Cancellation** — `POST /api/jobs/{id}/cancel` flips the job to
  `cancelled` and drops queued tasks.

---

## 2. Visitor — free-form chat (alternative path)

For visitors who don't want a form. The chat panel on the right is
always available; clicking the **Open prompt** card opens it with a
welcome message.

```mermaid
sequenceDiagram
    autonumber
    actor V as Visitor
    participant UI as Browser (chat panel)
    participant CR as Cloud Run /api/chat
    participant AZ as Azure OpenAI

    V->>UI: types "AITA short — burned my SIL's cake"
    UI->>CR: POST /api/chat {message, session_id?}
    CR->>AZ: chat.completions.create (system prompt + history)
    AZ-->>CR: assistant text + token usage
    CR->>CR: extract short_proposal JSON block
    CR-->>UI: {response, session_id, proposal?}
    UI->>V: shows assistant reply + (optional) proposal card
    V->>UI: clicks "Make this Short"
    UI->>CR: POST /api/chat/confirm {session_id}
    CR->>CR: creates jobs/<id>, enqueues RENDER_SHORT
    CR-->>UI: {job_id, task_id, proposal}
    UI->>UI: starts polling /api/jobs/{id} (chat-inline status card)
```

After the confirm, this path joins the same render → upload → research
chain as the form flow.

### Why two paths?

Different user mindsets:
- **Form** — "I know what I want, just take my topic and length."
  Faster path to a finished Short. Form fields are forgiving (notes,
  length slider, source-kind dropdown).
- **Chat** — "I'm exploring. I want the AI to ask questions and shape
  this with me." Better for ambiguous topics, multi-turn refinement,
  and people who type fast.

The chat extracts the same `ShortProposal` schema the form uses, so
the downstream chain is identical.

---

## 3. Owner — auto-publish + monitor

The site operator (you) wants more than the visitor experience: render
caps that don't lock you out of your own URL, monitoring, cancel.

```mermaid
flowchart LR
    A[Owner laptop] -->|outbound HTTPS| CR[Cloud Run]
    A -->|/agent/lease| Q[Firestore queue]
    A -->|leased task| W[render_short worker]
    W -->|GCS upload| G[Cloud Storage]
    W -->|YT upload| Y[YouTube]
    Y -->|video_id| FS[(Firestore: jobs)]

    O[Operator browser] -->|GET /api/health| CR
    CR -->|agents, spend usage| O
    O -->|POST /api/jobs/.../cancel| CR
    CR -->|drains queued tasks| Q
```

### Owner-specific flows

| Action | How |
|---|---|
| **Bypass per-IP rate limit** | Set `YTFACTORY_OWNER_IPS=<your-ip>` env on Cloud Run. Spend cap still applies. |
| **Monitor agent presence + spend** | `GET /api/health` → agents (last seen / mlx_free / kokoro_warm) + Azure spend USD today vs cap. |
| **Cancel a stuck job** | `POST /api/jobs/{id}/cancel`. Flips job status → `cancelled`, marks queued tasks FAILED so no agent leases them. Already-running tasks finish on the laptop. |
| **Publish to a YouTube channel** | The `pipeline/upload.py` module uses the OAuth tokens stored under `data/intermediate/<channel>/youtube_oauth.json`. The render worker passes `channel` as the upload target. |
| **Operate locally** | `./scripts/serve_cloud.sh` — same FastAPI app, runs on `:8765` with hot-reload on `control/`, `web/`, `shared/`. Useful when iterating on control-plane code before redeploying. |

---

## State machine — where any job lives at any moment

```mermaid
stateDiagram-v2
    [*] --> pending : user confirms
    pending --> rendering : agent leases RENDER_SHORT
    rendering --> uploading : mp4 produced
    uploading --> done : short_uri written
    done --> publishing : YOUTUBE_UPLOAD task picked up
    publishing --> researching : YT video_id known
    researching --> [*] : research handoff complete
    pending --> cancelled : POST /api/jobs/{id}/cancel
    rendering --> failed : exception in worker
    uploading --> failed : GCS unreachable
    publishing --> failed : YT API error
```

The `failed` and `cancelled` terminal states surface to the UI through
the same `/api/jobs/{id}` endpoint with `status` + `error` set; the
status card stops polling and shows a retry hint.

---

## Anti-abuse perimeter

Every public-facing endpoint has tiered limits. Defaults are tuned so a
single curious visitor can chat freely but bots can't burn the daily
Azure budget.

| Limit | Default | Where |
|---|---|---|
| Anonymous chat msgs / IP / day | 100 | `control/rate_limit.py` |
| Anonymous renders / IP / day | 20 | `control/rate_limit.py` |
| Per-session chat turns | 50 | `control/chat_service.py` |
| Global Azure spend / day | $5 USD | `YTFACTORY_AZURE_DAILY_CAP_USD` env, returns 503 |
| Owner IP bypass | none | `YTFACTORY_OWNER_IPS` env, comma-separated |

When a limit fires the UI surfaces a friendly message; the daily reset
is at UTC midnight (the Firestore counter doc is keyed by date).
