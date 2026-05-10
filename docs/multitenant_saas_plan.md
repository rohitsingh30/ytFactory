# ytFactory as a multi-tenant SaaS — design doc (2026-05-09)

> **Status:** vision doc, not yet built. Captured 2026-05-09 to be
> revisited after the single-user product surface is finished.
> Current build is single-user (you, hosting on your own GCP). This
> doc describes the future state where any creator can sign up.

## The vision

A creator visits `ytfactory.app` (or whatever domain), clicks "Sign
in with Google", grants YouTube upload scope, picks a channel
template (gallery of 8 existing templates OR describes their own
format via chat), and within minutes their first 5 videos are
rendering. They review, edit, publish. Repeat.

**Decisions made (2026-05-09):**

| Dimension | Choice | Rationale |
|---|---|---|
| Product shape | **Shape B — hosted SaaS** | We host. Zero user setup. Real product. Heaviest build but the only path to "anyone can use it". |
| Auth | **Sign-in-with-Google + ytFactory OAuth client** | User clicks a button, Google consent, we get a token. Standard SaaS pattern — matches what every other YouTube tool does. |
| Template UX | **Gallery default, chat available** | Big visual cards of the 8 existing templates. "None of these fit?" link opens chat. Fast path for 90% of users; expressive path for the rest. |

## Hard constraints to design around

### YouTube Data API quota — the binding constraint

YouTube gives **10,000 units/day per Google Cloud project**. Each
upload costs ~1,600 units → **~6 uploads/day across the entire
platform**, regardless of user count, if all users share one GCP
project.

Three mitigations, in priority order:

1. **Playwright Studio fallback for the bulk** — drives `studio.youtube.com`
   from server-side Chrome. No quota. Slower (~60-90s per upload vs
   ~30s via API). The current `/upload-via-playwright` skill flow
   ports server-side. **Probably the primary upload path at scale.**
2. **Tiered plans** — paid users get scheduled uploads via API (daily
   slot), free users get queued/Playwright. Stretches the 6/day to
   matter only on the "instant publish" path.
3. **Batch GCP project sharding** — break users into pools, each pool
   on its own GCP project (each with its own 10K quota). Engineering
   complexity scales linearly with pool count.

**The quota math is what makes this hard.** It also means: don't
build SaaS until Playwright fallback is proven for unattended bulk
uploads — that's the foundation everything else sits on.

### User-content liability

We become the upload origin to user channels → DMCA notices come to
us first. Mitigations:
- ToS that places content liability on the user.
- Pre-publish content scan (Anthropic's content moderation API or a
  cheap classifier) for clearly-violating material.
- A "report" channel for takedowns.
- Don't store user-generated mp4s longer than ~30 days.

### Per-user data isolation

User A must not see user B's renders, scripts, uploads, OAuth tokens.
Move from filesystem-as-state (today) to a real DB:
- **Firestore** for all dict-y state (users, channels, jobs, uploads,
  critiques). Already-deployed in `ytfactory-prod-v2`.
- **GCS** for blob state (mp4s, audio, thumbnails) under
  `gs://ytfactory-user-content/<user_id>/...`.
- The orchestrator's existing in-memory `SCRIPT_JOBS` / `CRITIQUE_JOBS`
  / `UPLOAD_JOBS` dicts get replaced by Firestore reads + writes.

## The flow a new user sees

```
1. visits ytfactory.app
   → landing page sells the value prop, "Sign in with Google" CTA

2. signs in with Google → grants YouTube + OpenID scopes
   → we fetch their basic profile + their YouTube channel(s)
   → user record created in Firestore: {user_id, email, channel_ids[]}

3. picks a channel
   → "Which channel do you want me to upload to?"
   → big card per channel they own (with subscriber counts, cover art)

4. picks a template
   → gallery of 8 visual cards: AITA stories / Cosmos explainers /
     football docs / Hindi mythology / nursery rhymes / etc.
   → "None of these fit?" → chat: "tell me about your channel"
   → AI maps chat description to closest template + customizes
     prompts for their voice

5. ytFactory authors the first 5 episodes (autonomous)
   → uses pull_stories.py (or equivalent for the picked template)
   → renders via Cloud Run JOB (each user's spec.json gets a
     user_id field; mp4 lands in their isolated GCS prefix)

6. user reviews each episode
   → big preview player, /v/<slug> page (already designed)
   → critique panel: "8/10 ship it"
   → edit title/description inline, click Publish
   → Playwright uploads to their YouTube (or API on the daily slot)

7. user goes back to /
   → "your channel" dashboard with all their renders + uploads
   → "Render 5 more" CTA repeats the flow
```

## Architecture sketch

```
                  User's browser
                       │
                       ▼
         ┌───────────────────────────┐
         │ ytfactory-web             │ Cloud Run service, public domain
         │ (Cloud Run service)       │ 
         │  - sign-in-with-Google    │ → Google OIDC
         │  - per-user UI            │
         │  - tenant routing         │
         └───────┬───────────────────┘
                 │
                 ├──► Firestore                ← users, channels, jobs, uploads, critiques
                 ├──► gs://ytfactory-user-content/<user_id>/...
                 │
                 │ "render this for user X"
                 ▼
         ┌───────────────────────────┐
         │ ytfactory-render-worker   │ Cloud Run JOB (per-execution)
         │ (already exists)          │ spec.json includes user_id
         └───────┬───────────────────┘
                 │
                 ├──► existing TTS + image services (shared, GPU-quota-bounded)
                 │
                 ▼
         ┌───────────────────────────┐
         │ ytfactory-uploader        │ Cloud Run JOB triggered after critique-passed
         │ (new)                     │ Playwright + headless Chrome
         │  - drives Studio UI        │ Falls back to API when within quota
         │  - fans out to user's chan │
         └───────────────────────────┘
```

## Per-user state model (Firestore)

```
users/<user_id>
  {email, google_oauth_token, created_at, plan: free|pro,
   monthly_render_count, monthly_upload_count}

users/<user_id>/channels/<channel_id>
  {youtube_channel_id, channel_handle, template:
   "aita_animated"|"cosmos_short"|..., template_overrides:{...},
   subscriber_count, last_synced_at}

users/<user_id>/channels/<channel_id>/renders/<render_id>
  {state, started_at, completed_at, mp4_gcs_uri, log_gcs_uri,
   error, critique_id, upload_id}

users/<user_id>/channels/<channel_id>/uploads/<upload_id>
  {video_id, video_url, privacy, scheduled_at, ...}
```

## Pricing shape (placeholder for later)

| Tier | $/mo | Renders/mo | Uploads/mo | Channels |
|---|---:|---:|---:|---:|
| Free | $0 | 5 | 5 (Playwright only) | 1 |
| Creator | $19 | 50 | 50 | 3 |
| Studio | $99 | 500 | 500 | 10 |

Actual rendering cost (~$0.05/Short) means $19/mo on 50 renders nets
~$16.50 margin. $99 on 500 renders nets ~$74. Healthy unit economics
unless YouTube quota blows up the upload path.

## Build phases (rough sequencing, post single-user product)

| Phase | Effort | What lands |
|---|---|---|
| **0** | done | Single-user product UI (current focus) |
| **1** | 1 week | Firestore-backed state for SCRIPT_JOBS / CRITIQUE_JOBS / UPLOAD_JOBS. Multi-tenant data isolation under the hood. |
| **2** | 1 week | Sign-in-with-Google + per-user OAuth token storage in Firestore. Tenant scoping in every API endpoint. |
| **3** | 3 days | Channel picker + template gallery in the new UI. New user flow lands their first render. |
| **4** | 1 week | Server-side Playwright uploader (port `/upload-via-playwright` into a Cloud Run JOB). |
| **5** | 1 week | Billing + plans (Stripe, plan gates, usage metering). |
| **6** | ongoing | Onboarding polish, ToS, abuse controls, support tooling. |

**Total to first paying customer: ~5-6 weeks** of focused work, after
the single-user product feels solid.

## What stays, what changes from today's stack

**Stays:**
- The render-worker JOB (`ytfactory-render-worker`)
- The orchestrator endpoints (`/api/jobs/from_script`, `/api/critique`,
  `/api/uploads/from_job`, `/api/cron/drain`)
- The `pipeline/*` rendering code (channel-agnostic)
- The 8 existing channel templates (become the gallery)

**Changes:**
- `SCRIPT_JOBS` dict → Firestore collection
- File-system channel state → GCS-prefixed per-user trees
- OAuth client_secret.json → multi-tenant OAuth flow + per-user token
  in Firestore
- The /api/uploads/from_job endpoint gains a `user_id` axis;
  Playwright fallback becomes the primary upload path
- `/renders` (current) becomes `/admin` (engineer mode); the user
  sees a per-channel dashboard at `/c/<their-channel-id>`

## Open questions for "later"

1. **Custom domain / branding** — `ytfactory.app` or something
   creator-friendlier? Domain availability + brand check.
2. **Pricing** — the table above is a placeholder. Need to actually
   measure CAC + churn before locking.
3. **Content moderation** — what's the right pre-publish content
   scan policy?
4. **Refund / cancel** — what does a user keep when they cancel?
   Their renders? Their access to render-history? Their auto-uploads?
5. **API for power users** — once we have endpoints, expose them as
   a public API for creators who want to integrate with their own
   tools.

## Memory pointer

`project_multitenant_saas_plan_2026_05_09.md` — points at this doc.
