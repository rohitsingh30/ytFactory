/**
 * Login route layout — forces dynamic rendering.
 *
 * Pre-2026-05-13 the /login HTML shell was statically generated and
 * cached for one year (`s-maxage=31536000` from Next.js's default
 * static-shell behaviour). That worked fine until the next deploy
 * shipped new chunk hashes — browsers with cached HTML would request
 * `/_next/static/chunks/app/login/page-<OLD_HASH>.js`, get a 404,
 * and the inline JavaScript that drives `useEffect` (and clears the
 * "checking session…" spinner) would never execute. Result: every
 * returning user got stuck on a forever-spinner until they manually
 * cleared site data.
 *
 * Marking this layout `force-dynamic` opts the entire `/login`
 * subtree out of the static-shell cache. The HTML is rendered fresh
 * on every request and always references the chunk hashes that
 * actually exist on the currently-deployed image.
 *
 * Cost: minimal. The login page is server-rendered HTML + a tiny
 * client-side bundle; recomputing it per request is sub-millisecond
 * on the existing Node.js runtime. The static-shell win it gives
 * up (CDN edge cache hits) is irrelevant for a page that's hit
 * once per signed-out user per session.
 */
export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function LoginLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return children;
}
