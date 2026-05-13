"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";

// `dynamic` exports must live in a server component. The /login/layout.tsx
// next to this file carries `export const dynamic = "force-dynamic"`
// so the HTML shell is rendered fresh on every request.

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginInner />
    </Suspense>
  );
}

function LoginInner() {
  const sp = useSearchParams();
  const errorParam = sp.get("error");

  // **2026-05-13 simplification.** Pre-fix this page rendered a
  // "checking session…" spinner that fetched /api/auth/whoami and
  // either router.replace()'d to /app or revealed the Sign-in button
  // based on the response. Three failure modes traced over the last
  // 2h to that flow:
  //
  //   1. Browser cached the HTML shell with old chunk hashes; the
  //      JS that drives the fetch never ran → spinner forever.
  //   2. Service worker (now retired, public/sw.js is a self-destruct
  //      kill-switch) intercepted the fetch and served stale.
  //   3. Auth-loop: middleware lacked YTFACTORY_SESSION_SECRET, so a
  //      valid cookie failed verification at the edge → redirect to
  //      /login → /login's whoami succeeded → router.replace("/app")
  //      → middleware bounced again → infinite loop visible only as
  //      the spinner re-mounting.
  //
  // The right architecture is "let the middleware decide". The
  // middleware on /app/* already redirects unauthenticated users to
  // /login. /login itself does NOT need to ping whoami — if the
  // user is already authenticated, the chrome-side click on
  // "Continue with Google" is a no-op (OAuth round-trip ends at
  // /api/auth/google/callback which sets the cookie + redirects to
  // /app). If the user IS authenticated and lands here by typing
  // the URL, the "Continue with Google" link still goes to the
  // OAuth start URL, which the backend's
  // pipeline.auth.routes.start_login() short-circuits to a redirect
  // to /app for already-authenticated users (existing behaviour;
  // see web/server.py).
  //
  // Net: no client-side state, no fetch, no spinner. The page
  // always shows "Continue with Google" instantly.

  return (
    <main className="relative flex min-h-screen items-center justify-center px-6">
      <div aria-hidden className="dot-grid pointer-events-none absolute inset-0 -z-10" />
      <div className="w-full max-w-sm">
        <Link
          href="/"
          className="mb-10 flex items-center justify-center gap-2 text-[13px] font-medium tracking-tight text-foreground"
        >
          <span className="grid h-6 w-6 place-items-center rounded-md border border-border bg-surface font-mono text-[10px]">
            yt
          </span>
          ytFactory
        </Link>

        <div className="rounded-xl border border-border bg-surface p-6">
          <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Sign in
          </div>
          <h1 className="mt-2 text-[20px] font-medium tracking-tight">
            Continue with Google
          </h1>
          <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">
            Access is invite-only. If you don&rsquo;t have an approved account, you&rsquo;ll
            see a &ldquo;request access&rdquo; page after sign-in.
          </p>

          <div className="mt-6">
            <Button asChild className="w-full">
              <a href="/api/auth/google/login">
                <GoogleGlyph className="mr-2 h-4 w-4" />
                Sign in with Google
              </a>
            </Button>
          </div>

          <div className="mt-3 text-center">
            <a
              href="/app"
              className="text-[11px] text-muted-foreground hover:text-foreground"
            >
              Already signed in? Open studio →
            </a>
          </div>

          {errorParam && (
            <div className="mt-4 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-[11px] text-destructive">
              {errorParam === "denied"
                ? "Your access request was denied."
                : errorParam === "auth_storage_permission"
                ? "Sign-in temporarily unavailable: the server can't reach the user store. The on-call has been alerted; please retry in a few minutes."
                : errorParam === "auth_storage_unavailable"
                ? "Sign-in temporarily unavailable: the user store is unreachable. Please retry in a few minutes."
                : `Sign-in failed: ${errorParam}`}
            </div>
          )}
        </div>

        <div className="mt-5 text-center font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          single-tenant · self-hosted
        </div>
      </div>
    </main>
  );
}

function GoogleGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 48 48" aria-hidden className={className}>
      <path
        fill="#FFC107"
        d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3 0 5.8 1.1 7.9 3l5.7-5.7C34 6.5 29.3 4.5 24 4.5 13.2 4.5 4.5 13.2 4.5 24S13.2 43.5 24 43.5 43.5 34.8 43.5 24c0-1.2-.1-2.4-.4-3.5z"
      />
      <path
        fill="#FF3D00"
        d="M6.3 14.7l6.6 4.8C14.7 15.4 19 12 24 12c3 0 5.8 1.1 7.9 3l5.7-5.7C34 6.5 29.3 4.5 24 4.5 16.4 4.5 9.8 8.7 6.3 14.7z"
      />
      <path
        fill="#4CAF50"
        d="M24 43.5c5.2 0 9.9-2 13.4-5.2l-6.2-5.2c-2 1.4-4.5 2.2-7.2 2.2-5.3 0-9.7-3.4-11.3-8L6 32.2C9.4 38.4 16.1 43.5 24 43.5z"
      />
      <path
        fill="#1976D2"
        d="M43.6 20.5H42V20H24v8h11.3c-.8 2.3-2.3 4.3-4.1 5.6l6.2 5.2c-.4.4 6.6-4.8 6.6-14.8 0-1.2-.1-2.4-.4-3.5z"
      />
    </svg>
  );
}
