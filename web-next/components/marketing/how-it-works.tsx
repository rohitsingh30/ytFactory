"use client";

import { motion } from "framer-motion";

const STEPS = [
  {
    n: "01",
    t: "Pick a template",
    d: "Eight battle-tested channels. Each one tuned for its niche — voice, length, visual grammar.",
  },
  {
    n: "02",
    t: "Customize the knobs",
    d: "Override the bits you care about. Topic, source, voice, length, music, captions, schedule.",
  },
  {
    n: "03",
    t: "Render & ship",
    d: "Watch the stage timeline live. Preview, approve, publish. API or Playwright fallback when quota's done.",
  },
];

export function HowItWorks() {
  return (
    <div className="mx-auto grid max-w-6xl gap-3 md:grid-cols-3">
      {STEPS.map((s, i) => (
        <motion.div
          key={s.n}
          initial={{ opacity: 0, y: 12 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ delay: i * 0.06, duration: 0.35 }}
          className="rounded-xl border border-border bg-surface p-7"
        >
          <div className="font-mono text-xs text-muted-foreground">{s.n}</div>
          <div className="mt-6 text-lg font-medium tracking-tight">{s.t}</div>
          <p className="mt-2 text-sm text-muted-foreground">{s.d}</p>
        </motion.div>
      ))}
    </div>
  );
}
