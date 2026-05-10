"use client";

import { motion } from "framer-motion";
import {
  ArrowUpRight,
  AudioLines,
  CaptionsIcon,
  GitBranch,
  Image as ImageIcon,
  Sliders,
  Upload,
} from "lucide-react";

/**
 * Bento grid of capabilities. Linear/Vercel pattern — varied tile sizes,
 * each tile is its own micro-product-shot. Monochrome, single-line icons.
 */
export function FeatureBento() {
  return (
    <div className="mx-auto grid max-w-6xl gap-3 md:grid-cols-6 md:grid-rows-2">
      {/* Big tile: customization */}
      <Tile className="md:col-span-4 md:row-span-2 p-7">
        <Eyebrow icon={<Sliders className="h-3 w-3" />}>Customization</Eyebrow>
        <h3 className="mt-3 text-2xl font-medium tracking-tight">
          Every knob the channel exposes.
        </h3>
        <p className="mt-2 max-w-md text-sm text-muted-foreground">
          Voice, length, visual style, music bed, captions density, source URL,
          schedule. Override only what matters; the studio handles the rest.
        </p>

        {/* Faux knob panel */}
        <div className="mt-7 grid grid-cols-2 gap-2.5">
          {[
            { k: "voice", v: "sarah · documentary" },
            { k: "length_s", v: "55" },
            { k: "music_bed", v: "ambient · low" },
            { k: "captions", v: "high density · 3-line" },
            { k: "source", v: "wikipedia · auto-pull" },
            { k: "schedule_at", v: "2026-05-10 09:00 IST" },
          ].map((f) => (
            <div key={f.k} className="rounded-md border border-border bg-surface-2 px-3 py-2">
              <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-muted-foreground">
                {f.k}
              </div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-foreground">{f.v}</div>
            </div>
          ))}
        </div>
      </Tile>

      {/* Pipeline */}
      <Tile className="md:col-span-2 p-6">
        <Eyebrow icon={<GitBranch className="h-3 w-3" />}>Pipeline</Eyebrow>
        <h3 className="mt-3 text-base font-medium tracking-tight">
          One pipeline, eight templates.
        </h3>
        <p className="mt-2 text-xs text-muted-foreground">
          Rewrite → cast → images → TTS → ASR → compose → upload. Stage timeline
          streams to your library.
        </p>
      </Tile>

      {/* Voice */}
      <Tile className="md:col-span-2 p-6">
        <Eyebrow icon={<AudioLines className="h-3 w-3" />}>Voice</Eyebrow>
        <h3 className="mt-3 text-base font-medium tracking-tight">
          Cloned, cached, on tap.
        </h3>
        <p className="mt-2 text-xs text-muted-foreground">
          F5, Chatterbox, Kokoro on Cloud Run L4. ~10× faster than laptop with
          automatic local fallback.
        </p>
      </Tile>

      {/* Images */}
      <Tile className="md:col-span-2 p-6">
        <Eyebrow icon={<ImageIcon className="h-3 w-3" />}>Visuals</Eyebrow>
        <h3 className="mt-3 text-base font-medium tracking-tight">
          FLUX.2 klein, always warm.
        </h3>
        <p className="mt-2 text-xs text-muted-foreground">
          Per-channel style prompts on Cloud Run with min-instances=1. Render
          stalls fall back to local Z-Image-Turbo.
        </p>
      </Tile>

      {/* Captions */}
      <Tile className="md:col-span-2 p-6">
        <Eyebrow icon={<CaptionsIcon className="h-3 w-3" />}>Captions</Eyebrow>
        <h3 className="mt-3 text-base font-medium tracking-tight">
          ASR-locked. Word-perfect.
        </h3>
        <p className="mt-2 text-xs text-muted-foreground">
          Whisper-MLX timestamps every word; captions overlay at compose. No
          baked-in text in diffusion.
        </p>
      </Tile>

      {/* Publish */}
      <Tile className="md:col-span-2 p-6">
        <Eyebrow icon={<Upload className="h-3 w-3" />}>Publish</Eyebrow>
        <h3 className="mt-3 text-base font-medium tracking-tight">
          API first. Studio fallback.
        </h3>
        <p className="mt-2 text-xs text-muted-foreground">
          When YouTube&apos;s 10K-unit quota burns through, Playwright drives Studio
          for unattended uploads. Same channel, no wait.
        </p>
      </Tile>
    </div>
  );
}

function Tile({ className = "", children }: { className?: string; children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-50px" }}
      transition={{ duration: 0.35, ease: "easeOut" }}
      className={`group relative overflow-hidden rounded-xl border border-border bg-surface transition-colors hover:border-border-strong ${className}`}
    >
      {children}
      <ArrowUpRight className="absolute right-4 top-4 h-3.5 w-3.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
    </motion.div>
  );
}

function Eyebrow({ children, icon }: { children: React.ReactNode; icon: React.ReactNode }) {
  return (
    <div className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-2 px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.15em] text-muted-foreground">
      {icon}
      {children}
    </div>
  );
}
