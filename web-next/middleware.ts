/**
 * Edge middleware — auth gate for /app/*.
 *
 * Anonymous users hitting /app/* get redirected to /login. The
 * matcher narrows the scope strictly to /app/* so we don't touch any
 * other route's response. (Earlier we added a global "no-store"
 * response header rewrite to bust GFE caching; that broke React
 * streaming SSR on Cloud Run by interrupting the response body
 * stream. Cache busting is now done via `next.config.mjs` headers
 * instead.)
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export function middleware(req: NextRequest) {
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
  matcher: ["/app/:path*"],
};
