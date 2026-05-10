"use client";

import { useEffect } from "react";
import Link from "next/link";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Global error boundary — caught for any uncaught render error in the
 * App Router tree. Linear-style: subtle, mono, recoverable.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface to console for the user; production should ship to Sentry/etc.
    console.error(error);
  }, [error]);

  return (
    <html lang="en" className="dark">
      <body className="bg-background text-foreground">
        <main className="flex min-h-screen flex-col items-center justify-center px-6 text-center">
          <div className="grid h-9 w-9 place-items-center rounded-md border border-border bg-surface text-amber-300">
            <AlertTriangle className="h-4 w-4" />
          </div>
          <div className="mt-5 font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Error
          </div>
          <h1 className="mt-2 text-[28px] font-medium tracking-tight">
            Something broke in the studio.
          </h1>
          <p className="mt-3 max-w-md text-[13px] text-muted-foreground">
            {error.message || "An unexpected error occurred."}
            {error.digest && (
              <span className="ml-1 font-mono text-[11px] text-muted-foreground/70">
                · {error.digest}
              </span>
            )}
          </p>
          <div className="mt-7 flex gap-2">
            <Button onClick={() => reset()}>
              <RotateCcw className="h-3.5 w-3.5" />
              Try again
            </Button>
            <Button asChild variant="outline">
              <Link href="/app">Back to studio</Link>
            </Button>
          </div>
        </main>
      </body>
    </html>
  );
}
