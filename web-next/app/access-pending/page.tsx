"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Hourglass, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";

interface WhoAmI {
  signed_in: boolean;
  email?: string;
  name?: string;
  picture?: string;
  status?: string;
}

export default function AccessPendingPage() {
  const router = useRouter();
  const [me, setMe] = useState<WhoAmI | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    async function check() {
      try {
        const r = await api.get<WhoAmI>("/api/auth/whoami");
        if (cancelled) return;
        setMe(r);
        if (!r.signed_in) {
          router.replace("/login");
          return;
        }
        if (r.status === "approved") {
          router.replace("/app");
          return;
        }
        if (r.status === "denied") {
          router.replace("/login?error=denied");
          return;
        }
      } catch {
        // transient — keep showing pending state
      }
    }
    check();
    const id = setInterval(() => setTick((n) => n + 1), 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [router, tick]);

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
          <div className="grid h-10 w-10 place-items-center rounded-md border border-border bg-surface-2 text-amber-400">
            <Hourglass className="h-4 w-4" />
          </div>
          <div className="mt-4 font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Access pending
          </div>
          <h1 className="mt-2 text-[20px] font-medium tracking-tight">
            Waiting on approval
          </h1>
          <p className="mt-2 text-[12.5px] leading-relaxed text-muted-foreground">
            We received your request from{" "}
            <span className="font-mono text-foreground">
              {me?.email ?? "your account"}
            </span>
            . An admin will review and approve. This page checks every 30
            seconds &mdash; you&rsquo;ll be redirected automatically.
          </p>

          <div className="mt-6 flex items-center gap-2 text-[11px] text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            checking…
          </div>

          <form action="/api/auth/logout" method="post" className="mt-6">
            <Button
              type="submit"
              variant="outline"
              size="sm"
              className="w-full"
            >
              Sign out
            </Button>
          </form>
        </div>

        <div className="mt-5 text-center font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          we&rsquo;ll let you in shortly
        </div>
      </div>
    </main>
  );
}
