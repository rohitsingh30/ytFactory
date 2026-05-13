# `web-next` session-cookie verification — un-quote before HMAC

> **Cross-channel rule (CLASS-OF-BUG, 2026-05-13).** Any non-Python
> verifier of a Python-set cookie MUST strip wrapping double-quotes
> (`"…"`) from the cookie value before parsing. Python's
> `http.cookies.Morsel` auto-quotes any value containing characters
> outside its `_LegalChars` set — `@` is not in the set, so every
> session cookie embedding an email address gets serialized as
> `Set-Cookie: yt_session="email|issued|sig"`. Python's
> `SimpleCookie._unquote()` reverses this on read, so the backend
> never notices. JavaScript verifiers (Next.js `req.cookies.get().value`,
> Express raw cookie strings, anything that touches the unparsed
> Set-Cookie body) get the LITERAL quotes back and silently fail
> HMAC verification.

## What broke (2026-05-13 — two-day debugging saga)

User reported the `/login` page hung on "checking session…" and
later, after a frontend rewrite, on a redirect loop:
`website → click "create" → /login → sign in → callback → /
→ click "create" → /login → …`. The user never saw the studio.

`whoami` returned `signed_in:true, status:approved` — proving the
backend accepted the cookie. But every `GET /app/*` was 307'd back
to `/login` with no error visible to the user. The middleware was
silently rejecting valid cookies and the symptom was indistinguishable
from "user not logged in".

## Why it broke

The cookie set by `web/server.py::_set_session_cookie` calls into
`pipeline.auth.identity.sign_session(email)` which produces:

```
{email}|{issued_unix_ts}|{base64url_no_pad_hmac_sha256_sig}
```

`http.cookies.Morsel.OutputString()` then runs `_quote(value)` which
checks every byte against `_LegalChars`:

```python
_LegalChars = string.ascii_letters + string.digits + "!#$%&'*+-.^_`|~:"
```

The `@` byte (0x40) is not in this set. The whole cookie value gets
wrapped: `Set-Cookie: yt_session="rohittomar@docx.co.in|1778656100|sig"`.
The browser stores the literal octets per RFC 6265 §5.2.

When Python reads the cookie back via FastAPI's `request.cookies`
(which uses `http.cookies.SimpleCookie` under the hood),
`_unquote()` strips the wrapping `"`. Backend HMAC matches.

When `web-next/middleware.ts::verifySessionCookie` reads the same
cookie via `req.cookies.get("yt_session")?.value`, Next.js returns
the **raw** byte sequence including the literal quotes. The
verifier's `cookie.split("|")` returned:

```
['"rohittomar@docx.co.in', '1778656100', 'sig"']
```

HMAC was computed over `"rohittomar@docx.co.in|1778656100` (with
leading quote) which never matched what the backend signed. Every
request to `/app/*` was rejected. The user got the loop.

### Diagnostic that surfaced it

The middleware was rewritten (commit `5c58062`) to log every
rejection cause via `console.warn`. One trip through `/app` produced:

```
[mw] reject: hmac mismatch email="rohittomar@docx.co.in
issued=1778656100 secret_len=64 expected=Ile-..VRV0
cookie=Jm9f..gNo sig_lens=43/44
```

Two unmistakable tells:

* **Email starts with `"`** — the cookie value has a literal leading
  quote.
* **`sig_lens=43/44`** — the cookie's signature is one byte longer
  than expected, because the trailing `"` got captured into the last
  split part.

## Fix (commit `cdf454a`)

`web-next/middleware.ts::verifySessionCookie` now un-quotes before
parsing:

```ts
let cookie = rawCookie;
if (cookie.length >= 2 && cookie.startsWith('"') && cookie.endsWith('"')) {
  cookie = cookie.slice(1, -1);
}
const parts = cookie.split("|");
// … rest of HMAC verify unchanged
```

This matches the Python-side behaviour. RFC 6265 stores the literal
octets — any client-side quote handling must be done by the verifier,
not by the cookie store.

## Why this took two days

Symptom presentation steered the diagnosis wrong, three times in a
row:

1. **"Stuck checking session" spinner** → looked like a stale-HTML
   cache or chunk-hash mismatch. Shipped `force-dynamic` on
   `/login/layout.tsx`, a stale-shell guard in the root layout, and
   a service-worker self-destruct (commits `5f16b79` + `5e3e0d5`).
   All three are real hardening but none was the root cause.
2. **"Works in incognito but not in regular browser"** → looked like
   a cookie / session-secret mismatch. Discovered `web-next` was
   missing the `YTFACTORY_SESSION_SECRET` env binding, granted the
   secret to the runtime SA + bound it (revision `00048-f99`). Real
   bug, but only one of two — the cookie still failed verification
   AFTER binding the secret.
3. **`whoami` returns true, `/app` returns opaqueredirect** → only
   this asymmetry pointed at the verifier divergence. Took adding
   the `[mw] reject: …` logger to the middleware to prove which
   verifier rejected and on what specific check.

## Diagnostic rules (don't repeat)

When a cookie-based auth shows asymmetric behaviour — works in one
verifier, fails in another, both have the same secret — run these
checks BEFORE touching frontend cache or service workers:

1. **Diff env vars between every service that touches auth.**
   `gcloud run services describe` on each, grep for
   `SESSION|SECRET|AUTH|OAUTH`. Any asymmetry is the root cause
   until proven otherwise. See
   [`docs/auth_debugging.md`](./auth_debugging.md) §"Step 1".
2. **Add `console.warn` to every reject branch in the verifier.**
   Print the exact cookie bytes the failing verifier received,
   truncated to first/last 4 chars of the sig (NEVER log the full
   sig). The reject reason is what tells you whether it's TTL,
   parts count, secret missing, or HMAC mismatch.
3. **Check for surprise prefix/suffix bytes on the cookie value.**
   Wrapping `"` is the most common (Python `http.cookies` quote
   behaviour). URL-encoding `%XX` is the second-most common.
4. **Reproduce the failing verifier with a self-signed cookie.**
   Sign a cookie with the exact same secret using the exact same
   formula, send it via curl. If the verifier accepts your
   self-signed cookie but rejects the browser's real cookie, the
   bug is in cookie SERIALIZATION (this case), not verification.

## Affected users

Every user with `@` in their email — i.e. every Google-OAuth user
of ytFactory. The bug landed when `web-next` replaced the legacy
all-Python `web/server.py` for the dashboard. Any other service that
verifies Python-set cookies in non-Python code is potentially
affected.

## Hardening (commits that landed in the same investigation)

These shipped during the debugging detour. None was the root cause
but each addresses a real edge case worth keeping:

* `5f16b79` — `web-next/app/login/layout.tsx` carries
  `dynamic = "force-dynamic"`. The /login HTML shell is rendered
  fresh per-request and never cached, so a future Next.js chunk-hash
  rotation can't trap returning users.
* `5f16b79` — `web-next/components/stale-shell-guard.tsx` listens
  for `ChunkLoadError` / failed `<script>` loads on every page mount
  and forces ONE hard reload (idempotent via session-storage
  breadcrumb) to recover existing tabs that were trapped before the
  cache fix.
* `5e3e0d5` — `web-next/public/sw.js` is now a self-destruct
  service worker (skipWaiting + claim + wipe-every-cache +
  unregister + force-navigate every controlled tab). Original
  studio SW intercepted `/api/*` GETs with scope "/" and survived
  deploys; in some failure modes it served stale chunks. Removed
  proactively.
* `530d386` — `web-next/app/login/page.tsx` no longer fetches
  `/api/auth/whoami` on mount. The middleware on `/app/*` is the
  authoritative gate; the page just renders "Continue with Google"
  + "Already signed in? Open studio →" links. Eliminates the
  client-side state machine that was mis-diagnosed as a hang.
* `9c9a5ce` — `web-next/app/login/page.tsx` exposes a
  `?debug=1` panel that runs whoami + `/app` HEAD probes inline so
  a future stuck-login report can be triaged from one screenshot,
  no DevTools required.
* `5c58062` — `web-next/middleware.ts::verifySessionCookie` now
  logs every rejection cause to `console.warn`. Cloud Logging
  surfaces these with a `resource.labels.service_name` filter.

The combination is defensive: even if a NEW way of breaking the
cookie path surfaces, the logger pins the cause within one user
report.

## Cross-references

* `web/server.py::_set_session_cookie` — set-cookie call site
* `pipeline/auth/identity.py::sign_session` / `verify_session` —
  HMAC formula (Python source of truth)
* `web-next/middleware.ts::verifySessionCookie` — JS verifier (the
  fix landed here)
* [`docs/auth_debugging.md`](./auth_debugging.md) — the meta-rule for
  diagnosing auth bugs without going down the same rabbit hole
* [`docs/cloudrun_dual_url_host_normalization.md`](./cloudrun_dual_url_host_normalization.md)
  — earlier auth fix (host-normalization) on the same code path
* `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_cookie_python_quotes_trap.md`
  — terse memory pointer

## Test surface

There is no automated test pinning this fix today. Adding one would
require mocking the Python cookie-set + JS cookie-read round trip,
which crosses runtimes; the fix is small and the diagnostic logger
provides a runtime safety net. Filed as follow-up; not blocking.

## Cross-cutting follow-up

* Audit every cookie set by Python that's verified by non-Python
  code. Today only `yt_session` qualifies. If we add CSRF tokens or
  per-user feature flags as cookies, use the same un-quote prelude
  in the verifier.
* Consider switching `pipeline.auth.identity.sign_session` to
  base64-url-encode the entire payload BEFORE the HMAC pipe. That
  keeps the cookie value within `_LegalChars` for any email and
  makes this trap impossible to hit again.
