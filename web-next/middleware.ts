/**
 * Edge middleware — host normalization + auth gate for /app/*.
 *
 * Two responsibilities, in order:
 *
 * 1. **Host normalization.** Cloud Run hands every service two public
 *    URLs (the project-id-hash form like
 *    `ytfactory-web-next-7hwnzw7lya-as.a.run.app` and the
 *    project-number form like
 *    `ytfactory-web-next-283470729204.asia-southeast1.run.app`). Both
 *    reach the same container, but cookies are host-bound — a cookie
 *    set on host A is not sent on a request to host B. Our Google
 *    OAuth flow stores a CSRF state nonce in `yt_oauth_state` cookie
 *    on the host the user clicked "Sign in" from, but Google's
 *    callback always lands on the SINGLE registered `redirect_uri`
 *    host. If a user starts on the non-canonical host, the cookie
 *    never reaches the callback and they see `{"error":"state mismatch"}`.
 *    We force every request onto the canonical host (configured via
 *    `YTFACTORY_CANONICAL_HOST`) BEFORE any cookie work happens.
 *
 * 2. **Auth gate for `/app/*`.** Anonymous users hitting `/app/*`
 *    get redirected to `/login`. (Earlier we added a global
 *    "no-store" response header rewrite to bust GFE caching; that
 *    broke React streaming SSR on Cloud Run by interrupting the
 *    response body stream. Cache busting is now done via
 *    `next.config.mjs` headers instead.)
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const CANONICAL_HOST = process.env.YTFACTORY_CANONICAL_HOST?.trim();

export async function middleware(req: NextRequest) {
  // 1. Host normalization (always-on when configured).
  if (CANONICAL_HOST) {
    const incomingHost = req.headers.get("host") ?? req.nextUrl.host;
    if (incomingHost && incomingHost !== CANONICAL_HOST) {
      const url = req.nextUrl.clone();
      url.host = CANONICAL_HOST;
      url.protocol = "https:";
      url.port = "";
      // 308 preserves method + body — important for POSTs caught
      // mid-flight on the wrong host (e.g. SSE streams, form posts).
      return NextResponse.redirect(url, 308);
    }
  }

  // 2. Auth gate for /app/* only. Skip everything else early so the
  // matcher's broader scope (needed for host normalization) doesn't
  // change behaviour outside the studio.
  if (!req.nextUrl.pathname.startsWith("/app")) {
    return NextResponse.next();
  }

  const enabled = process.env.YT_AUTH_ENABLED === "1";
  if (!enabled) return NextResponse.next();

  const session = req.cookies.get("yt_session")?.value;
  // Audit S1.25 — verify cookie validity, not just presence. Pre-fix
  // any attacker sending ``Cookie: yt_session=anything`` bypassed
  // this edge gate (backend still validated, but SSR pages would
  // render before the API rejected the call). We re-implement the
  // HMAC scheme from pipeline/auth/identity.py here at the edge so
  // unauthenticated requests get redirected to /login BEFORE any
  // SSR happens.
  if (session && (await verifySessionCookie(session))) {
    return NextResponse.next();
  }

  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.searchParams.set("next", req.nextUrl.pathname + req.nextUrl.search);
  return NextResponse.redirect(url);
}


/**
 * Edge-side HMAC verifier matching the cookie format produced by
 * `pipeline/auth/identity.py::sign_session`:
 *
 *   `<email>|<issued_unix_ts>|<base64url_no_pad_hmac_sha256_sig>`
 *
 * Returns true iff:
 *   - the cookie has exactly 3 `|`-separated segments,
 *   - the HMAC-SHA256(secret, "email|issued") matches the trailing sig
 *     in constant time, and
 *   - the issued timestamp is within YT_SESSION_TTL_S (default 7 days).
 *
 * Returns false on any failure (so the caller redirects to /login).
 *
 * Secret comes from `YTFACTORY_SESSION_SECRET` — the SAME env the
 * backend uses. If the env is absent (deploy misconfig) we fail
 * closed by returning false → redirect to /login. The backend then
 * returns 503 for the API call and the operator sees a clear error.
 */
async function verifySessionCookie(cookie: string): Promise<boolean> {
  const parts = cookie.split("|");
  if (parts.length !== 3) {
    console.warn(`[mw] reject: parts=${parts.length} (expected 3) cookie_len=${cookie.length}`);
    return false;
  }
  const [email, issuedStr, sigB64] = parts;
  if (!email || !issuedStr || !sigB64) {
    console.warn(`[mw] reject: empty part email=${!!email} issued=${!!issuedStr} sig=${!!sigB64}`);
    return false;
  }
  const issued = Number.parseInt(issuedStr, 10);
  if (!Number.isFinite(issued)) {
    console.warn(`[mw] reject: non-numeric issued=${issuedStr}`);
    return false;
  }
  const ttlSec = Number.parseInt(
    process.env.YT_SESSION_TTL_S ?? "604800", 10,
  );
  const ageSec = Date.now() / 1000 - issued;
  if (ageSec > ttlSec) {
    console.warn(`[mw] reject: expired age_s=${ageSec.toFixed(0)} ttl_s=${ttlSec}`);
    return false;
  }

  const secret = process.env.YTFACTORY_SESSION_SECRET;
  if (!secret) {
    console.warn(`[mw] reject: no YTFACTORY_SESSION_SECRET env`);
    return false;
  }
  try {
    const enc = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw",
      enc.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const expectedRaw = await crypto.subtle.sign(
      "HMAC",
      key,
      enc.encode(`${email}|${issuedStr}`),
    );
    const expected = base64UrlEncodeNoPad(new Uint8Array(expectedRaw));
    const ok = constantTimeEqual(expected, sigB64);
    if (!ok) {
      // SAFE truncation: print only sig length + first/last 4 chars.
      // Email + timestamp aren't secrets, fine to log in full so the
      // operator can correlate against the backend's signed cookie.
      console.warn(
        `[mw] reject: hmac mismatch email=${email} issued=${issuedStr} ` +
        `secret_len=${secret.length} expected=${expected.slice(0,4)}..${expected.slice(-4)} ` +
        `cookie=${sigB64.slice(0,4)}..${sigB64.slice(-4)} sig_lens=${expected.length}/${sigB64.length}`,
      );
    }
    return ok;
  } catch (e) {
    console.warn(`[mw] reject: subtle-crypto error ${String(e)}`);
    return false;
  }
}


function base64UrlEncodeNoPad(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}


function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export const config = {
  // Run on every request EXCEPT Next internals + static asset
  // extensions. The /app/* auth gate is path-checked inside the
  // function — broadening the matcher is required so host
  // normalization fires on /api/auth/google/login (the OAuth entry
  // point that sets the CSRF cookie) and on the /login page itself.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|js|css|woff|woff2|ttf|map)$).*)",
  ],
};
