"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/app/page-header";
import { ChannelHeroCard } from "@/components/app/channel-hero-card";
import { channelsApi } from "@/lib/api";
import type { ChannelSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

const LANGUAGE_FILTERS = [
  { value: "all", label: "All languages" },
  { value: "en", label: "English" },
  { value: "hi", label: "Hindi" },
  { value: "hi-en", label: "Hinglish" },
];

export default function ChannelsPage() {
  const [channels, setChannels] = useState<ChannelSummary[] | null>(null);
  const [lang, setLang] = useState<string>("all");

  useEffect(() => {
    channelsApi.list().then((c) => setChannels(c.channels));
  }, []);

  const filtered = useMemo(() => {
    if (!channels) return null;
    if (lang === "all") return channels;
    return channels.filter((c) => c.language === lang);
  }, [channels, lang]);

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio · Channels"
        title="Channels"
        description="Eight live YouTube channels — each a complete production pipeline. Pick one to render or to tweak its defaults."
        actions={
          <Button asChild size="sm">
            <Link href="/app/create">
              <Plus className="h-3.5 w-3.5" />
              Create
            </Link>
          </Button>
        }
      />

      <div className="px-6 py-6 md:px-8">
        {/* Language filter chip row — subtle, single-line */}
        <div className="mb-6 flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Filter
          </span>
          {LANGUAGE_FILTERS.map((f) => {
            const active = lang === f.value;
            return (
              <button
                key={f.value}
                type="button"
                onClick={() => setLang(f.value)}
                className={cn(
                  "rounded-md border px-2.5 py-1 text-[11.5px] transition-colors",
                  active
                    ? "border-foreground/40 bg-foreground/10 text-foreground"
                    : "border-border bg-surface text-muted-foreground hover:border-border-strong hover:text-foreground",
                )}
              >
                {f.label}
              </button>
            );
          })}
        </div>

        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {(filtered ?? Array(6).fill(null)).map((c: ChannelSummary | null, i: number) =>
            c ? (
              <ChannelHeroCard
                key={c.key}
                channel={c}
                href={`/app/channels/${c.key}`}
                index={i}
              />
            ) : (
              <Skeleton key={i} className="h-72" />
            ),
          )}
        </div>

        {filtered && filtered.length === 0 && (
          <div className="mt-12 rounded-xl border border-dashed border-border bg-surface-2 p-10 text-center text-[12.5px] text-muted-foreground">
            No channels match this filter.
          </div>
        )}
      </div>
    </div>
  );
}
