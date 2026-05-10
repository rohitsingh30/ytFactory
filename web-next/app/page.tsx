import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { StudioMockup } from "@/components/marketing/studio-mockup";
import { ChannelBand } from "@/components/marketing/channel-band";
import { FeatureBento } from "@/components/marketing/feature-bento";
import { HowItWorks } from "@/components/marketing/how-it-works";
import { StatsBand } from "@/components/marketing/stats-band";

export default function LandingPage() {
  return (
    <main className="relative">
      {/* Top nav */}
      <header className="sticky top-0 z-30 border-b border-border/60 bg-background/70 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-6">
          <Link
            href="/"
            className="flex items-center gap-2 text-[13px] font-medium tracking-tight text-foreground"
          >
            <span className="grid h-6 w-6 place-items-center rounded-md border border-border bg-surface font-mono text-[10px]">
              yt
            </span>
            ytFactory
          </Link>

          <nav className="hidden items-center gap-7 text-[13px] text-muted-foreground md:flex">
            <a href="#product" className="hover:text-foreground">Product</a>
            <a href="#how" className="hover:text-foreground">How it works</a>
            <a href="#channels" className="hover:text-foreground">Channels</a>
            <a href="#stats" className="hover:text-foreground">Stats</a>
          </nav>

          <div className="flex items-center gap-2">
            <Button asChild variant="ghost" size="sm">
              <Link href="/app">Open studio</Link>
            </Button>
            <Button asChild size="sm">
              <Link href="/app/create">
                Create
                <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="relative px-6 pt-24 md:pt-32">
        <div className="mx-auto max-w-5xl text-center">
          <div className="mx-auto mb-7 inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            New · v1 of the studio is live
          </div>
          <h1 className="text-balance text-[44px] font-medium leading-[1.02] tracking-tightest md:text-[72px] lg:text-[80px]">
            <span className="display-gradient">An AI studio</span>
            <br />
            for YouTube Shorts.
          </h1>
          <p className="mx-auto mt-7 max-w-xl text-pretty text-[15px] leading-relaxed text-muted-foreground md:text-[17px]">
            Describe a Short. ytFactory writes the script, casts the voice,
            generates the visuals, cuts the captions, and ships it. Eight live
            channels, every knob you need.
          </p>
          <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
            <Button asChild size="lg">
              <Link href="/app/create">
                Create a Short
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
            <Button asChild variant="outline" size="lg">
              <Link href="/app">View the studio</Link>
            </Button>
          </div>
          <p className="mx-auto mt-5 font-mono text-[11px] uppercase tracking-[0.18em] text-muted-foreground/80">
            no signup · single operator · runs on your GCP
          </p>
        </div>

        {/* Mockup */}
        <div className="mx-auto mt-20 max-w-6xl px-2">
          <StudioMockup />
        </div>
      </section>

      {/* Trust band */}
      <section id="channels" className="px-6 pt-28">
        <ChannelBand />
      </section>

      {/* Hairline */}
      <div className="mx-auto mt-24 max-w-6xl px-6">
        <div className="hairline" />
      </div>

      {/* Bento */}
      <section id="product" className="px-6 py-24">
        <div className="mx-auto max-w-3xl px-6 text-center">
          <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Product
          </div>
          <h2 className="mt-3 text-balance text-3xl font-medium tracking-tight md:text-5xl">
            Built for the work, not the demo.
          </h2>
          <p className="mt-4 text-balance text-[15px] text-muted-foreground">
            Every part of a Short — script, voice, visuals, captions, publish — is
            a knob, a stage, a fallback. No black boxes.
          </p>
        </div>

        <div className="mt-14 px-2">
          <FeatureBento />
        </div>
      </section>

      {/* Hairline */}
      <div className="mx-auto max-w-6xl px-6">
        <div className="hairline" />
      </div>

      {/* How it works */}
      <section id="how" className="px-6 py-24">
        <div className="mx-auto max-w-3xl px-6 text-center">
          <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Workflow
          </div>
          <h2 className="mt-3 text-balance text-3xl font-medium tracking-tight md:text-5xl">
            From idea to published in three clicks.
          </h2>
        </div>

        <div className="mt-14 px-2">
          <HowItWorks />
        </div>
      </section>

      {/* Stats */}
      <section id="stats" className="px-6 py-24">
        <div className="mx-auto max-w-3xl px-6 text-center">
          <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Live
          </div>
          <h2 className="mt-3 text-balance text-3xl font-medium tracking-tight md:text-5xl">
            Real channels. Real numbers.
          </h2>
        </div>

        <div className="mt-14 px-2">
          <StatsBand />
        </div>
      </section>

      {/* CTA */}
      <section className="px-6 pb-32 pt-12">
        <div className="mx-auto max-w-4xl rounded-2xl border border-border bg-surface p-12 text-center md:p-16">
          <h3 className="text-balance text-3xl font-medium tracking-tight md:text-5xl">
            Open the studio.
          </h3>
          <p className="mx-auto mt-4 max-w-md text-[15px] text-muted-foreground">
            Pick a channel, customize the knobs, hit render.
          </p>
          <div className="mt-9 flex justify-center gap-3">
            <Button asChild size="lg">
              <Link href="/app/create">
                Create a Short
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
            <Button asChild variant="outline" size="lg">
              <Link href="/app">Open studio</Link>
            </Button>
          </div>
        </div>
      </section>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-4 px-6 py-8 text-xs text-muted-foreground md:flex-row md:items-center">
          <div className="flex items-center gap-2">
            <span className="grid h-5 w-5 place-items-center rounded-md border border-border bg-surface font-mono text-[9px]">
              yt
            </span>
            ytFactory · {new Date().getFullYear()}
          </div>
          <div className="flex items-center gap-6 font-mono uppercase tracking-[0.16em]">
            <Link href="/app" className="hover:text-foreground">studio</Link>
            <Link href="/app/channels" className="hover:text-foreground">channels</Link>
          </div>
        </div>
      </footer>
    </main>
  );
}
