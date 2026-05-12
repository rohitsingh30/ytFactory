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

export function middleware(req: NextRequest) {
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
  if (session) return NextResponse.next();

  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.searchParams.set("next", req.nextUrl.pathname + req.nextUrl.search);
  return NextResponse.redirect(url);
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
