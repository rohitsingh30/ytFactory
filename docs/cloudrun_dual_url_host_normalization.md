# Cloud Run dual-URL host normalization

> **TL;DR.** Every Cloud Run service is reachable on **two** equivalent
> public URLs — the project-id-hash form
> (`ytfactory-web-next-7hwnzw7lya-as.a.run.app`) and the project-number
> form (`ytfactory-web-next-283470729204.asia-southeast1.run.app`). They
> hit the same container but have **separate cookie jars**. Any feature
> that depends on a cookie surviving across a redirect — sign-in,
> CSRF tokens, session continuity — breaks the moment a user lands on
> the non-canonical host. The repo-wide fix is edge-middleware host
> normalization in `web-next/middleware.ts`, gated on
> `YTFACTORY_CANONICAL_HOST` (auto-set by `cloud/web-next/deploy.sh`).

## What broke (2026-05-12)

User clicked "Sign in with Google" from
`https://ytfactory-web-next-283470729204.asia-southeast1.run.app/login`
and got dumped on:

```
{"error":"state mismatch"}
```

at:

```
https://ytfactory-web-next-7hwnzw7lya-as.a.run.app/api/auth/google/callback?state=…&code=…
```

## Why it broke

The OAuth flow is a three-hop dance:

1. Browser hits `…-283470729204.…/api/auth/google/login`. FastAPI
   mints a CSRF nonce, stores it in cookie `yt_oauth_state` (host =
   `…-283470729204.…`, scope `/api/auth/`), 302s to Google.
2. Google takes consent + redirects back to the **single** registered
   `redirect_uri`, which is `…-7hwnzw7lya-as.a.run.app/api/auth/google/callback`.
3. Browser sends the callback to host `…-7hwnzw7lya-as.a.run.app`. The
   `yt_oauth_state` cookie was set on a different host, so it's not
   sent. FastAPI compares `state` (from query) to cookie (empty),
   mismatch → `400 state mismatch`.

```
host A (.-283470729204.-)              host B (.-7hwnzw7lya-)
     │                                       │
     │  GET /api/auth/google/login           │
     │ ───────────────────────────────►      │  (one container, but
     │                                       │   the cookie set here
     │  Set-Cookie: yt_oauth_state=N1        │   only fires on host A)
     │ ◄─────────────────────────────── 302  │
     │  Location: accounts.google.com        │
     │                                       │
     │  GET /api/auth/google/callback?state=N1│
     │ ────────────────────────────────────► │  cookie jar for host B
     │  (no yt_oauth_state cookie sent)      │  is empty → expected != got
     │ ◄─────────────────────────── 400 mismatch
```

Cookies are bound to the host that set them. Two URLs = two cookie
jars. The same-container model misleads everyone (laptop dev, the
agent, even the docs) into thinking the hosts are interchangeable.
They aren't, for any cookie-shaped state.

## The fix

`web-next/middleware.ts` runs an **edge-middleware host
normalization** layer ahead of every other concern, including the
auth gate:

```ts
const CANONICAL_HOST = process.env.YTFACTORY_CANONICAL_HOST?.trim();

export function middleware(req) {
  if (CANONICAL_HOST) {
    const incomingHost = req.headers.get("host") ?? req.nextUrl.host;
    if (incomingHost && incomingHost !== CANONICAL_HOST) {
      const url = req.nextUrl.clone();
      url.host = CANONICAL_HOST;
      url.protocol = "https:";
      url.port = "";
      return NextResponse.redirect(url, 308);
    }
  }
  // ...auth gate runs after.
}
```

Why **308** (not 302/301):

- `308 Permanent Redirect` preserves the original method **and body**.
  The auth flow itself is GET-only, but `/api/*` POSTs from a stale
  bookmark on the wrong host (form submits, mid-flight chat
  messages) need to ride through without losing their payload. `301`
  changes the method to GET on most clients (RFC 7231 §6.4.2's
  loophole); `302` does the same. `308` is unambiguous.

The matcher widened from the previous `/app/:path*` to a global
exclusion list (skip `_next/static`, `_next/image`, image / font
extensions), because the OAuth entry point is `/api/auth/google/login`
— it MUST run on the canonical host so the cookie lands in the same
jar Google's callback returns to. The auth gate path-checks
`/app/*` internally so behaviour outside `/app/*` is unchanged.

## Where the canonical host comes from

`cloud/web-next/deploy.sh` discovers it from
`gcloud run services describe ... --format='value(status.url)'`.
That field returns the project-id-hash form (the one Google's OAuth
client has registered), strips the scheme, and sets it as
`YTFACTORY_CANONICAL_HOST` on the service:

```bash
CANONICAL_HOST=$(gcloud run services describe "${SERVICE}" \
    --project="${PROJECT}" --region="${REGION}" \
    --format='value(status.url)' 2>/dev/null \
    | sed -E 's|^https?://||' || true)

# ... eventually wired into:
--set-env-vars="...|YTFACTORY_CANONICAL_HOST=${CANONICAL_HOST}"
```

First-deploy bootstrap: if the service does not yet exist
(`status.url` is empty), the env is omitted and the very next deploy
locks the host. Since OAuth registration with Google is manual on
day one anyway, this is a clean order of operations.

## Verifying in production

```bash
# Non-canonical host should 308 to canonical:
curl -sI https://ytfactory-web-next-283470729204.asia-southeast1.run.app/login \
  | grep -E "^HTTP|^location"
# Expected:
#   HTTP/2 308
#   location: https://ytfactory-web-next-7hwnzw7lya-as.a.run.app/login

# Canonical host should pass through:
curl -sI https://ytfactory-web-next-7hwnzw7lya-as.a.run.app/login \
  | head -1
# Expected: HTTP/2 200

# OAuth flow from non-canonical → 308 first, so the cookie always
# lands on the canonical host:
curl -sI https://ytfactory-web-next-283470729204.asia-southeast1.run.app/api/auth/google/login \
  | grep -E "^HTTP|^location"
# Expected: HTTP/2 308 → /api/auth/google/login on canonical host
```

## When this rule applies

Any Cloud Run service that:

- Sets cookies on at least one route, AND
- Has more than one route a user can land on (i.e. not a pure
  internal-API service with M2M-bearer auth), AND
- Is exposed publicly under both Cloud Run URLs.

In ytFactory today that's `ytfactory-web-next` (the Next.js public
face) and `ytfactory-web` (FastAPI orchestrator, which sets
`yt_session` + `yt_oauth_state`). The middleware-side fix lives in
the Next.js layer because that's the user-facing entry point;
ytfactory-web is reached via the Next.js proxy (`app/api/[...path]/
route.ts`) and inherits the canonical-host guarantee transparently.

If you add a third public Cloud Run service that sets cookies, port
this same `middleware.ts` pattern (or the FastAPI equivalent — a
single ASGI middleware that 308s on non-canonical host).

## Failure modes this prevents

| symptom                                 | root cause                                            |
|-----------------------------------------|-------------------------------------------------------|
| `{"error":"state mismatch"}` on OAuth   | CSRF cookie set on host A, callback on host B         |
| User signed in but `/app/*` says no     | `yt_session` set on host A, request to host B         |
| SSE stream drops mid-render             | session refresh on canonical host invalidates host B  |
| Form POST from a bookmark loses session | non-canonical-host POST → no cookie → 401 → drop body |

All four trace to the same root: cookies are host-bound, Cloud Run
gives every service two equivalent hosts, and the OAuth registration
points at one.

## Why we don't fix this client-side

Browser-side mitigation would require either (a) JS that refuses to
load the page on the non-canonical host (UX hostile, breaks if the
user pasted the URL deliberately), or (b) `Set-Cookie: Domain=…run.app`
to share the cookie across both hosts (Cloud Run blocks this — `.run.app`
is in the public-suffix list, browsers reject domain cookies on PSL
suffixes). Edge-side 308 is the only correct layer.

## See also

- Implementation: `web-next/middleware.ts` (host-normalization +
  auth-gate)
- Deployer: `cloud/web-next/deploy.sh` (auto-discovers + sets
  `YTFACTORY_CANONICAL_HOST`)
- FastAPI side of the OAuth flow: `web/server.py::google_login` /
  `google_callback`, `pipeline/auth/identity.py`
- Two-frontend layout this composes with: `docs/two_frontend_topology.md`
- Pre-build mechanic this rides on top of:
  `docs/cloudrun_web_next_prebuild.md`
- Memory: `feedback_cloudrun_dual_url_oauth_state_mismatch.md`
- Origin session post-mortem: 2026-05-12 (rohittomar@docx.co.in
  "state mismatch" → middleware fix shipped same session)
