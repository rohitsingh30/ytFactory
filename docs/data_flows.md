# Data Flows

System-level view of how data moves through ytFactory. Complements
`user_flows.md` (which is told from the user's perspective) and
`architecture.md` (which describes the components themselves).

If you're debugging "where did my data go?" or "why is this stuck?",
start here.

---

## 0. Where data lives

```mermaid
graph TB
    subgraph Cloud["Cloud (always on, scale to zero)"]
        CR[Cloud Run<br/>FastAPI app]
        FS[(Firestore<br/>jobs + tasks + chat_sessions + ratelimits)]
        GCS[(Cloud Storage<br/>gs://ytfactory-prod-artifacts)]
        SM[(Secret Manager<br/>Azure key + agent token)]
    end

    subgraph Laptop["Laptop (intermittent, holds GPU + models)"]
        AG[agent/main.py]
        WORKERS[workers/heavy + workers/light]
        PIPE[pipeline/* + make_shorts.py<br/>Kokoro / mflux / whisper / ffmpeg]
        DISK["data/cache + data/intermediate<br/>(transient, sweepable)"]
    end

    subgraph External["External services"]
        AZ[Azure OpenAI]
        YT[YouTube Data API]
        REDDIT[Reddit / Wikipedia]
    end

    Browser --> CR
    CR -->|read/write| FS
    CR -->|signed URLs| GCS
    CR -->|chat completions| AZ
    AG -->|outbound HTTPS only| CR
    AG --> WORKERS
    WORKERS --> PIPE
    PIPE --> DISK
    WORKERS -->|upload mp4/thumb| GCS
    WORKERS -->|publish video| YT
    WORKERS -->|scrape source| REDDIT
    CR -->|read at boot| SM
```

ASCII shorthand:

```
       Browser
          │
          ▼
    Cloud Run app  ◄──── Azure OpenAI (chat)
          │
          ├──► Firestore  (jobs / tasks / chat / ratelimits)
          ├──► Cloud Storage  (gs://.../jobs/<id>/...)
          └──► Secret Manager (read once at boot)
                    ▲
                    │ outbound HTTPS only
                    │
              Laptop agent
                    │
                    ├──► workers/heavy (render)
                    ├──► workers/light (yt upload, research handoff)
                    └──► data/cache + data/intermediate (transient)
                              │
                              └──► Reddit / Wikipedia / YouTube (external)
```

Two collections in Firestore are worth knowing:

| Collection | Doc id | What it holds |
|---|---|---|
| `tasks/<task_id>` | uuid | A single unit of work in the queue. Status: queued / leased / done / failed. |
| `jobs/<job_id>` | uuid | User-facing job state. Tracks all stages from chat-confirm to YT publish. |
| `chat_sessions/<session_id>` *(future)* | uuid | Multi-turn chat state. In-memory today; Firestore-backed when sessions need to outlive a Cloud Run cold start. |
| `ratelimits/<date>/...` | YYYY-MM-DD | Per-IP daily counters + global daily Azure spend. Resets at UTC midnight. |
| `youtube_videos/<video_id>` | YT video id | Post-publish handoff target — research pipeline reads from here. |

GCS layout under `gs://ytfactory-prod-artifacts/jobs/<job_id>/`:

```
proposal.json         # the ShortProposal that birthed the job (kept 30d)
short.mp4             # the finished Short (kept 7d → lifecycle delete)
thumb.png             # YouTube thumb (kept 30d)
beats/00..NN.png      # per-beat illustrations (kept 1d → lifecycle delete)
voice.wav             # narration (kept 1d)
captions.srt          # word-timestamps (kept 1d)
script.json           # rewritten narration (kept 1d)
cast.json             # character descriptions (kept 1d)
prompts.json          # per-beat scene prompts (kept 1d)
footage/cut_NN.mp4    # broadcast cut-ins (kept 1d, sports only)
```

After a successful YouTube upload, the worker explicitly **GCs** the
heavy artifacts (beats/, voice.wav, etc.) so they don't sit on the
1-day clock. Lifecycle rules are the safety net.

---

## 1. Chat extraction — text in, ShortProposal out

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI
    participant CR as POST /api/chat
    participant RL as rate_limit
    participant CS as ChatService
    participant AZ as Azure OpenAI
    participant SP as ShortProposal

    User->>UI: "AITA short — burned my SIL's cake"
    UI->>CR: {message, session_id}
    CR->>RL: check_spend_cap()
    RL-->>CR: ok (under $5/day)
    CR->>RL: check_and_increment(IP, "chat")
    RL-->>CR: ok (under 100/day)
    CR->>CS: chat(session_id, message)
    CS->>CS: load session messages from in-memory store
    CS->>AZ: chat.completions.create([system, ...messages, user])
    AZ-->>CS: assistant text + usage{prompt_tokens, completion_tokens}
    CS->>RL: record_token_usage(p, c)
    RL->>RL: backend.add_spend(usd)
    CS->>CS: _extract_proposal(text) — regex + balanced-brace
    Note over CS: looks for {"type":"short_proposal", ...}
    CS->>SP: pydantic-validate as ShortProposal
    CS-->>CR: ChatResult(response, proposal?)
    CR-->>UI: {response, session_id, proposal?}
```

The system prompt is in `control/chat_service.py:SYSTEM_PROMPT`. It
asks the model to emit the JSON block once it has channel + topic.
The extractor is forgiving:

- accepts `{"type":"short_proposal"...}` and `{"type": "short_proposal" ...}` (whitespace tolerated)
- balanced-brace walk handles nested objects in `notes`
- failed `json.loads` returns `None` (the chat continues without an
  enqueueable proposal); the user sees the assistant text but no card

---

## 2. Job creation — proposal to queued task

Two entry points, identical downstream:

```mermaid
flowchart LR
    A[POST /api/render<br/>form-driven] --> Z(_enqueue_render_job)
    B[POST /api/chat/confirm<br/>chat-driven] --> Z
    Z -->|create_job| F[(Firestore<br/>jobs/&lt;id&gt;)]
    Z -->|enqueue| Q[(Firestore<br/>tasks/&lt;id&gt;)]
    Z --> R[ConfirmResponse<br/>job_id + task_id]
    F --> P[status=pending<br/>stage=queued]
    Q --> S[kind=RENDER_SHORT<br/>status=queued<br/>created_at=now]
```

- **Both paths** call `chat_routes._enqueue_render_job(proposal)`.
- A `JobEnvelope` (Firestore: `jobs/<id>`) and a `TaskEnvelope`
  (Firestore: `tasks/<id>`) are created in the same call. They share
  the `job_id`; the task starts the chain.
- Status is `pending` until an agent leases the task; once leased,
  the worker advances it to `rendering`, `uploading`, `done`.

---

## 3. Lease protocol — pull-based, outbound-only

The laptop is behind NAT and can't accept inbound connections. The
agent connects out to Cloud Run, long-polls for tasks, and acks them
back over the same connection.

```mermaid
sequenceDiagram
    autonumber
    participant AG as Laptop agent
    participant CR as Cloud Run /agent/*
    participant FS as Firestore
    loop every 15s
        AG->>CR: POST /agent/heartbeat<br/>{agent_id, mlx_free, kokoro_warm, ...}
        CR-->>AG: 200 server_time
    end
    par lease loop (long-poll up to 30s)
        AG->>CR: POST /agent/lease {agent_id, caps}
        CR->>FS: query queued tasks WHERE kind IN caps
        Note over CR: composite index on<br/>status + kind + created_at
        alt match found
            FS-->>CR: candidate task
            CR->>FS: transactional set status=leased,<br/>lease_owner=agent, lease_expires_at=now+ttl
            CR-->>AG: {task, payload}
            AG->>AG: run worker (render_short / yt_upload / research_handoff)
            AG->>CR: POST /agent/ack/{task_id} {status: ok|error, output_uri?, error?}
            CR->>FS: status=done OR re-queue (until max_attempts)
        else no work
            CR-->>AG: {task: null, wait_s: 30}
        end
    end
```

### Reliability guarantees

| Failure mode | Recovery |
|---|---|
| Agent crashes mid-task | Lease has TTL (default 5 min). Reaper requeues. |
| Lease ack lost in transit | Stale ack with `lease_owner != agent_id` is ignored. Task already requeued. |
| Worker raises exception | `runner.run` returns `(False, None, "<exception>")`; ack with `status=error`; task re-queued up to `max_attempts=3` times before FAILED. |
| Cloud Run cold start | Heartbeat gets exponential backoff up to 30s; lease loop continues. |
| Firestore composite index missing | Lease endpoint 500s; agent backoff retries. (Created at deploy time — see `gcloud firestore indexes composite list`.) |

---

## 4. Render — heavy worker on laptop

The big one. RENDER_SHORT mega-task wraps the existing
`make_shorts.py` pipeline so we didn't have to split each stage into
its own task.

```mermaid
flowchart TB
    Start([RENDER_SHORT leased]) --> A[build raw story from payload]
    A --> B[resolve channel YAML<br/>mystoriesanimated → channels/mystoriesanimated.yaml]
    B --> C[mark stage=rewrite_cast]
    C --> D[claude rewrite + cast<br/>parallel asyncio.to_thread]
    D --> E[mark stage=render]
    E --> F[subprocess<br/>python make_shorts.py --script ...]
    F --> G{rc == 0?}
    G -- no --> X[mark_failed stage=render]
    X --> X2[ack error → re-queued or FAILED]
    G -- yes --> H[find data/shorts/&lt;slug&gt;.mp4]
    H --> I[mark stage=gcs_upload]
    I --> J[upload mp4 + thumb + proposal to GCS]
    J --> K[mark_done short_uri set]
    K --> L[enqueue YOUTUBE_UPLOAD]
    L --> M[cleanup data/intermediate/&lt;channel&gt;/&lt;slug&gt;]
    M --> End([ack ok])
```

`make_shorts.py` itself is a separate world — it loads Kokoro, mflux,
Whisper, runs ffmpeg, hits 8 internal stages. The render worker
treats it as a black box and just owns the file-level contract:

- **input**: data/intermediate/&lt;channel&gt;/scripts/&lt;slug&gt;.json
- **output**: data/shorts/&lt;slug&gt;.mp4 + (optional) thumb png

If you want to know what happens *inside* make_shorts.py, see
`docs/legacy_pipeline.md`.

### Per-task scratch + cleanup

Every leased task gets a fresh temp dir at `$YTFACTORY_SCRATCH_ROOT/task-<id8>-...`.
The runner deletes it unconditionally on ack — success, failure, or
worker exception. So even if a render crashes the laptop process, the
scratch dir doesn't leak.

```mermaid
flowchart LR
    L[lease task] --> S[mktemp scratch dir]
    S --> R[run worker fn with TaskContext]
    R -->|any outcome| C[shutil.rmtree scratch]
    C --> A[ack /agent/ack/&lt;id&gt;]
```

---

## 5. Post-render — YouTube upload + GC + research handoff

```mermaid
sequenceDiagram
    autonumber
    participant AG as Agent
    participant FS as Firestore
    participant GCS
    participant YT
    participant R as pipeline.research

    Note over AG: leases YOUTUBE_UPLOAD
    AG->>GCS: download short.mp4 to scratch
    AG->>YT: insert(video, channel=...)
    YT-->>AG: video_id
    AG->>FS: jobs/&lt;id&gt;.youtube_url = https://youtu.be/...
    AG->>GCS: gc_heavy_artifacts(job_id)<br/>delete beats/, voice.wav, captions, scripts/cast/prompts
    AG->>FS: enqueue RESEARCH_HANDOFF

    Note over AG: leases RESEARCH_HANDOFF
    AG->>FS: youtube_videos/&lt;video_id&gt; = handoff metadata
    AG->>R: pipeline.research.rebuild(quiet=True)
    AG->>R: pipeline.youtube_stats.fetch_all([video_id])
    Note over AG: research-side helpers wrapped in try/except —<br/>YT is already published, never fail this step
```

### Why GC before research?

The published video is the persistent artifact. The intermediates
(beats, voice, captions) were only needed to *produce* it. Once it's
on YouTube:

- **Storage cost** trends to zero — only `proposal.json` + `thumb.png`
  + `short.mp4` (briefly) remain.
- **Research pipeline** works from the YouTube Data API + a small
  metadata doc. Doesn't need the rendering artifacts.
- **Privacy** — chat-extracted notes only live as long as the render
  is in progress.

GCS lifecycle rules are the **backstop**:

| Pattern | Auto-delete after |
|---|---|
| `jobs/*/short.mp4` | 7 days |
| `jobs/*/{thumb.png, proposal.json}` | 30 days |
| `jobs/*/{voice.wav, captions.srt, prompts.json, script.json, cast.json}` | 1 day |
| `jobs/**/*.png` (catches beats/) | 1 day |

---

## 6. Job status polling — the UI's view

After enqueue, the UI polls `GET /api/jobs/{id}` every ~3s until the
job reaches a terminal state (`done`, `failed`, `cancelled`).

```mermaid
sequenceDiagram
    participant UI
    participant CR as GET /api/jobs/{id}
    participant FS as Firestore jobs/&lt;id&gt;
    participant GCS
    loop every 3s
        UI->>CR: GET /api/jobs/{id}
        CR->>FS: get(job_id)
        alt status=done
            CR->>GCS: signed_url(short_uri, ttl=10min)
            GCS-->>CR: short-lived HTTPS URL
        end
        CR-->>UI: JobView{status, stage, short_signed_url, youtube_url, error}
        alt terminal status
            UI->>UI: stop polling
            UI->>User: download mp4 + watch on YT
        end
    end
```

The signed URL is regenerated on every poll (cheap) and TTLs to 10
minutes — long enough to download in the browser, short enough that
sharing the URL is safe.

---

## 7. Rate-limit + spend-cap circuit

A safety perimeter on the public endpoint that prevents bots and
runaway chats from draining the Azure budget.

```mermaid
flowchart TB
    Req[/POST /api/chat or /api/render/]
    Req --> A{owner_ip?}
    A -- yes --> Skip[skip per-IP counter]
    A -- no --> B{spent &gt;= $5/day cap?}
    B -- yes --> S503[503 Service Unavailable<br/>"resumes after UTC midnight"]
    B -- no --> C{IP used quota?}
    C -- yes --> S429[429 Too Many Requests]
    C -- no --> D[counter +1]
    D --> Allow[forward to handler]
    Skip --> Allow
    Allow --> Handler[chat or render]
```

The counters live in `ratelimits/<UTC-date>/...` so they reset every
day automatically — no cron needed.

---

## 8. What surfaces in `/api/health`

For ops visibility into "is anything broken right now":

```mermaid
graph TB
    H[GET /api/health] --> A1[last_seen agents<br/>id, seconds_ago, mlx_free, kokoro_warm]
    H --> A2[azure_spend_usd_today]
    H --> A3[azure_spend_pct of cap]
    H --> A4[ok: true]
```

Sample response:

```json
{
  "ok": true,
  "agents": [
    {"agent_id": "Rohits-MacBook-Pro", "seconds_ago": 4,
     "mlx_free_pct": 78.0, "kokoro_warm": true, "mflux_warm": false,
     "on_battery": false}
  ],
  "azure_spend_usd_today": 0.0096,
  "azure_spend_cap_usd": 5.0,
  "azure_spend_pct": 0.19
}
```

If `agents` is empty → no laptop is connected; queued jobs sit waiting.
If `seconds_ago > 60` for the agent you expect → it's not heartbeating.
If `azure_spend_pct > 90` → chat is about to start returning 503s.

---

## Cross-references

- `docs/architecture.md` — components, deployment, IAM, repo layout
- `docs/user_flows.md` — three user types and their journeys
- `docs/legacy_pipeline.md` — what `make_shorts.py` does internally
- `README.md` — top-level overview and runtime instructions
