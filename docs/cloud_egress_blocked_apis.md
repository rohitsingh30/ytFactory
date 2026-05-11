# Public APIs that block Cloud Run egress (CLASS-OF-BUG)

> **Established 2026-05-11** during the `/api/discover/{channel}`
> deploy. The first live smoke against `mystoriesanimated/tifu`
> returned a 502 with `403 Client Error: Blocked for url:
> https://www.reddit.com/r/tifu/top.json…` even though the same call
> works from a laptop. Root cause: **Reddit blocks Cloud Run egress
> IP ranges**. Other "public, no-auth" scraping endpoints are likely
> to behave the same way once they're hit from cloud infra.

## The hazard

Public scraping endpoints that "just work" from a laptop browser /
local Python process can return **403 / 429** when called from
Cloud Run. Reddit is the confirmed case (egress-IP block on
`asia-southeast1` ranges). X / TikTok / YouTube embedded transcripts
typically have similar gates. The 403 is silent — no error in the
laptop dev loop, regression only surfaces in production.

## Mitigation: open-source Pullpush + optional Reddit OAuth (2026-05-11)

`pipeline/sources/reddit_api.py` now auto-selects between three
backends, so the Cloud Run egress block is no longer a hard wall:

```
1. OAuth         → oauth.reddit.com   (requires REDDIT_CLIENT_ID + _SECRET)
2. Pullpush      → api.pullpush.io    (open-source, no auth, hours-delayed)
3. Anonymous     → www.reddit.com     (laptop only; 403s on Cloud Run)
```

**Selection logic** (`_pick_backend()`):

```
if REDDIT_FETCH_BACKEND env explicitly set        → use it (oauth|pullpush|anon)
elif REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET set  → "oauth"
elif K_SERVICE env set (Cloud Run runtime marker) → "pullpush"  ← AUTOMATIC
else                                              → "anon"
```

**This means cloud renders work out of the box** — no per-app Reddit
registration required. The cloud worker / `ytfactory-web` detect
`K_SERVICE=...` (Cloud Run sets this on every container) and route
to Pullpush automatically. The only user-visible difference from
the OAuth path is freshness: Pullpush is archival (hours-delayed),
so a Reddit URL the user pasted within the last few hours might
not be indexed yet — in which case `fetch_post_by_url` raises and
the caller degrades to user-typed `topic`/`notes`.

### When to register a Reddit script-app on top of this

Register only if you need *real-time freshness* (≤ a few minutes
old). The cloud-side recipe stays the same:

```
1. https://www.reddit.com/prefs/apps → create "script" app.
2. echo -n "<id>"     | gcloud secrets create reddit-client-id     --data-file=-
   echo -n "<secret>" | gcloud secrets create reddit-client-secret --data-file=-
3. for s in reddit-client-id reddit-client-secret; do
     gcloud secrets add-iam-policy-binding "$s" \
       --member="serviceAccount:tts-runner@ytfactory-prod-v2.iam.gserviceaccount.com" \
       --role=roles/secretmanager.secretAccessor
   done
4. gcloud run jobs update     ytfactory-render-worker-v2 --update-secrets=REDDIT_CLIENT_ID=...,REDDIT_CLIENT_SECRET=...
   gcloud run services update ytfactory-web              --update-secrets=REDDIT_CLIENT_ID=...,REDDIT_CLIENT_SECRET=...
```

Once the secrets are bound, `_pick_backend()` returns `"oauth"` and
the Pullpush backend is bypassed automatically.

### Forcing a backend explicitly

For canary / debugging, set `REDDIT_FETCH_BACKEND` to one of
`oauth | pullpush | anon` to skip auto-selection:

```bash
gcloud run jobs update ytfactory-render-worker-v2 \
  --update-env-vars=REDDIT_FETCH_BACKEND=pullpush
```

## What it means for ytFactory

- **Cloud-side adapter calls cannot assume native sources will work.**
  Any pipeline path that hit a public scraping endpoint from the
  laptop must have a **non-native fallback** before it ships to a
  Cloud Run service / job.
- **`pipeline/sources/reddit_api.py` and `pipeline/social/reddit_scrape.py`
  are the canonical Reddit hits.** Both raise on 4xx via
  `requests.raise_for_status()` → caller surfaces as 502 unless
  explicitly caught.
- **The `/api/discover/{channel}` endpoint already wraps
  `_native_items_for` in `_safe_native_items_for`** so a Reddit 403
  silently degrades to LLM-only. This is the pattern other cloud-side
  adapters should follow.

## Affected channels

| channel | native source | cloud-egress risk |
|---|---|---|
| `mystoriesanimated` (all variants) | r/AmItheAsshole, r/tifu, r/MaliciousCompliance, r/ProRevenge | **confirmed 403** |
| `scrollpulse` | round-robin r/AskReddit etc. | **confirmed 403** (same reddit.com host) |
| `historyrecapped` | en.wikipedia.org "On this day" | low (wiki not known to block) |
| `cosmosdecoded` | en.wikipedia.org filtered | low |
| `airecap` | news.ycombinator.com top AI | low (HN doesn't IP-block) |

## Mitigations available today

1. **Catch + degrade pattern** — wrap the source call in
   `_safe_<adapter>_items_for(...)` that swallows the exception, logs
   it, and returns `("", [])`. Caller composes with an LLM brainstorm
   fallback. Reference impl:
   `control/routes/discover_routes.py::_safe_native_items_for`.
2. **LLM brainstorm always blended** — even when the native source
   succeeds, blend in a few LLM-generated candidates so the operator
   sees variety on every "Generate another" click. See
   [`docs/discover_topic_generation.md`](./discover_topic_generation.md).
3. **`YTFACTORY_DISCOVER_LLM_DISABLE=1`** — set in CI / tests to
   force the native-only path so the test asserts the 502 surface
   without hitting the LLM backend.

## Mitigations that we have NOT shipped yet

- **Egress NAT through a residential proxy** — rejected: cost +
  TOS risk + adds a new failure mode (proxy upstream).
- **Run the scrape on a Cloud Function with a different egress range** —
  same IP-range block applies to all Google datacenter ranges.
- **PRAW / official OAuth** — would solve the 403 but requires
  per-channel app registration + token rotation. Out of scope while
  the LLM fallback is working.

## Sweep recipe — find new sites that hit public scraping APIs

Run when adding a new channel / niche / source adapter:

```bash
# Any module calling a known scraping host from cloud-eligible code
grep -rnE "(reddit\.com|x\.com|twitter\.com|tiktok\.com|youtube\.com/watch)" \
    pipeline/ control/ web/ --include='*.py'
# Audit each hit: is it in a path that runs on Cloud Run? If yes,
# does it have a degrade-to-fallback wrapper?
grep -rn "raise_for_status\(\)" pipeline/sources/ pipeline/social/ --include='*.py'
```

Each match in code that runs server-side (not in a `__main__` block,
not behind `if not _is_cloud_run()`) is a candidate for the safe-
wrapper pattern.

## Cross-references

- [`docs/discover_topic_generation.md`](./discover_topic_generation.md) —
  the dispatcher pattern (`_safe_native_items_for` + LLM blend).
- [`docs/reddit_scraping.md`](./reddit_scraping.md) — Reddit JSON-API
  details + per-call rate limits. Now references this doc for the
  cloud-egress case.
- `pipeline/sources/reddit_api.py` — module docstring extended with
  pointer to this doc.
- `~/.claude/projects/.../memory/feedback_reddit_blocks_cloudrun_egress.md` —
  terse memory pointer.

## Recurrence escalation

If a **second** scraping host (X, TikTok, embedded YouTube) is
confirmed blocked from Cloud Run egress, escalate to a dedicated
"cloud-side scraper architecture" decision (residential proxy /
official API auth / scrape-on-laptop-and-cache pattern). Single
observation today (Reddit) — degrade-to-LLM is the right move.
