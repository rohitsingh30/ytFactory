# ytFactory

End-to-end product for generating YouTube Shorts from a chat conversation.

A user opens the website, describes the Short they want in chat, and the
system produces a finished 1080×1920 mp4 — script, voice, illustrations,
captions, broadcast cut-ins where applicable. Owner accounts can also
publish the result directly to a connected YouTube channel.

> **Status: mid-migration.** The repo is being split from a monolithic
> laptop-only pipeline into a hybrid cloud + laptop architecture. See
> `docs/architecture.md` for the target design and `docs/legacy_pipeline.md`
> for how the current monolithic pipeline still works while the migration
> is in progress.

## Architecture in one line

Cloud control plane on Cloud Run + Firestore + GCS, fronted by a public
chat UI; light I/O work runs on Cloud Run jobs; heavy MLX rendering runs
on the laptop via a pull-based agent over outbound HTTPS.

## Repo layout (target)

```
control/   # Cloud Run service: chat, queue, scheduler, telemetry, auth
workers/
  light/     # Cloud Run jobs: claude CLI, scrapers, YT upload
  heavy/     # invoked by laptop agent: images, tts, asr, compose, footage
agent/     # laptop daemon (launchd) — heartbeat, lease, runner
shared/    # llm wrapper, schema, channel resolver, prompts
channels/  # nested by target YT channel + per-format YAMLs
web/static/ # chat UI + operator UI
```

The legacy flat layout (`pipeline/`, `make_shorts.py`, `web/server.py`,
`pull_stories.py`, `upload.py`) still exists during the migration and
will be torn down step by step.

## Production targets

| YouTube channel | Source | Aesthetic |
|---|---|---|
| **MyStoriesAnimated** | Reddit (AITA / TIFU / etc.) | Flat 2D crayon, pastel fills |
| **SportsStoriesAnimated** | Football moments | Tifo line-art + real broadcast cut-ins at the climactic moment |
| **MahabharatHindi** | Mahabharat episodes | Amar Chitra Katha comic-book, Hindi narration |

Other channel YAMLs are research / variant configs that may share a
target channel.

## Running locally (legacy path, current default)

```bash
./scripts/serve.sh   # FastAPI on :8765, web UI at /
```

This launches the monolithic web/server.py which spawns subprocesses
for `pull_stories.py` → `make_shorts.py` → `upload.py`. It works today
and stays working until the migration is complete.

## Cost target

<$10/month at low traffic. Hard caps in code from day 1: per-IP +
per-user rate limits, $5/day Azure OpenAI spend cap, scale-to-zero
on every cloud component, no GPU on cloud (ever).

## License

Private.
