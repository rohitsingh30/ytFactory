import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center px-6 text-center">
      <div aria-hidden className="dot-grid pointer-events-none absolute inset-0 -z-10" />
      <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
        404
      </div>
      <h1 className="mt-3 text-balance text-[36px] font-medium leading-[1.05] tracking-tight md:text-[56px]">
        <span className="display-gradient">Lost in the studio.</span>
      </h1>
      <p className="mt-4 max-w-md text-[14px] text-muted-foreground">
        That page doesn&apos;t exist. Maybe it was a render that was never created.
      </p>
      <div className="mt-7 flex gap-2">
        <Button asChild>
          <Link href="/app">
            <ArrowLeft className="h-3.5 w-3.5" />
            Open studio
          </Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/">Back to landing</Link>
        </Button>
      </div>
    </main>
  );
}
