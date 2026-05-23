# Security Observations

Inventory of auth schemes, secrets handling, IAM, and surface risks.
Not an audit report — a map for the next person actually doing the
audit, with code citations.

Sources: code reads on 2026-05-22.

---

## 1. Authentication schemes (three layers)

### Layer A — Agent bearer token

**Where:** `control/auth.py` (canonical), `control/core/auth.py`
(duplicate copy).

**Mechanism:** Shared bearer token in env `YTFACTORY_AGENT_TOKEN`. Any
caller sending `Authorization: Bearer <token>` is admitted. Token
comparison is constant-time (`hmac.compare_digest` at
`control/auth.py:37`).

**Used by:**
- `control/agent_routes.py:32` (laptop agent leasing jobs from the
  control plane) — `APIRouter(prefix="/agent", dependencies=[Depends(require_agent)])`.
- `control/routes/agent_routes.py:32` — same (duplicate).
- `control/routes/state_routes.py:77`,
  `control/routes/scheduler_routes.py:28`,
  `control/routes/telemetry_routes.py:75`,
  `control/routes/cloud_routes.py:15`,
  `control/routes/critique_routes.py:153` — all gate behind
  `YTFACTORY_AGENT_TOKEN` Bearer header.

**Risks:**
- Shared secret. If leaked, the whole agent surface is compromised
  with no fine-grained revocation — rotate via Secret Manager
  (auth.py:8 says "Simple and good enough for v1; rotate via Secret
  Manager later" — i.e. not yet wired).
- Multiple gate sites duplicate the token-check logic
  (`scheduler_routes.py:20-23` etc.) rather than reusing
  `require_agent` from `control/auth.py`. Each ad-hoc check is one
  more place a constant-time-compare bug could hide.
- `control/scheduler_routes.py:23` returns 503 if the env is unset
  ("scheduler unauthenticated: YTFACTORY_AGENT_TOKEN not set") —
  fail-closed on missing config. Good.

### Layer B — Operator PIN session

**Where:** `control/routes/auth_pin.py`.

**Mechanism:** Single-user PIN gated by env `YTFACTORY_OPERATOR_PIN`.
PIN comparison is constant-time
(`hmac.compare_digest` at `auth_pin.py:158`). On successful
`POST /api/auth/login`, an HttpOnly session cookie `yt_pin_session`
is set. Cookie value = `HMAC_SHA256(secret, "issued=<unix_ts>")` —
no DB lookup; rotation via `YTFACTORY_SESSION_SECRET`.

**Cookie hygiene:**
- HttpOnly: yes (line 170).
- Secure: defaults TRUE in prod (line 178); override via
  `YTFACTORY_COOKIE_SECURE=0` for laptop http://localhost. Audit
  S1.24 referenced — pre-fix default was 0, which let the cookie
  cross plain HTTP.
- SameSite=Lax (line 172). Reasonable for the single-tenant case;
  if cross-site embed is ever needed, revisit.
- TTL 7 days (default; `YTFACTORY_SESSION_TTL_S`).
- Distinct cookie name from Google-OAuth session
  (`yt_session` vs `yt_pin_session`) per audit S1.20 to avoid
  silent 401 loops — confirmed at line 61.

**Used by:**
- `control/routes/render_routes.py:104` (POST /api/render),
  `control/routes/render_routes.py:557` (PUT publish),
  `control/routes/clone_video_routes.py:596` — all require PIN
  session via `Depends(require_pin)`.

**Risks:**
- Single PIN value = single point of compromise. The multi-tenant
  SaaS plan (auth_pin.py:7 references `docs/multitenant_saas_plan.md`)
  replaces this with Firebase Auth OIDC; not done yet.
- The HMAC issuance has no nonce — token replay within the 7-day TTL
  is possible if a cookie is stolen.
- `_RUNTIME_SECRET = secrets.token_hex(32)` at line 65 generates a
  process-local secret if `YTFACTORY_SESSION_SECRET` is unset.
  Multiple processes (laptop dev + prod) would issue tokens neither
  can verify across — and the docstring at line 65 admits this:
  "production SHOULD set the env." If prod is ever deployed without
  the env, every user gets logged out on every container restart
  (annoying, not catastrophic).
- The `auth_enabled()` function (line 68) returns False if PIN env
  is empty → "no gate." If prod is misconfigured (env not set),
  every endpoint that uses `require_pin` becomes wide open with no
  alarm. Fail-OPEN on missing config.

### Layer C — Firebase Auth (browser-side, critique chat)

**Where:** `firestore.rules` (browser-facing rules);
`control/routes/critique_routes.py:136-153` (token minting,
referenced).

**Mechanism:** Browser obtains a Firebase Auth custom token minted
by `POST /api/jobs/<id>/critique/token`. The custom token's uid is
`yt_<sha256-of-email-prefix>` (per `firestore.rules:6-13`). Same uid
is written to the critique doc as `created_by_uid` at start. Firestore
rule at `firestore.rules:35-38` enforces "browser uid matches
created_by_uid" for read/update/delete + create.

**Risks:**
- `firestore.rules:2` comment says `ytfactory-prod-v2 (2026-05-11)`
  but CLAUDE.md + deploy.sh use `ytfactory-prod-v3`. Stale comment
  — verify rules are actually deployed to v3's Firestore.
- The uid hashing (sha256 of email prefix) is one-way but
  predictable — anyone who knows a user's email can compute the
  expected uid. Combined with token-minting being server-side
  (legitimate path), this is fine — but if a future code path lets
  the browser MINT its own uid claim, the prediction lets an attacker
  generate a target's uid trivially.
- Implicit-deny on every other Firestore collection
  (`firestore.rules:50-57`) — good. Server-side code uses ADC and
  bypasses rules. Browser can only touch its own critique chat.

---

## 2. Owner UID Firestore-doc fencing

**Where:** `control/core/jobs.py:175-202` (write `owner_uid` on
job creation); `control/core/jobs.py:304-367` (write_back); read
fences at `control/routes/render_routes.py:190-227, 489-522`.

**Mechanism:** `owner_uid` is recorded on every job doc at creation
time. Read endpoints (e.g., `/api/jobs/{job_id}`) compare the
requesting user's uid (from `request.state.user_email`) to the doc's
`owner_uid`. Three cases per `render_routes.py:196-208`:
- No `owner_uid` on doc → admit (legacy or scheduler-driven jobs).
- `owner_uid` matches → admit.
- `owner_uid` does NOT match → deny.

**Risks:**
- The "no owner_uid → admit" case is a backward-compat hatch
  (line 196). Comment at line 226-228 says "once every in-flight
  job carries owner_uid we can flip this to deny." Today: a
  malicious caller could omit the field on doc-create (if there's
  any path that lets them write Firestore directly) → bypass
  fencing. The legitimate write paths all populate it
  (`jobs.py:175-202`), so the risk depends on no other code path
  existing that doesn't.
- `owner_uid` is set from `request.state.user_email`
  (render_routes.py:190) — i.e., the user's email is the uid.
  Not opaque. Predictable. Combined with the Firebase Auth
  uid-hashing (Layer C), there are TWO different uid conventions
  in use (email-string here, sha256 hash there). Future migration
  to multi-tenant SaaS needs to unify.
- Doc filters at `render_routes.py:522` use
  `d.get("owner_uid") in (None, user)` — legacy docs visible to
  current user. Same "admit if absent" risk.

---

## 3. Secrets handling

### Laptop (`.env`)

- `YTFACTORY_AGENT_TOKEN` — agent bearer token.
- `YTFACTORY_OPERATOR_PIN` — PIN.
- `YTFACTORY_SESSION_SECRET` — HMAC key for PIN cookies.
- `AZURE_OPENAI_API_KEY`, `ANTHROPIC_API_KEY` — LLM keys (per
  `cloud/render-worker-v2/deploy.sh:105, 137`).
- `YTFACTORY_COOKIE_SECURE` — toggle for HTTP localhost dev.

**Risk:** `.env` files are commonly committed by accident. Confirm
`.gitignore` covers `.env`, `.env.local`, `.env.*` (not verified in
this pass).

### Cloud (`Secret Manager` mount)

- `cloud/render-worker-v2/deploy.sh:105` →
  `--update-secrets="AZURE_OPENAI_API_KEY=azure-openai-key:latest"`.
- `cloud/render-worker-v2/deploy.sh:137` (echoed help) →
  `--update-secrets=ANTHROPIC_API_KEY=ytfactory-anthropic-key:latest`.

The switch from `--set-secrets` to `--update-secrets` at line 108-114
is documented as audit T1.11 — `--set-secrets` is REPLACE-not-merge,
so any subsequent `--update-secrets` toggle would wipe earlier secret
references. Switched to additive. Good (defends against operator
mistakes).

**Risk:** If a future deploy.sh reintroduces `--set-secrets`, the
secret-wipe regression returns. There's no lint / pre-commit check
to prevent this.

---

## 4. IAM service accounts

**Where:** `cloud/iam/` (referenced in CLAUDE.md). Sampled:
- `render-runner@ytfactory-prod-v3.iam.gserviceaccount.com` — service
  account for the Cloud Run JOB (`deploy.sh:100`).

**Risk surface (not fully audited):**
- Whichever role bundle the agent SA gets needs read access to logs /
  Firestore / GCS / metrics (per onboarding-qa Q76). The grant
  hasn't been verified in this pass.
- `render-runner@` SA needs to write to GCS (artifact uploads),
  read Firestore (job doc lookup), invoke other Cloud Run services
  (TTS, image-gen, ASR). Confirm least-privilege: it should NOT
  have project-wide editor.

---

## 5. Surface risks

### 5.1 Auth-OPEN-by-default for PIN flow

`auth_pin.py:68-71` — `auth_enabled()` returns False when
`YTFACTORY_OPERATOR_PIN` is unset. Every endpoint with
`Depends(require_pin)` becomes a no-op. The docstring at line 119
says "No-op when no PIN is configured (dev convenience)." For laptop
dev this is intentional. For prod a misconfigured deploy silently
removes authentication entirely.

**Mitigation:** A boot-time check that production env (recognised by
something like `GOOGLE_CLOUD_PROJECT == "ytfactory-prod-v3"`) MUST
have the PIN env or refuse to start. Not present today.

### 5.2 Bearer token in source-code env-var name

`grep "YTFACTORY_AGENT_TOKEN" pipeline/ control/ web/ --include="*.py"`
returns ~20 hits. The variable NAME is everywhere in code; the
VALUE is never (verified — no hits for `Bearer ` followed by a
literal token in code). Good — no secret-in-code.

### 5.3 Routes without auth (potential surface)

`web/server.py:1792-1813` imports 18 control routers. Of those:
- `agent_routes` — gated by `require_agent` (dependencies=[]).
- `render_routes` — selectively gated (104, 557 use `require_pin`;
  others may not).
- `auth_pin` — login/logout (correctly ungated).
- `dashboard_routes` — UNKNOWN (need per-route audit).
- `cloud_routes`, `discover_routes`, `music_routes`, `niche_routes`,
  `niche_specs_routes`, `voices_routes`, `song_sample_routes`,
  `clone_video_routes`, `channels_routes`, `script_jobs_routes` —
  per-route audit needed.

`grep "Depends(require_pin)\|Depends(require_agent)" control/routes/`
returned only:
- `clone_video_routes.py:596`
- `render_routes.py:104, 557`

That's THREE protected endpoints across an entire fleet of routers.
The other ~108 routes (per the grep count: 111 `@router.*` across
control/) are EITHER intentionally public OR silently unprotected.
This needs a per-route audit before any multi-tenant rollout.

### 5.4 Cookie security flag override

`auth_pin.py:178` — `YTFACTORY_COOKIE_SECURE=0` makes the cookie
non-Secure (sent over plain HTTP). Documented for laptop dev. If
that env is ever set in prod, the PIN session cookie is exposed on
HTTP intercepts.

### 5.5 No CSRF protection mentioned

`auth_pin.py` does not configure CSRF tokens. SameSite=Lax mitigates
most cross-site POST attempts but doesn't fully prevent
state-changing GET (rare in REST but possible).

### 5.6 Firestore rules drift from project

`firestore.rules:2` header says `ytfactory-prod-v2 (2026-05-11)`. The
current project is `ytfactory-prod-v3`. If the rules were never
re-deployed to v3, the rules in PROD are whatever Firestore default
is — which might be "allow read/write if request.auth != null" (a
common starter rule) or "deny all" depending on how the project was
created. Verify via
`gcloud firestore rules describe --project=ytfactory-prod-v3`.

### 5.7 OAuth flows

`control/routes/oauth_web_routes.py` exists (imported in
`web/server.py:1803`). Not inspected in this pass. YouTube upload
OAuth lives somewhere (per onboarding-qa Q9 + Q33: YouTube uploads
exist but Q53 says "Zero per-channel uploads/ records across all 7
channel dirs" — i.e., the upload path is broken or not used). The
OAuth credential storage path is unknown without per-file inspection.

### 5.8 Process-local fallback secret

`auth_pin.py:65` — `_RUNTIME_SECRET = secrets.token_hex(32)` generated
once per process if `YTFACTORY_SESSION_SECRET` is unset. Different
processes can't validate each other's tokens. Per CLAUDE.md the cloud
runs the worker as a JOB (one-shot per render) — so the secret
ROTATES on every render if env is unset. The `auth_pin` code only
runs in the `web-server` service (long-lived) where this fallback
would be stable across the container's lifetime but lost on restart.
Symptom: every container restart logs out all users.

---

## 6. Specific findings worth filing

| # | Finding | Severity |
|---|---|---|
| S1 | PIN-mode fails OPEN if env unset (auth_pin.py:68) | High |
| S2 | `firestore.rules` header says v2 but project is v3 | Medium |
| S3 | Per-route auth audit needed (3 of ~111 routes have explicit `Depends`) | High |
| S4 | `_RUNTIME_SECRET` fallback rotates across container restarts | Medium |
| S5 | `owner_uid is None → admit` backdoor in render_routes.py | Medium |
| S6 | Email-string vs sha256-hash uid mismatch between layers | Low |
| S7 | No boot-time check that prod has required auth env set | High |
| S8 | OAuth flows not audited in this pass | Unknown |
| S9 | `pipeline.x_upload.py` (X/Twitter) referenced in old docs but file does not exist in repo (already removed) — historical / not a current risk | None |

---

## What's healthy

- Bearer + PIN both use `hmac.compare_digest` (constant-time).
- Cookie name disambiguation (yt_session vs yt_pin_session) survived
  the audit S1.20 fix.
- Cookie Secure defaults TRUE in prod (audit S1.24 fix).
- Secrets pulled from Secret Manager, not baked into images.
- `--update-secrets` (additive) used instead of `--set-secrets`
  (replace) per audit T1.11.
- Firestore implicit-deny on every collection except critique-chat.
- Service accounts (render-runner@) follow the pattern of one SA
  per service.

---

## Next steps for an actual audit

1. Per-route audit of `control/routes/*.py` — categorise each
   endpoint as: public (intentional), public (oversight), gated
   (PIN), gated (agent).
2. Verify `firestore.rules` is deployed to v3.
3. Verify all SAs have least-privilege roles (no project-wide
   editor on `render-runner@`).
4. Add a boot-time check: prod env MUST set
   `YTFACTORY_OPERATOR_PIN` + `YTFACTORY_SESSION_SECRET` +
   `YTFACTORY_AGENT_TOKEN`. Refuse to start otherwise.
5. Re-audit cookie hygiene under the multi-tenant SaaS migration
   plan.
