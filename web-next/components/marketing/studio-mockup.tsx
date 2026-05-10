"use client";

import { motion } from "framer-motion";
import { ArrowRight, Check, CircleDot, Loader2, Mic2, Sparkles, Wand2 } from "lucide-react";

/**
 * High-fidelity faux screenshot of the studio. Used as the hero anchor.
 * Replaces decorative gradients with something that LOOKS like a real product.
 *
 * Tone reference: Linear / Vercel / v0 product shots — restrained,
 * tight typography, monochrome with a single accent dot.
 */
export function StudioMockup() {
  return (
    <div className="relative">
      {/* Dotted halo behind the frame */}
      <div aria-hidden className="dot-grid pointer-events-none absolute -inset-12 -z-10" />

      <motion.div
        initial={{ opacity: 0, y: 24 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: [0.2, 0.8, 0.2, 1] }}
        className="relative mx-auto w-full max-w-5xl overflow-hidden rounded-xl border border-border bg-surface shadow-[0_60px_120px_-30px_rgba(0,0,0,0.7)]"
      >
        {/* Browser-ish chrome */}
        <div className="flex items-center gap-2 border-b border-border bg-surface-2 px-3.5 py-2.5">
          <div className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-border-strong" />
            <span className="h-2.5 w-2.5 rounded-full bg-border-strong" />
            <span className="h-2.5 w-2.5 rounded-full bg-border-strong" />
          </div>
          <div className="ml-3 flex h-6 flex-1 items-center justify-center rounded-md border border-border bg-background px-3 text-[11px] font-mono text-muted-foreground">
            ytfactory.app/app/create
          </div>
        </div>

        {/* App body */}
        <div className="grid grid-cols-12 gap-0">
          {/* Left rail */}
          <div className="col-span-2 hidden flex-col gap-1 border-r border-border bg-surface px-2 py-3 md:flex">
            {[
              { l: "Dashboard", a: false },
              { l: "Create", a: true },
              { l: "Library", a: false },
              { l: "Queue", a: false },
              { l: "Channels", a: false },
              { l: "Settings", a: false },
            ].map((it) => (
              <div
                key={it.l}
                className={`rounded px-2 py-1.5 text-[11px] tracking-tight ${
                  it.a ? "bg-surface-2 text-foreground" : "text-muted-foreground"
                }`}
              >
                {it.l}
              </div>
            ))}
          </div>

          {/* Main */}
          <div className="col-span-12 md:col-span-7 border-r border-border">
            <div className="border-b border-border px-5 py-3.5 text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
              <span>Studio</span>
              <span className="px-1.5 text-muted-foreground/50">/</span>
              <span className="text-foreground">Create</span>
            </div>

            <div className="p-5">
              <div className="text-xs uppercase tracking-[0.18em] text-muted-foreground">Step 03</div>
              <div className="mt-1 text-lg font-medium tracking-tight">Customize the render</div>

              <div className="mt-5 grid grid-cols-2 gap-3">
                {[
                  { k: "Channel", v: "history-recapped" },
                  { k: "Template", v: "footage-only short" },
                  { k: "Voice", v: "documentary · sarah" },
                  { k: "Length", v: "55s" },
                ].map((f) => (
                  <div key={f.k} className="rounded-md border border-border bg-surface-2 p-3">
                    <div className="text-[10px] uppercase tracking-[0.15em] text-muted-foreground">
                      {f.k}
                    </div>
                    <div className="mt-1.5 truncate font-mono text-xs text-foreground">{f.v}</div>
                  </div>
                ))}
              </div>

              <div className="mt-4 rounded-md border border-border bg-surface-2 p-3">
                <div className="text-[10px] uppercase tracking-[0.15em] text-muted-foreground">Topic</div>
                <div className="mt-1.5 text-sm leading-relaxed text-foreground/90">
                  The 1962 Cuban Missile Crisis — thirteen days that nearly ended the world.
                </div>
              </div>

              <div className="mt-5 flex items-center justify-between">
                <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <CircleDot className="h-3 w-3 text-foreground" />
                  Step 03 of 04
                </div>
                <div className="flex items-center gap-2">
                  <span className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground">
                    Back
                  </span>
                  <span className="inline-flex items-center gap-1.5 rounded-md bg-foreground px-3 py-1.5 text-xs font-medium text-background">
                    Render <ArrowRight className="h-3.5 w-3.5" />
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Right inspector — pretend live render */}
          <div className="col-span-12 md:col-span-3 bg-surface">
            <div className="border-b border-border px-4 py-3.5 text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
              Live render
            </div>
            <div className="space-y-3 p-4">
              {[
                { l: "Rewrite", s: "done" },
                { l: "Cast", s: "done" },
                { l: "Images", s: "done" },
                { l: "TTS", s: "running" },
                { l: "ASR", s: "pending" },
                { l: "Compose", s: "pending" },
                { l: "Upload", s: "pending" },
              ].map((stage) => (
                <div key={stage.l} className="flex items-center gap-2.5 text-xs">
                  {stage.s === "done" && (
                    <Check className="h-3.5 w-3.5 text-emerald-400" />
                  )}
                  {stage.s === "running" && (
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-violet-300" />
                  )}
                  {stage.s === "pending" && (
                    <span className="h-1.5 w-1.5 rounded-full bg-border-strong" />
                  )}
                  <span
                    className={
                      stage.s === "pending"
                        ? "text-muted-foreground"
                        : stage.s === "running"
                          ? "text-foreground"
                          : "text-foreground/70"
                    }
                  >
                    {stage.l}
                  </span>
                  {stage.s === "running" && (
                    <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                      02m 14s
                    </span>
                  )}
                </div>
              ))}

              <div className="!mt-6 rounded-md border border-border bg-surface-2 p-3">
                <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.15em] text-muted-foreground">
                  <Sparkles className="h-3 w-3" />
                  Critic
                </div>
                <div className="mt-2 text-xs text-foreground/85">
                  Hook strong. Era lock {`{`}1962{`}`}. Captions density on track.
                </div>
              </div>
            </div>
          </div>
        </div>
      </motion.div>

      {/* Floating chips */}
      <FloatingChip className="hidden md:flex" delay={0.4} top="-top-3" left="left-8">
        <Wand2 className="h-3 w-3" />
        Author
      </FloatingChip>
      <FloatingChip className="hidden md:flex" delay={0.55} top="-top-3" left="right-10">
        <Mic2 className="h-3 w-3" />
        Voice
      </FloatingChip>
    </div>
  );
}

function FloatingChip({
  children,
  className = "",
  delay = 0,
  top = "-top-3",
  left = "left-8",
}: {
  children: React.ReactNode;
  className?: string;
  delay?: number;
  top?: string;
  left?: string;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.4 }}
      className={`absolute ${top} ${left} z-10 inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-[10px] font-mono uppercase tracking-[0.15em] text-muted-foreground ${className}`}
    >
      {children}
    </motion.div>
  );
}
