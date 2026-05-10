"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, ExternalLink, Loader2, Pencil, Plus, Save, Trash2, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Slider } from "@/components/ui/slider";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ChannelAvatar } from "@/components/app/channel-avatar";
import { ChannelStatStrip } from "@/components/app/channel-stat-strip";
import { NicheFormDialog } from "@/components/app/niche-form-dialog";
import { StatusPill } from "@/components/app/status-pill";
import { EmptyState } from "@/components/app/empty-state";
import { channelLabel, fmtCount } from "@/components/app/channel-meta";
import { channelsApi, jobsApi, nichesApi } from "@/lib/api";
import type {
  ChannelSummary,
  CustomizationField,
  CustomizationSchema,
  Job,
  NicheDoc,
} from "@/lib/types";
import { cn, relativeTime } from "@/lib/utils";

export default function ChannelDetailPage() {
  const params = useParams<{ channel: string }>();
  const ch = params.channel;
  const [channel, setChannel] = useState<ChannelSummary | null>(null);
  const [schema, setSchema] = useState<CustomizationSchema | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [defaults, setDefaults] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!ch) return;
    Promise.all([
      channelsApi.get(ch),
      channelsApi.schema(ch),
      jobsApi.list({ channel: ch, limit: 12 }),
    ])
      .then(([c, s, j]) => {
        setChannel(c);
        setSchema(s);
        setJobs(j.jobs);
        const seeded: Record<string, unknown> = {};
        s.fields.forEach((f) => {
          if (f.default !== undefined && f.default !== null) seeded[f.key] = f.default;
        });
        // Seed the niche selector from the sidecar so the Niche tab
        // shows the user's saved pick (or null when nothing's saved).
        if (s.default_variant) seeded.default_variant = s.default_variant;
        setDefaults(seeded);
      })
      .catch(() => {});
  }, [ch]);

  async function saveDefaults() {
    if (!ch) return;
    setSaving(true);
    try {
      await channelsApi.patchDefaults(ch, defaults);
      toast.success("Defaults saved");
    } catch (e) {
      toast.error("Save failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex min-h-full flex-col">
      {/* Compact header — channel identity + actions in one row, no separate hero. */}
      <div className="flex flex-col gap-4 border-b border-border px-6 py-5 md:px-8">
        <div className="flex items-center justify-between gap-3">
          <div className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Channel · {ch}
          </div>
          <div className="flex items-center gap-2">
            <Button asChild variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
              <Link href="/app/channels">
                <ArrowLeft className="h-3.5 w-3.5" />
                All channels
              </Link>
            </Button>
            <Button asChild size="sm">
              <Link href={`/app/create?channel=${ch}`}>
                <Wand2 className="h-3.5 w-3.5" />
                Render new
              </Link>
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <div className="flex items-center gap-3 min-w-0">
            <ChannelAvatar channel={ch ?? ""} size="lg" />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="truncate text-[22px] font-semibold tracking-tight">
                  {channel?.label ?? channelLabel(ch)}
                </h1>
                {channel?.custom_url && (
                  <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
                    {channel.custom_url}
                  </span>
                )}
                {channel?.youtube_url && (
                  <a
                    href={channel.youtube_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
                    title="Open on YouTube"
                  >
                    <ExternalLink className="h-3 w-3" />
                    YouTube
                  </a>
                )}
              </div>
              {channel?.tagline && (
                <p className="mt-0.5 truncate text-[12.5px] text-muted-foreground">
                  {channel.tagline}
                </p>
              )}
            </div>
          </div>
          <div className="ml-auto">
            <ChannelStatStrip
              subscribers={channel?.subscribers}
              videoCount={channel?.youtube_video_count}
              totalViews={channel?.total_views}
              size="sm"
            />
          </div>
        </div>
      </div>

      <div className="px-6 py-6 md:px-8">
        <div className="mt-3">
          <ChannelTabs
            ch={ch}
            channel={channel}
            schema={schema}
            jobs={jobs}
            defaults={defaults}
            setDefaults={setDefaults}
            saving={saving}
            onSave={saveDefaults}
          />
        </div>
      </div>
    </div>
  );
}

function ChannelTabs({
  ch,
  channel,
  schema,
  jobs,
  defaults,
  setDefaults,
  saving,
  onSave,
}: {
  ch: string | undefined;
  channel: ChannelSummary | null;
  schema: CustomizationSchema | null;
  jobs: Job[] | null;
  defaults: Record<string, unknown>;
  setDefaults: React.Dispatch<React.SetStateAction<Record<string, unknown>>>;
  saving: boolean;
  onSave: () => void;
}) {
  const variants = schema?.variants ?? [];
  return (
    <Tabs defaultValue="latest" className="w-full">
      <TabsList className="h-auto w-full justify-start gap-1 bg-surface-2/60 p-1">
        <TabsTrigger value="latest" className="px-3 py-1.5 text-[12px]">
          Latest videos
        </TabsTrigger>
        <TabsTrigger value="renders" className="px-3 py-1.5 text-[12px]">
          Recent renders
        </TabsTrigger>
        <TabsTrigger value="niche" className="px-3 py-1.5 text-[12px]">
          Niches
        </TabsTrigger>
        <TabsTrigger value="defaults" className="px-3 py-1.5 text-[12px]">
          Defaults
        </TabsTrigger>
      </TabsList>

      <TabsContent value="latest" className="mt-5">
        <RecentShorts channel={channel} />
      </TabsContent>

      <TabsContent value="renders" className="mt-5">
        <RecentRenders ch={ch} jobs={jobs} />
      </TabsContent>

      <TabsContent value="niche" className="mt-5">
        <NichePane
          channel={ch}
          defaultVariant={
            typeof defaults.default_variant === "string"
              ? (defaults.default_variant as string)
              : null
          }
          onChangeDefault={(v) => setDefaults((d) => ({ ...d, default_variant: v }))}
          onSaveDefault={onSave}
          saving={saving}
        />
      </TabsContent>

      <TabsContent value="defaults" className="mt-5">
        <div className="grid gap-6 lg:grid-cols-[1fr_1.4fr]">
          <ChannelFacts channel={channel} ch={ch} />
          <DefaultsEditor
            schema={schema}
            defaults={defaults}
            setDefaults={setDefaults}
            saving={saving}
            onSave={onSave}
          />
        </div>
      </TabsContent>
    </Tabs>
  );
}

function NichePane({
  channel,
  defaultVariant,
  onChangeDefault,
  onSaveDefault,
  saving,
}: {
  channel: string | undefined;
  defaultVariant: string | null;
  onChangeDefault: (v: string) => void;
  onSaveDefault: () => void;
  saving: boolean;
}) {
  const [niches, setNiches] = useState<NicheDoc[] | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingNiche, setEditingNiche] = useState<NicheDoc | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  async function reload() {
    if (!channel) return;
    try {
      const r = await nichesApi.list(channel);
      setNiches(r.niches);
    } catch (e) {
      toast.error("Couldn't load niches", { description: e instanceof Error ? e.message : String(e) });
      setNiches([]);
    }
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel]);

  function openCreate() {
    setEditingNiche(null);
    setDialogOpen(true);
  }
  function openEdit(n: NicheDoc) {
    setEditingNiche(n);
    setDialogOpen(true);
  }

  async function deleteOne(n: NicheDoc) {
    if (!channel) return;
    if (!confirm(`Delete niche "${n.label}"? The JSON file will be removed.`)) return;
    setDeleting(n.key);
    try {
      await nichesApi.delete(channel, n.key);
      toast.success(`Deleted ${n.label}`);
      reload();
    } catch (e) {
      toast.error("Delete failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="space-y-5">
      {/* Default-variant picker — kept for the create wizard's default; Add button lives here too */}
      {niches && niches.length > 0 ? (
        <div className="rounded-md border border-border bg-surface-2/50 px-4 py-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <Label className="text-[11.5px] font-medium">Default niche for this channel</Label>
              <p className="mt-0.5 text-[10.5px] text-muted-foreground">
                Pre-selected in the create wizard. Persisted as a sidecar.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Select
                value={defaultVariant ?? ""}
                onValueChange={(v) => onChangeDefault(v)}
              >
                <SelectTrigger className="w-56 h-8 text-[12px]">
                  <SelectValue placeholder="(none — first niche)" />
                </SelectTrigger>
                <SelectContent>
                  {niches.map((n) => (
                    <SelectItem key={n.key} value={n.key}>{n.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button size="sm" variant="outline" onClick={onSaveDefault} disabled={saving}>
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                Save
              </Button>
              <Button size="sm" onClick={openCreate}>
                <Plus className="h-3.5 w-3.5" /> Add niche
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex justify-end">
          <Button size="sm" onClick={openCreate}>
            <Plus className="h-3.5 w-3.5" /> Add niche
          </Button>
        </div>
      )}

      {/* Niche cards */}
      {niches === null ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-36" />)}
        </div>
      ) : niches.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-surface-2 p-10 text-center text-[12.5px] text-muted-foreground">
          No niches yet — click <span className="text-foreground font-medium">Add niche</span> to create the first one.
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {niches.map((n) => (
            <NicheCard
              key={n.key}
              niche={n}
              isDefault={n.key === defaultVariant}
              onEdit={() => openEdit(n)}
              onDelete={() => deleteOne(n)}
              deleting={deleting === n.key}
            />
          ))}
        </div>
      )}

      {channel && (
        <NicheFormDialog
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          channel={channel}
          existing={editingNiche}
          onSaved={reload}
        />
      )}
    </div>
  );
}

function NicheCard({
  niche,
  isDefault,
  onEdit,
  onDelete,
  deleting,
}: {
  niche: NicheDoc;
  isDefault: boolean;
  onEdit: () => void;
  onDelete: () => void;
  deleting: boolean;
}) {
  return (
    <div
      className={cn(
        "group flex flex-col gap-2 rounded-xl border bg-surface p-4 transition-colors",
        isDefault ? "border-emerald-400/55" : "border-border hover:border-border-strong",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="truncate text-[13px] font-medium tracking-tight">{niche.label}</span>
            {isDefault && (
              <span className="rounded-full bg-emerald-400/15 px-1.5 py-0.5 text-[9.5px] uppercase tracking-[0.12em] text-emerald-300">
                default
              </span>
            )}
          </div>
          <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-muted-foreground">
            {niche.key}
          </div>
        </div>
        <div className="flex items-center gap-1 opacity-60 group-hover:opacity-100 transition-opacity">
          <Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={onEdit} title="Edit">
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0 text-rose-300 hover:text-rose-200"
            onClick={onDelete}
            disabled={deleting}
            title="Delete"
          >
            {deleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
          </Button>
        </div>
      </div>
      {niche.description && (
        <p className="line-clamp-3 text-[11.5px] leading-snug text-muted-foreground">
          {niche.description}
        </p>
      )}
      <div className="mt-auto flex flex-wrap gap-1.5 pt-1 text-[10px]">
        <NicheBadge>{niche.format}</NicheBadge>
        <NicheBadge>{niche.length_kind === "long" ? "long form" : "short form"}</NicheBadge>
        <NicheBadge>{niche.voice}</NicheBadge>
        <NicheBadge>{niche.source_kind}</NicheBadge>
        {niche.created_by !== "user" && (
          <NicheBadge muted>{niche.created_by}</NicheBadge>
        )}
      </div>
    </div>
  );
}

function NicheBadge({ children, muted }: { children: React.ReactNode; muted?: boolean }) {
  return (
    <span
      className={cn(
        "rounded-full border px-1.5 py-0.5 font-mono uppercase tracking-[0.1em]",
        muted
          ? "border-border bg-transparent text-muted-foreground/70"
          : "border-border bg-surface-2 text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

interface InspirationVideo {
  video_id: string;
  title: string;
  thumbnail: string | null;
  views: number | null;
  watch_url: string;
}

function RecentShorts({ channel }: { channel: ChannelSummary | null }) {
  // Lazy-fetch the FULL list (no limit) — `channel.recent_videos` is the
  // 3-tile preview reserved for the channel hero card. The Latest videos
  // tab needs every cached short so the user can scroll through history.
  const [videos, setVideos] = useState<InspirationVideo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const channelKey = channel?.key;

  useEffect(() => {
    if (!channelKey) return;
    let cancelled = false;
    setVideos(null);
    setError(null);
    channelsApi
      .inspiration(channelKey)
      .then((res) => {
        if (cancelled) return;
        setVideos(res.videos as InspirationVideo[]);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setError(e.message);
        setVideos([]);
      });
    return () => {
      cancelled = true;
    };
  }, [channelKey]);

  const channelLink = channel?.youtube_url ? (
    <a
      href={channel.youtube_url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground transition-colors hover:text-foreground"
    >
      channel page
      <ExternalLink className="h-3 w-3" />
    </a>
  ) : null;

  if (channel === null || videos === null) {
    return (
      <div>
        <SectionHeader
          title="Latest on YouTube"
          subtitle="Shorts shipped from this channel"
          action={channelLink ?? undefined}
        />
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {Array(8).fill(0).map((_, i) => (
            <Skeleton key={i} className="aspect-[9/16] rounded-md" />
          ))}
        </div>
      </div>
    );
  }

  if (videos.length === 0) {
    return (
      <div>
        <SectionHeader
          title="Latest on YouTube"
          subtitle="Shorts shipped from this channel"
          action={channelLink ?? undefined}
        />
        <div className="mt-3 rounded-xl border border-dashed border-border bg-surface-2 p-8 text-center text-[12.5px] text-muted-foreground">
          {error ? `Couldn't load videos — ${error}` : "Nothing live yet — render the first one."}
        </div>
      </div>
    );
  }

  return (
    <div>
      <SectionHeader
        title="Latest on YouTube"
        subtitle={`${videos.length} ${videos.length === 1 ? "short" : "shorts"} on this channel`}
        action={channelLink ?? undefined}
      />
      <div className="mt-3 max-h-[640px] overflow-y-auto rounded-md border border-border bg-surface-2/40 p-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {videos.map((v) => (
            <a
              key={v.video_id}
              href={v.watch_url}
              target="_blank"
              rel="noreferrer"
              className="group block overflow-hidden rounded-md border border-border bg-background"
            >
              <div className="relative aspect-[9/16] overflow-hidden">
                {v.thumbnail && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={v.thumbnail}
                    alt=""
                    loading="lazy"
                    className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-[1.04]"
                  />
                )}
                <span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 via-black/40 to-transparent px-2.5 pb-2 pt-6 text-[10.5px] font-mono uppercase tracking-[0.16em] text-white/85">
                  {fmtCount(v.views)} views
                </span>
              </div>
              <div className="p-2.5">
                <div className="line-clamp-2 text-[11.5px] font-medium leading-snug tracking-tight">
                  {v.title}
                </div>
              </div>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}

function RecentRenders({ ch, jobs }: { ch: string | undefined; jobs: Job[] | null }) {
  return (
    <div>
      <SectionHeader
        title="Recent renders"
        subtitle="Local pipeline runs (last 12)"
      />
      <div className="mt-3">
        {jobs === null ? (
          <Skeleton className="h-64 rounded-xl" />
        ) : jobs.length === 0 ? (
          <EmptyState title="No renders yet" description="Hit Render new to kick off the first one." />
        ) : (
          <ul className="divide-y divide-border rounded-xl border border-border bg-surface">
            {jobs.map((j) => (
              <li key={j.job_id}>
                <Link
                  href={`/app/render/${j.job_id}`}
                  className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-surface-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[12.5px] font-medium tracking-tight">
                      {j.topic ?? "(untitled)"}
                    </div>
                    <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">
                      {j.stage ?? "—"} · {relativeTime(j.updated_at ?? j.created_at)}
                    </div>
                  </div>
                  <StatusPill
                    status={j.status}
                    pulse={j.status === "rendering" || j.status === "uploading"}
                  />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ChannelFacts({ channel, ch }: { channel: ChannelSummary | null; ch: string | undefined }) {
  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        Pipeline defaults
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-[12.5px]">
        <Stat label="Slug" value={ch} mono />
        <Stat label="Language" value={channel?.language} />
        <Stat label="Default format" value={channel?.default_format} mono />
        <Stat label="TTS" value={channel?.tts_provider ?? "—"} mono />
        <Stat label="Images" value={channel?.image_provider ?? "footage"} mono />
        <Stat label="Default voice" value={channel?.default_voice ?? "—"} mono />
        <Stat label="Default length" value={channel?.default_length_s ? `${channel.default_length_s}s` : "—"} />
        <Stat label="Variants" value={String(channel?.variants_count ?? 0)} />
      </dl>
    </div>
  );
}

function DefaultsEditor({
  schema,
  defaults,
  setDefaults,
  saving,
  onSave,
}: {
  schema: CustomizationSchema | null;
  defaults: Record<string, unknown>;
  setDefaults: React.Dispatch<React.SetStateAction<Record<string, unknown>>>;
  saving: boolean;
  onSave: () => void;
}) {
  // Ordered group definitions — `keys` selects which schema fields land
  // in each tab. Keep these tight and predictable so the right-rail
  // doesn't grow unbounded as the schema gains fields. Niche lives at
  // the page-level tab strip, so we don't reproduce it here.
  const GROUPS: { id: string; label: string; keys: string[] }[] = [
    { id: "content", label: "Content", keys: ["source_kind", "source_ref", "length_s", "voice"] },
    { id: "style", label: "Style", keys: ["captions_density", "music_bed"] },
    { id: "publishing", label: "Publishing", keys: ["visibility", "schedule_at"] },
  ];
  const HIDDEN = new Set(["topic", "notes"]);

  const fields = schema?.fields ?? [];
  const grouped = GROUPS.map((g) => ({
    ...g,
    fields: g.keys
      .map((k) => fields.find((f) => f.key === k))
      .filter((f): f is CustomizationField => Boolean(f)),
  }));
  const grouping = new Set([...GROUPS.flatMap((g) => g.keys), ...HIDDEN]);
  const otherFields = fields.filter((f) => !grouping.has(f.key));
  if (otherFields.length > 0) {
    grouped.push({ id: "other", label: "Other", keys: [], fields: otherFields });
  }
  const tabs = grouped.filter((g) => g.fields.length > 0);
  const defaultTab = tabs[0]?.id ?? "content";

  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Override defaults
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">
            Persisted as sidecar JSON
          </div>
        </div>
        <Button size="sm" onClick={onSave} disabled={saving || !schema}>
          {saving ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Saving
            </>
          ) : (
            <>
              <Save className="h-3.5 w-3.5" />
              Save
            </>
          )}
        </Button>
      </div>

      {!schema ? (
        <Skeleton className="mt-5 h-32" />
      ) : tabs.length === 0 ? (
        <div className="mt-5 rounded-md border border-dashed border-border bg-surface-2/40 px-3 py-6 text-center text-[12px] text-muted-foreground">
          No editable defaults for this channel.
        </div>
      ) : (
        <Tabs defaultValue={defaultTab} className="mt-4">
          <TabsList className="flex h-auto w-full flex-wrap justify-start gap-1 bg-surface-2/60">
            {tabs.map((t) => (
              <TabsTrigger key={t.id} value={t.id} className="px-2.5 py-1 text-[11.5px]">
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>

          {tabs.map((g) => (
            <TabsContent key={g.id} value={g.id} className="mt-4 space-y-4">
              {g.fields.map((f) => (
                <DefaultsField
                  key={f.key}
                  field={f}
                  value={defaults[f.key]}
                  onChange={(v) => setDefaults((d) => ({ ...d, [f.key]: v }))}
                />
              ))}
            </TabsContent>
          ))}
        </Tabs>
      )}
    </div>
  );
}

function SectionHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-end justify-between gap-2">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          {title}
        </div>
        {subtitle && (
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">{subtitle}</div>
        )}
      </div>
      {action}
    </div>
  );
}

function Stat({ label, value, mono }: { label: string; value?: string | null; mono?: boolean }) {
  return (
    <div>
      <dt className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
        {label}
      </dt>
      <dd className={mono ? "mt-0.5 truncate font-mono text-[11.5px] text-foreground/90" : "mt-0.5 truncate text-foreground/90"}>
        {value ?? "—"}
      </dd>
    </div>
  );
}

function DefaultsField({
  field,
  value,
  onChange,
}: {
  field: CustomizationField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <Label htmlFor={field.key} className="text-[12px]">
          {field.label}
        </Label>
        {field.kind === "slider" && (
          <span className="font-mono text-[11px] text-muted-foreground">
            {String(value ?? field.default ?? "")}
            {field.key === "length_s" ? "s" : ""}
          </span>
        )}
      </div>
      <div className="mt-1.5">
        {field.kind === "select" && (
          <Select
            value={value !== undefined && value !== null ? String(value) : undefined}
            onValueChange={onChange}
          >
            <SelectTrigger id={field.key}>
              <SelectValue placeholder="—" />
            </SelectTrigger>
            <SelectContent>
              {(field.options ?? []).map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {field.kind === "slider" && (
          <Slider
            value={[Number(value ?? field.default ?? field.min ?? 0)]}
            min={field.min ?? 0}
            max={field.max ?? 100}
            step={field.step ?? 1}
            onValueChange={(v) => onChange(v[0])}
            className="mt-2"
          />
        )}
        {field.kind === "switch" && (
          <Switch checked={Boolean(value)} onCheckedChange={onChange} />
        )}
        {(field.kind === "text" || field.kind === "url") && (
          <Input
            id={field.key}
            type={field.kind}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value)}
            className="bg-surface-2"
          />
        )}
        {field.kind === "number" && (
          <Input
            id={field.key}
            type="number"
            min={field.min}
            max={field.max}
            value={typeof value === "number" ? value : ""}
            onChange={(e) => onChange(Number(e.target.value))}
            className="bg-surface-2"
          />
        )}
      </div>
    </div>
  );
}
