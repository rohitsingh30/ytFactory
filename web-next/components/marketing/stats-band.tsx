"use client";

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { api } from "@/lib/api";
import type { DashboardData } from "@/lib/types";

interface Stat {
  label: string;
  value: string;
  hint?: string;
}

const PLACEHOLDER: Stat[] = [
  { label: "Channels live", value: "8", hint: "across niches" },
  { label: "Renders / week", value: "—" },
  { label: "Avg time to ship", value: "~7m" },
  { label: "Pipeline stages", value: "7" },
];

export function StatsBand() {
  const [stats, setStats] = useState<Stat[]>(PLACEHOLDER);

  useEffect(() => {
    let cancelled = false;
    api
      .get<DashboardData>("/api/dashboard")
      .then((d) => {
        if (cancelled) return;
        setStats([
          { label: "Channels live", value: String(d.channels?.length ?? 8), hint: "across niches" },
          {
            label: "Renders / week",
            value: d.renders_7d != null ? String(d.renders_7d) : "—",
          },
          {
            label: "Uploads / week",
            value: d.uploads_7d != null ? String(d.uploads_7d) : "—",
          },
          { label: "Avg time to ship", value: "~7m", hint: "p50" },
        ]);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mx-auto grid max-w-5xl grid-cols-2 gap-px overflow-hidden rounded-xl border border-border bg-border md:grid-cols-4">
      {stats.map((s, i) => (
        <motion.div
          key={s.label}
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ delay: i * 0.05, duration: 0.3 }}
          className="bg-background p-7"
        >
          <div className="num display-gradient text-3xl font-medium tracking-tight md:text-4xl">
            {s.value}
          </div>
          <div className="mt-2 text-xs uppercase tracking-[0.18em] text-muted-foreground">
            {s.label}
          </div>
          {s.hint && (
            <div className="mt-1 font-mono text-[10px] text-muted-foreground/70">{s.hint}</div>
          )}
        </motion.div>
      ))}
    </div>
  );
}
