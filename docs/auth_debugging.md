# Auth bug debugging — env-diff first, then verifier logs, then frontend

> **Meta-rule (WORKFLOW-IMPROVEMENT, 2026-05-13).** Born from a
> two-day debugging saga where four speculative frontend fixes
> shipped before the actual root cause (a cookie-quote serialization
> mismatch — see [`web_next_session_cookie.md`](./web_next_session_cookie.md))
> got diagnosed. The wrong order of investigation cost ~5 hours of
> agent time + the user's patience. This doc is the prescribed
> investigation order so the next agent doesn't repeat it.

## When this applies

Any auth-related symptom that fits the shape "user reports the
dashboard isn't behaving correctly after sign-in":

* "stuck on the login page"
* "redirect loop"
* "I sign in but I'm still not signed in"
* "works in incognito but not in my regular browser"
* "the dashboard shows me as logged out even though I just logged in"

## Investigation order — DON'T deviate

### Step 1. Diff env vars between EVERY auth-touching service

Two services touch auth in this codebase:

* `web/server.py` (control plane / `ytfactory-web` Cloud Run service)
  signs cookies and serves OAuth callback.
* `web-next/middleware.ts` (`ytfactory-web-next` Cloud Run service)
  verifies cookies at the edge.

Both MUST have an identical `YTFACTORY_SESSION_SECRET`. Any
asymmetry is the root cause until proven otherwise. Run:

```bash
ADC=$(gcloud auth application-default print-access-token)
TOKEN=$(curl -sS -X POST -H "Authorization: Bearer $ADC" \
  -H "Content-Type: application/json" \
  --data '{"scope":["https://www.googleapis.com/auth/cloud-platform"],"lifetime":"3600s"}' \
  "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/ytfactory-deployer@ytfactory-prod-v2.iam.gserviceaccount.com:generateAccessToken" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['accessToken'])")

for svc in ytfactory-web ytfactory-web-next; do
  echo "=== $svc ==="
  CLOUDSDK_AUTH_ACCESS_TOKEN=$TOKEN gcloud run services describe $svc \
    --region=asia-southeast1 --project=ytfactory-prod-v2 --format=json \
  | python3 -c "
import json, sys
env = json.load(sys.stdin)['spec']['template']['spec']['containers'][0].get('env', [])
for e in env:
    n = e['name']
    if any(k in n for k in ('SECRET','KEY','TOKEN','PIN','AUTH','OAUTH','SESSION','CANONICAL')):
        v = e.get('value','') or e.get('valueFrom',{}).get('secretKeyRef',{}).get('name','<from-secret>')
        print(f'  {n}: {v}')
"
done
```

If any auth-related env is asymmetric (set on one, missing on
another, OR pointing at different secret names), STOP. Fix the env
first, deploy, retest. **Do not touch cache, SW, or chunks until
this asymmetry is resolved.**

The 2026-05-13 saga's first hour-long detour was caused by skipping
this step — `web-next` was missing `YTFACTORY_SESSION_SECRET`
entirely, making the middleware fail-closed on every valid cookie.
A 30-second env diff would have surfaced it.

### Step 2. Trust nothing — add a reject logger to the verifier

If env is symmetric AND the cookie still gets rejected, the verifier
itself is the next suspect. Pre-2026-05-13 the
`web-next/middleware.ts::verifySessionCookie` returned `false`
silently on every failure mode (parts count, TTL exceeded, missing
secret, HMAC mismatch). That was the second bug class — silent
fail-closed gives zero diagnostic signal.

Every reject branch in any auth verifier MUST log:

```ts
console.warn(
  `[mw] reject: <reason> ` +
  `<context fields the operator can correlate>`,
);
```

Truncate sig to first/last 4 chars (`sig.slice(0,4)+'..'+sig.slice(-4)`).
NEVER log the full sig. Email + timestamp + secret length are
non-secrets and must be in the line so the operator can correlate
against what the backend signed. See `web-next/middleware.ts`
(commit `5c58062`) for the reference implementation.

Cloud Logging picks these up automatically. To read them after the
user reproduces:

```bash
ADC=$(gcloud auth application-default print-access-token)
TOKEN=$(curl -sS -X POST -H "Authorization: Bearer $ADC" \
  -H "Content-Type: application/json" \
  --data '{"scope":["https://www.googleapis.com/auth/cloud-platform"],"lifetime":"3600s"}' \
  "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/ytfactory-deployer@ytfactory-prod-v2.iam.gserviceaccount.com:generateAccessToken" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['accessToken'])")

curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "resourceNames": ["projects/ytfactory-prod-v2"],
    "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"ytfactory-web-next\" AND textPayload:\"[mw] reject\"",
    "orderBy": "timestamp desc",
    "pageSize": 10
  }' \
  "https://logging.googleapis.com/v2/entries:list" \
| python3 -c "import json,sys
d=json.load(sys.stdin)
for e in d.get('entries',[])[:10]:
    print(e['timestamp'], e.get('textPayload','')[:300])
"
```

The reject reason tells you which check failed. If it's HMAC
mismatch, look for surprise prefix/suffix bytes in the printed
email (wrapping `"` is the 2026-05-13 case; URL-encoded `%xx` is
another common case).

### Step 3. Reproduce with a self-signed cookie

If the verifier rejects every browser cookie but you're not sure
why, sign a fresh cookie yourself with the secret and send it via
curl. If the verifier ACCEPTS your self-signed cookie but REJECTS
the browser's real cookie, the bug is in cookie SERIALIZATION
(Python's `http.cookies` quote behaviour, an OAuth callback bug, a
proxy stripping the cookie, …) — NOT in verification.

Recipe:

```bash
ADC=$(gcloud auth application-default print-access-token)
SECRET=$(curl -sS -H "Authorization: Bearer $ADC" \
  "https://secretmanager.googleapis.com/v1/projects/ytfactory-prod-v2/secrets/ytfactory-session-secret/versions/latest:access" \
| python3 -c "import json,sys,base64; d=json.load(sys.stdin); print(base64.b64decode(d['payload']['data']).decode().rstrip())")

python3 -c "
import base64, hashlib, hmac, time, urllib.request
secret = '$SECRET'
def sign(email, sec):
    issued = int(time.time())
    payload = f'{email}|{issued}'
    sig = hmac.new(sec.encode(), payload.encode(), hashlib.sha256).digest()
    return f'{payload}|' + base64.urlsafe_b64encode(sig).decode().rstrip('=')

cookie = sign('YOUR-TEST-EMAIL@example.com', secret)
print(f'cookie: {cookie[:50]}...{cookie[-12:]}')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, *a, **k): return None
    def http_error_307(self, *a, **k): return None
op = urllib.request.build_opener(NoRedirect)
req = urllib.request.Request(
    'https://ytfactory-web-next-7hwnzw7lya-as.a.run.app/app',
    headers={'Cookie': f'yt_session={cookie}', 'User-Agent': 'Mozilla/5.0'})
try:
    resp = op.open(req, timeout=10)
    print(f'status={resp.status} → MIDDLEWARE ACCEPTED self-signed cookie')
    print('Bug is in cookie SERIALIZATION, not verification.')
except urllib.error.HTTPError as e:
    print(f'status={e.code} location={e.headers.get(\"location\",\"\")} → MIDDLEWARE REJECTED self-signed cookie')
    print('Bug IS in verification — re-check secret bytes, HMAC formula, parts count.')
"
```

### Step 4. ONLY THEN look at frontend cache, service workers, chunk hashes

These are real edge cases that cause OTHER bugs (a stale browser
HTML cache referencing rotated chunk hashes WILL eat user traffic
after a deploy if `/login` is statically generated; a service worker
WILL intercept fetches with stale cached responses if it survives a
deploy boundary). But they are very rarely the cause of an auth
asymmetry. Steps 1-3 catch >90% of auth-loop reports in a fraction
of the time.

The 2026-05-13 saga shipped the cache + SW + chunk-hash hardening
BEFORE running steps 1-3. Each fix is net-positive (commits
`5f16b79`, `5e3e0d5`) but none addressed the symptom. Burned ~3
hours of investigation that the env-diff would have saved.

## Browser-only bugs need browser reproduction

If a bug only surfaces in a real browser (curl from the agent's
shell can't reproduce it), DO NOT keep speculating from the
server-side. Use one of these BEFORE shipping another fix:

1. **Add a `?debug=1` query param to the failing page** that runs
   the diagnostic checks (whoami fetch + middleware probe + cookie
   inspection) inline and renders the JSON result on the page. The
   user pastes the JSON back to the agent. Reference impl:
   `web-next/app/login/page.tsx::DebugPanel` (commit `9c9a5ce`).

2. **Reproduce in playwright with the user's real Chrome profile.**
   The 2026-05-13 attempt didn't work because Chrome cookies are
   encrypted with macOS Keychain — playwright Chrome can't decrypt
   them without unlocking the keychain. The `?debug=1` panel is
   therefore the more reliable path.

3. **Read Cloud Logging for the verifier reject lines** — only
   useful if step 2 of the investigation order (logger) was done
   first.

## Affected services

Currently only `ytfactory-web-next` (the dashboard SSR/edge service)
verifies cookies non-trivially. The `ytfactory-web` backend control
plane verifies via Python. If a third service ever joins the auth
mesh (e.g. a separate API gateway), this doc must be updated to
include it in the env-diff loop.

## Cross-references

* [`docs/web_next_session_cookie.md`](./web_next_session_cookie.md)
  — the cookie-quotes trap that motivated this doc
* [`docs/cloudrun_dual_url_host_normalization.md`](./cloudrun_dual_url_host_normalization.md)
  — earlier auth fix on the same code path
* `web-next/middleware.ts` — the verifier with the diagnostic logger
* `web/server.py::_set_session_cookie` — the Python set-cookie call
* `pipeline/auth/identity.py` — HMAC source of truth
* `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_auth_loop_check_env_first.md`
  — memory pointer
* `~/.claude/projects/-Users-rohit-ytFactory/memory/feedback_cookie_python_quotes_trap.md`
  — memory pointer for the underlying bug
