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
import Link from "next/link";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function LoginLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  if (process.env.YTFACTORY_FRONTEND_ONLY === "1") {
    return (
      <main className="flex min-h-screen items-center justify-center px-6">
        <div className="w-full max-w-sm rounded-xl border border-border bg-surface p-6">
          <p className="font-mono text-xs text-muted-foreground">ytFactory</p>
          <h1 className="mt-3 text-xl font-medium">The website is online</h1>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            Sign-in and video creation will be available once the API is connected.
          </p>
          <Link href="/" className="mt-6 inline-block text-sm underline underline-offset-4">
            Back to the website
          </Link>
        </div>
      </main>
    );
  }
  return children;
}
