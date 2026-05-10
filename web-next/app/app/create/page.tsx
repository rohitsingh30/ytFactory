"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  ExternalLink,
  Loader2,
  Music,
  Play,
  Settings2,
  Sparkles,
  Wand2,
} from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Slider } from "@/components/ui/slider";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/app/page-header";
import { ChannelHeroCard } from "@/components/app/channel-hero-card";
import { AudioSampleButton } from "@/components/app/audio-sample-button";
import { PreviewableTile } from "@/components/app/previewable-tile";
import { CloneVoiceDialog } from "@/components/app/clone-voice-dialog";
import { SongPicker, type SongPickerValues } from "@/components/app/song-picker";
import { ChannelAvatar } from "@/components/app/channel-avatar";
import { channelLabel, CHANNEL_TONE } from "@/components/app/channel-meta";
import {
  channelsApi,
  discoverApi,
  musicApi,
  nichesApi,
  renderApi,
  voicesApi,
  type DiscoverItem,
  type FamousVoiceTemplate,
  type MusicBed,
  type VoiceInfo,
} from "@/lib/api";
import type { ChannelSummary, CustomizationField, CustomizationSchema, NicheDoc } from "@/lib/types";
import { cn } from "@/lib/utils";

const AUTO_PULL_CHANNELS = new Set([
  "mystoriesanimated",
  "scrollpulse",
  "historyrecapped",
  "cosmosdecoded",
]);
// AUTO_PULL_CHANNELS retained for documentation only — every channel
// is now backed by either a native source adapter or an LLM brainstorm
// fallback (or both), so the auto-generate button is always rendered
// and always returns a usable suggestion.
void AUTO_PULL_CHANNELS;

type StepId = "mode" | "channel" | "customize";

const STEPS: { id: StepId; label: string; hint: string }[] = [
  { id: "mode", label: "How", hint: "Pick how you want to start" },
  { id: "channel", label: "Channel", hint: "Pick the channel" },
  { id: "customize", label: "Customize & review", hint: "Audition voices, tweak knobs, render" },
];

function fmtLanguage(l: string): string {
  if (l === "hi") return "Hindi";
  if (l === "hi-en") return "Hinglish";
  return "English";
}

export default function CreatePage() {
  const router = useRouter();
  const params = useSearchParams();
  const presetChannel = params.get("channel");

  // ?channel=… deep-links straight into the channel-focus flow.
  const [step, setStep] = useState<StepId>(presetChannel ? "channel" : "mode");
  const [channels, setChannels] = useState<ChannelSummary[] | null>(null);
  const [picked, setPicked] = useState<string | null>(presetChannel);
  const [variant, setVariant] = useState<string | null>(null);
  const [schema, setSchema] = useState<CustomizationSchema | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [submitting, setSubmitting] = useState(false);

  // Load channels once
  useEffect(() => {
    channelsApi
      .list()
      .then((c) => setChannels(c.channels))
      .catch(() => toast.error("Couldn't load channels"));
  }, []);

  // Load schema when channel changes
  useEffect(() => {
    if (!picked) return;
    setSchema(null);
    channelsApi
      .schema(picked)
      .then((s) => {
        setSchema(s);
        const seeded: Record<string, unknown> = {};
        s.fields.forEach((f) => {
          if (f.default !== undefined && f.default !== null) seeded[f.key] = f.default;
        });
        // Default the form-length toggle to "short" — overridden below
        // once the picked niche's length_kind is fetched.
        if (seeded.length_kind === undefined) seeded.length_kind = "short";
        if (seeded.length_minutes === undefined) seeded.length_minutes = 30;
        setValues(seeded);
        // Auto-pick the variant: prefer the user-saved niche from the
        // channel /defaults editor, then the channel YAML's
        // declared default_format, then the first variant. The user no
        // longer sees a variant picker in step 1 — they can shuffle
        // defaults in step 2 if needed.
        const savedNiche =
          s.default_variant && s.variants.some((v) => v.value === s.default_variant)
            ? s.default_variant
            : null;
        const channelDefault =
          channels?.find((c) => c.key === picked)?.default_format ?? null;
        const hasDefault = channelDefault
          ? s.variants.some((v) => v.value === channelDefault)
          : false;
        const fallback =
          savedNiche || (hasDefault && channelDefault) || s.variants[0]?.value || null;
        setVariant((prev) => prev ?? fallback);
      })
      .catch(() => toast.error("Couldn't load customization knobs"));
  }, [picked, channels]);

  // When the variant changes, fetch its NicheDoc and seed length_kind
  // from it. Best-effort — fails silently if the niche JSON doesn't
  // exist (channel hasn't been backfilled).
  //
  // Implementation: list-then-find rather than get-by-key. This avoids
  // the 404 console noise the dashboard was throwing every time the
  // user landed on /create with a variant whose niche JSON hadn't been
  // authored yet (which is most variants for non-mystoriesanimated
  // channels). list() always returns 200 with whatever's there, so the
  // network tab stays clean.
  useEffect(() => {
    if (!picked || !variant) return;
    nichesApi
      .list(picked)
      .then((r: { niches: NicheDoc[] }) => {
        const n = r.niches.find((d) => d.key === variant);
        if (!n) return;
        setValues((prev) => ({ ...prev, length_kind: n.length_kind }));
      })
      .catch(() => {
        // No niches at all — leave the user's current length_kind alone.
      });
  }, [picked, variant]);

  // Pre-jump to customize when arriving with ?channel=…
  useEffect(() => {
    if (presetChannel && schema && variant) setStep("customize");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presetChannel, schema]);

  const selectedChannel = useMemo(
    () => channels?.find((c) => c.key === picked) ?? null,
    [channels, picked],
  );

  function next() {
    if (step === "mode") setStep("channel");
    else if (step === "channel") setStep("customize");
  }
  function prev() {
    if (step === "customize") setStep("channel");
    else if (step === "channel") setStep("mode");
  }

  const canAdvance = useMemo(() => {
    if (step === "mode") return false; // mode-step uses direct routing/onPick
    if (step === "channel") return Boolean(picked);
    if (step === "customize") {
      if (!schema) return false;
      return schema.fields
        .filter((f) => f.required)
        .every((f) => {
          const v = values[f.key];
          return typeof v === "string" ? v.trim().length > 0 : v !== undefined && v !== null;
        });
    }
    return true;
  }, [step, picked, variant, schema, values]);

  async function submit() {
    if (!picked || !schema) return;
    setSubmitting(true);
    try {
      const topic = String(values.topic ?? "").trim();
      const notes = String(values.notes ?? "").trim();
      // Length is now form-bound (short / long), not seconds. The renderer
      // gets a sensible default per kind; per-niche YAML can refine.
      const length_kind = String(values.length_kind ?? "short");
      const long_minutes = Number(values.length_minutes ?? 30);
      const length_s = length_kind === "long" ? Math.max(1, long_minutes) * 60 : 55;
      const source_kind = String(values.source_kind ?? "auto");
      const source_ref = String(values.source_ref ?? "").trim() || null;

      // Pull only the user-facing knobs the renderer cares about into
      // channel_overrides — everything in here is forwarded to make_short
      // via --override key=value (see pipeline/render/shorts.py::cli_main).
      // Skip empty strings / undefined so the YAML default keeps winning
      // when the user didn't touch a field.
      const channel_overrides: Record<string, unknown> = {};
      const passthrough = [
        "voice", "music_bed", "captions_density", "visibility", "schedule_at",
        "audio_mode", "song_style", "song_vocal_gender", "song_model",
        "visual_source",
      ] as const;
      for (const k of passthrough) {
        const v = values[k];
        if (v === undefined || v === null) continue;
        if (typeof v === "string" && v.trim() === "") continue;
        channel_overrides[k] = v;
      }

      const result = await renderApi.enqueue({
        channel: picked,
        topic,
        notes,
        format: variant ?? schema.variants[0]?.value ?? "auto",
        source_kind,
        source_ref,
        length_s,
        channel_overrides,
      });
      toast.success("Render queued", { description: `Job ${result.job_id.slice(0, 8)}…` });
      router.push(`/app/render/${result.job_id}`);
    } catch (e) {
      toast.error("Couldn't enqueue render", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setSubmitting(false);
    }
  }

  const lockedReadout = useMemo(() => {
    const parts: string[] = [];
    if (picked) parts.push(channelLabel(picked));
    const variantLabel = schema?.variants.find((v) => v.value === variant)?.label;
    if (variantLabel) parts.push(variantLabel);
    return parts.join(" · ");
  }, [picked, variant, schema]);

  const stepIdx = STEPS.findIndex((s) => s.id === step);
  const progress = ((stepIdx + 1) / STEPS.length) * 100;

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        title="Make a Short"
        description="Pick how you want to start. Channel-focus walks you through a wizard; the others jump into their dedicated flow."
      />

      <div className="mx-auto w-full max-w-6xl flex-1 px-6 py-8 md:px-8">
        <Stepper current={step} progress={progress} />

        <div className="mt-10">
          <AnimatePresence mode="wait">
            <motion.div
              key={step}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.18, ease: "easeOut" }}
            >
              {step === "mode" && (
                <ModeStep
                  channels={channels ?? []}
                  onPickChannelFocus={() => setStep("channel")}
                />
              )}
              {step === "channel" && (
                <ChannelStep
                  channels={channels}
                  picked={picked}
                  onPick={(k) => {
                    if (k === picked) return;
                    setPicked(k);
                    setVariant(null);
                  }}
                />
              )}
              {step === "customize" && (
                <CustomizeReviewStep
                  channel={selectedChannel}
                  variant={variant}
                  schema={schema}
                  values={values}
                  onChange={(k, v) => setValues((p) => ({ ...p, [k]: v }))}
                />
              )}
            </motion.div>
          </AnimatePresence>
        </div>
      </div>

      {/* Sticky footer nav — hidden on the mode step (cards self-route) */}
      {step !== "mode" && (
      <div className="sticky bottom-0 z-20 mt-6 border-t border-border bg-background/85 backdrop-blur">
        <div className="mx-auto flex w-full max-w-6xl items-center gap-3 px-6 py-3.5 md:px-8">
          {step === "channel" ? (
            <Button variant="ghost" size="sm" onClick={prev} className="text-muted-foreground hover:text-foreground">
              <ArrowLeft className="h-3.5 w-3.5" />
              Back
            </Button>
          ) : (
            <Button variant="ghost" size="sm" onClick={prev} className="text-muted-foreground hover:text-foreground">
              <ArrowLeft className="h-3.5 w-3.5" />
              Back
            </Button>
          )}

          <div className="ml-2 hidden min-w-0 flex-1 items-center gap-2 text-[12px] text-muted-foreground sm:flex">
            {lockedReadout ? (
              <>
                <Check className="h-3 w-3 shrink-0 text-emerald-300/80" />
                <span className="truncate">{lockedReadout}</span>
              </>
            ) : (
              <span className="truncate text-muted-foreground/60">Nothing picked yet.</span>
            )}
          </div>

          {step === "customize" ? (
            <Button onClick={submit} disabled={!canAdvance || submitting} className="px-5">
              {submitting ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Queueing render
                </>
              ) : (
                <>
                  <Wand2 className="h-3.5 w-3.5" />
                  Render Short
                </>
              )}
            </Button>
          ) : (
            <Button onClick={next} disabled={!canAdvance} className="px-5">
              Continue
              <ArrowRight className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>
      )}
    </div>
  );
}

/* ----------------------------- Stepper ----------------------------- */

function Stepper({ current, progress }: { current: StepId; progress: number }) {
  const idx = STEPS.findIndex((s) => s.id === current);
  const active = STEPS[idx];
  return (
    <div>
      <div className="flex items-center justify-between text-[12px]">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            Step {String(idx + 1).padStart(2, "0")} / {String(STEPS.length).padStart(2, "0")}
          </span>
          <span className="text-foreground tracking-tight">{active.label}</span>
          <span className="text-muted-foreground">— {active.hint}</span>
        </div>
      </div>
      <div className="mt-3 h-px w-full overflow-hidden bg-border">
        <motion.div
          className="h-full bg-foreground/85"
          initial={false}
          animate={{ width: `${progress}%` }}
          transition={{ duration: 0.35, ease: "easeOut" }}
        />
      </div>
    </div>
  );
}

/* ----------------------------- Step 0: Mode ----------------------------- */

/**
 * The "How" step. Three substantive cards — same anatomical DNA as
 * ChannelHeroCard (banner + content stack + 3-tile preview reel + CTA),
 * but each card's banner hero is a bespoke visual mockup of THAT mode's
 * actual experience: a real channel-avatar cluster (Channel-focused),
 * a faux video-frame + analysis bars (Clone), or a chat exchange (AI).
 * No generic Lucide-icon-in-a-circle "placeholder" avatars.
 */
function ModeStep({
  channels,
  onPickChannelFocus,
}: {
  channels: ChannelSummary[];
  onPickChannelFocus: () => void;
}) {
  return (
    <div className="grid items-stretch gap-5 md:grid-cols-2 lg:grid-cols-3">
      <ChannelFocusModeCard channels={channels} onPick={onPickChannelFocus} index={0} />
      <CloneModeCard index={1} />
      <AIModeCard index={2} />
    </div>
  );
}

/* Shared shell — keeps anatomy consistent across the three modes. ------- */

function ModeCardShell({
  hero,
  title,
  tagline,
  stats,
  description,
  reelLabel,
  reel,
  ctaLabel,
  href,
  onClick,
  index,
  disabled = false,
  disabledReason = "Coming soon",
}: {
  hero: React.ReactNode;
  title: string;
  tagline: string;
  stats: { label: string; value: string }[];
  description: string;
  reelLabel: string;
  reel: React.ReactNode;
  ctaLabel: string;
  href?: string;
  onClick?: () => void;
  index: number;
  disabled?: boolean;
  disabledReason?: string;
}) {
  const inner = (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.05, 0.2), duration: 0.28, ease: "easeOut" }}
      className={cn(
        "group relative flex h-full flex-col overflow-hidden rounded-xl border-2 border-border bg-surface text-left transition-all",
        disabled
          ? "cursor-not-allowed opacity-60 grayscale-[0.35]"
          : "hover:border-border-strong hover:bg-surface-2 hover:-translate-y-0.5 hover:shadow-[0_14px_36px_-10px_rgba(0,0,0,0.55)]",
      )}
    >
      {disabled && (
        <div className="pointer-events-none absolute right-3 top-3 z-10 inline-flex items-center gap-1.5 rounded-full bg-amber-300/15 px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.2em] text-amber-200 ring-1 ring-amber-300/40 shadow-sm backdrop-blur-sm">
          <Sparkles className="h-2.5 w-2.5" />
          {disabledReason}
        </div>
      )}

      <div className="relative h-44 overflow-hidden">
        {hero}
        {/* Soft fade from banner into card body so the title region reads. */}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-20 bg-gradient-to-t from-surface via-surface/55 to-transparent" />
      </div>

      <div className="flex flex-1 flex-col gap-3.5 p-5">
        <div className="min-w-0">
          <div className="text-[16px] font-semibold tracking-tight">{title}</div>
          <div className="mt-1 font-mono text-[10.5px] uppercase tracking-[0.18em] text-muted-foreground">
            {tagline}
          </div>
        </div>

        <div className="flex items-center gap-3 border-y border-border py-2.5">
          {stats.map((s, i) => (
            <div
              key={s.label}
              className={cn("flex items-baseline gap-1.5", i > 0 && "border-l border-border pl-3")}
            >
              <span className="font-mono text-[13.5px] font-medium text-foreground tabular-nums">
                {s.value}
              </span>
              <span className="font-mono text-[9.5px] uppercase tracking-[0.16em] text-muted-foreground">
                {s.label}
              </span>
            </div>
          ))}
        </div>

        <p className="text-[12.5px] leading-relaxed text-muted-foreground">{description}</p>

        <div className="mt-auto pt-1">
          <div className="mb-2 font-mono text-[9.5px] uppercase tracking-[0.18em] text-muted-foreground/85">
            {reelLabel}
          </div>
          {reel}
        </div>

        <div className="mt-2 flex items-center justify-between gap-3 border-t border-border pt-3.5">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground/80">
            {disabled ? "Not available yet" : "Step 01 → 02"}
          </span>
          <span
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-[12px] font-medium transition-colors",
              disabled
                ? "border-foreground/10 bg-foreground/[0.02] text-muted-foreground/70"
                : "border-foreground/15 bg-foreground/[0.04] text-foreground group-hover:border-foreground/30 group-hover:bg-foreground/[0.10]",
            )}
          >
            {ctaLabel}
            {!disabled && (
              <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
            )}
          </span>
        </div>
      </div>
    </motion.div>
  );

  if (disabled) {
    return (
      <div
        role="button"
        aria-disabled="true"
        tabIndex={-1}
        title={disabledReason}
        className="block h-full select-none"
      >
        {inner}
      </div>
    );
  }

  if (href) {
    return (
      <Link href={href} className="block h-full">
        {inner}
      </Link>
    );
  }
  return (
    <button type="button" onClick={onClick} className="block h-full w-full text-left">
      {inner}
    </button>
  );
}

/* Mode 1 — Channel-focused ----------------------------------------------- */

const FALLBACK_CHANNEL_KEYS = [
  "mystoriesanimated",
  "cosmosdecoded",
  "historyrecapped",
  "hindutavaanimated",
  "scrollpulse",
  "sportsrecapped",
  "rhymetimejunction",
];

function ChannelFocusModeCard({
  channels,
  onPick,
  index,
}: {
  channels: ChannelSummary[];
  onPick: () => void;
  index: number;
}) {
  const featured = channels.length
    ? channels.slice(0, 7)
    : FALLBACK_CHANNEL_KEYS.map((key) => ({ key, label: channelLabel(key) } as ChannelSummary));
  const bannerStrips = featured.slice(0, 4);
  const avatarCluster = featured.slice(0, 5);
  const reelChannels = featured.slice(0, 3);

  return (
    <ModeCardShell
      index={index}
      title="Channel-focused"
      tagline="Pick a channel · author from a niche"
      stats={[
        { label: "channels", value: String(channels.length || FALLBACK_CHANNEL_KEYS.length) },
        { label: "niches", value: "20+" },
        { label: "control", value: "full" },
      ]}
      description="Walk through the wizard for one of your existing channels. Pick the channel, lock the niche, audition voices, render."
      ctaLabel="Choose a channel"
      onClick={onPick}
      reelLabel="Your channels"
      hero={
        <div className="absolute inset-0">
          {/* Stripe of real channel banner gradients — no flat fill. */}
          <div className="absolute inset-0 grid grid-cols-4">
            {bannerStrips.map((c) => {
              const tone = CHANNEL_TONE[c.key] ?? CHANNEL_TONE._fallback;
              return <div key={c.key} className={cn("h-full w-full", tone.banner)} />;
            })}
          </div>
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_45%,rgba(255,255,255,0.10),transparent_65%)]" />
          {/* Overlapping avatar cluster — replaces placeholder Wand2 icon. */}
          <div className="absolute inset-x-0 top-1/2 -translate-y-1/2">
            <div className="flex justify-center -space-x-3">
              {avatarCluster.map((c) => (
                <ChannelAvatar
                  key={c.key}
                  channel={c.key}
                  size="lg"
                  className="ring-4 ring-surface shadow-md"
                />
              ))}
            </div>
          </div>
          <div className="absolute inset-x-0 top-3 text-center font-mono text-[9.5px] uppercase tracking-[0.22em] text-foreground/75">
            {channels.length} channels · 20+ niches
          </div>
        </div>
      }
      reel={
        <div className="grid grid-cols-3 gap-1.5">
          {reelChannels.map((c) => {
            const tone = CHANNEL_TONE[c.key] ?? CHANNEL_TONE._fallback;
            return (
              <div
                key={c.key}
                className="relative aspect-[9/16] overflow-hidden rounded-md border border-border bg-background"
              >
                <div className={cn("absolute inset-0", tone.banner)} />
                <div className="absolute inset-x-1.5 bottom-1.5 flex items-center gap-1.5 rounded bg-background/70 px-1.5 py-1 backdrop-blur">
                  <ChannelAvatar channel={c.key} size="xs" />
                  <span className="truncate text-[9px] font-medium text-foreground">{c.label}</span>
                </div>
              </div>
            );
          })}
        </div>
      }
    />
  );
}

/* Mode 2 — Clone a video ------------------------------------------------- */

function CloneModeCard({ index }: { index: number }) {
  return (
    <ModeCardShell
      index={index}
      title="Clone a video"
      tagline="Reverse-engineer a viral format"
      stats={[
        { label: "input", value: "URL" },
        { label: "depth", value: "frame" },
        { label: "output", value: "niche" },
      ]}
      description="Paste a video URL — we deep-analyse the format (hook, pacing, captions, voice, visuals) and turn it into a fresh niche your channels can produce."
      ctaLabel="Paste a URL"
      href="/app/create/clone"
      reelLabel="Input → analysis → niche"
      hero={
        <div className="absolute inset-0 bg-gradient-to-br from-fuchsia-600/40 via-rose-600/15 to-purple-950/65">
          {/* Faux browser URL chrome — hints at what you paste. */}
          <div className="absolute inset-x-5 top-4 flex items-center gap-1.5 rounded-md bg-background/80 px-2.5 py-1.5 ring-1 ring-border backdrop-blur">
            <span className="h-1.5 w-1.5 rounded-full bg-rose-400/85" />
            <span className="h-1.5 w-1.5 rounded-full bg-amber-400/85" />
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400/85" />
            <span className="ml-1 truncate font-mono text-[10px] text-muted-foreground">
              youtube.com/shorts/<span className="text-foreground">aB7c…</span>
            </span>
          </div>
          {/* 9:16 video frame mockup with play triangle — the "video". */}
          <div className="absolute left-[28%] top-1/2 -translate-x-1/2 -translate-y-[42%]">
            <div className="relative h-28 w-[63px] overflow-hidden rounded-md border border-border/80 bg-gradient-to-b from-fuchsia-500/55 via-rose-700/35 to-purple-950/75 shadow-2xl">
              <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_38%,rgba(255,255,255,0.22),transparent_65%)]" />
              <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2">
                <div className="grid h-7 w-7 place-items-center rounded-full bg-background/95 ring-1 ring-border">
                  <Play className="ml-0.5 h-3.5 w-3.5 fill-foreground text-foreground" />
                </div>
              </div>
              <div className="absolute inset-x-1 bottom-1 h-1 rounded-full bg-foreground/15">
                <div className="h-full w-2/5 rounded-full bg-rose-300/90" />
              </div>
            </div>
          </div>
          {/* Analysis bars — hints "we tear it down to its DNA". */}
          <div className="absolute right-5 top-1/2 -translate-y-[42%] flex flex-col gap-1.5">
            {[
              { w: "w-16", label: "hook" },
              { w: "w-12", label: "pace" },
              { w: "w-20", label: "voice" },
              { w: "w-14", label: "visu" },
              { w: "w-10", label: "cta" },
            ].map((b) => (
              <div key={b.label} className="flex items-center gap-1.5">
                <span className="w-8 font-mono text-[8.5px] uppercase tracking-[0.15em] text-foreground/75">
                  {b.label}
                </span>
                <div className={cn("h-1 rounded-full bg-fuchsia-200/75", b.w)} />
              </div>
            ))}
          </div>
        </div>
      }
      reel={
        <div className="grid grid-cols-3 gap-1.5">
          {/* Input frame */}
          <div className="relative aspect-[9/16] overflow-hidden rounded-md border border-border bg-gradient-to-b from-zinc-800 to-zinc-950">
            <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_42%,rgba(255,255,255,0.18),transparent_65%)]" />
            <div className="absolute inset-x-1.5 top-1.5 truncate font-mono text-[8px] uppercase tracking-[0.16em] text-muted-foreground">
              input
            </div>
            <div className="absolute left-1/2 top-1/2 grid h-5 w-5 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-foreground/85 text-background">
              <Play className="ml-0.5 h-2.5 w-2.5 fill-current" />
            </div>
            <div className="absolute inset-x-1.5 bottom-1.5 truncate font-mono text-[8px] text-muted-foreground">
              URL
            </div>
          </div>
          {/* Analysis */}
          <div className="relative aspect-[9/16] overflow-hidden rounded-md border border-border bg-gradient-to-br from-fuchsia-500/30 via-rose-700/20 to-purple-950/50">
            <div className="absolute inset-x-1.5 top-1.5 font-mono text-[8px] uppercase tracking-[0.16em] text-muted-foreground">
              analyse
            </div>
            <div className="absolute inset-x-2 top-1/2 flex -translate-y-1/2 flex-col gap-1">
              {[
                { k: "hook", w: "w-[85%]" },
                { k: "pace", w: "w-[60%]" },
                { k: "voice", w: "w-[75%]" },
                { k: "art", w: "w-[50%]" },
              ].map((s) => (
                <div key={s.k} className="flex items-center gap-1">
                  <span className="w-6 font-mono text-[7px] uppercase text-muted-foreground/85">{s.k}</span>
                  <div className={cn("h-0.5 rounded-full bg-fuchsia-300/80", s.w)} />
                </div>
              ))}
            </div>
          </div>
          {/* New niche output */}
          <div className="relative aspect-[9/16] overflow-hidden rounded-md border border-emerald-500/45 bg-gradient-to-br from-emerald-500/30 via-teal-600/20 to-emerald-900/45">
            <div className="absolute inset-x-1.5 top-1.5 font-mono text-[8px] uppercase tracking-[0.16em] text-emerald-200">
              niche
            </div>
            <div className="absolute left-1/2 top-1/2 grid h-5 w-5 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-emerald-400 text-background">
              <Sparkles className="h-2.5 w-2.5" />
            </div>
            <div className="absolute inset-x-1.5 bottom-1.5 truncate font-mono text-[8px] font-medium text-foreground">
              + new
            </div>
          </div>
        </div>
      }
    />
  );
}

/* Mode 3 — AI-generated -------------------------------------------------- */

function AIModeCard({ index }: { index: number }) {
  return (
    <ModeCardShell
      index={index}
      title="AI-generated"
      tagline="Describe it · we build it"
      stats={[
        { label: "input", value: "chat" },
        { label: "decisions", value: "auto" },
        { label: "speed", value: "fast" },
      ]}
      description="Open the chat and describe the Short you want. The assistant figures out channel, niche, source, voice, and length, then queues the render."
      ctaLabel="Coming soon"
      disabled
      disabledReason="Coming soon"
      reelLabel="A typical exchange"
      hero={
        <div className="absolute inset-0 bg-gradient-to-br from-sky-600/35 via-indigo-700/25 to-violet-950/65">
          {/* Faux conversation — replaces placeholder MessageSquare icon. */}
          <div className="absolute inset-x-5 top-4 flex flex-col gap-1.5">
            <div className="self-end max-w-[78%] rounded-2xl rounded-br-sm bg-foreground px-3 py-1.5 text-[10.5px] leading-snug text-background shadow-md">
              30s short on the LIGO discovery, please.
            </div>
            <div className="self-start max-w-[88%] rounded-2xl rounded-bl-sm bg-background/90 px-3 py-1.5 text-[10.5px] leading-snug text-foreground shadow-md ring-1 ring-border backdrop-blur">
              <span className="font-mono text-[8.5px] uppercase tracking-[0.18em] text-muted-foreground">
                Cosmos Decoded
              </span>
              <div>32s · 6 scenes · Sarah voice</div>
            </div>
            <div className="self-end max-w-[55%] rounded-2xl rounded-br-sm bg-foreground px-3 py-1.5 text-[10.5px] text-background shadow-md">
              Render it.
            </div>
            <div className="self-start inline-flex items-center gap-1.5 rounded-full bg-emerald-400/95 px-2 py-0.5 text-[9.5px] font-medium text-background shadow">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-background/90" />
              queued
            </div>
          </div>
        </div>
      }
      reel={
        <div className="grid grid-cols-3 gap-1.5">
          {[
            {
              tag: "you",
              text: "30s short on LIGO",
              wrap: "self-end bg-foreground text-background rounded-br-sm",
            },
            {
              tag: "assistant",
              text: "Cosmos · 32s · 6 scenes",
              wrap: "self-start bg-background text-foreground ring-1 ring-border rounded-bl-sm",
            },
            {
              tag: "render",
              text: "queued",
              wrap: "self-start bg-emerald-400 text-background rounded-bl-sm",
            },
          ].map((b) => (
            <div
              key={b.tag}
              className="relative aspect-[9/16] overflow-hidden rounded-md border border-border bg-gradient-to-b from-sky-900/50 via-indigo-950/40 to-violet-950/65"
            >
              <div className="absolute inset-x-1.5 top-1.5 font-mono text-[8px] uppercase tracking-[0.16em] text-muted-foreground/90">
                {b.tag}
              </div>
              <div className="absolute inset-x-1.5 bottom-1.5 flex flex-col">
                <div
                  className={cn(
                    "rounded-2xl px-1.5 py-1 text-[8.5px] font-medium leading-tight shadow",
                    b.wrap,
                  )}
                >
                  {b.text}
                </div>
              </div>
            </div>
          ))}
        </div>
      }
    />
  );
}

/* ----------------------------- Step 1: Channel ----------------------------- */

function ChannelStep({
  channels,
  picked,
  onPick,
}: {
  channels: ChannelSummary[] | null;
  picked: string | null;
  onPick: (k: string) => void;
}) {
  if (!channels) {
    return (
      <div className="grid gap-5 md:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-72" />
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-5 md:grid-cols-2 lg:grid-cols-3">
      {channels.map((c, i) => (
        <ChannelHeroCard
          key={c.key}
          channel={c}
          index={i}
          selected={c.key === picked}
          onSelect={() => onPick(c.key)}
        />
      ))}
    </div>
  );
}

function VariantPicker({
  schema,
  channelKey,
  picked,
  onPick,
}: {
  schema: CustomizationSchema | null;
  channelKey: string;
  picked: string | null;
  onPick: (v: string) => void;
}) {
  if (!schema) {
    return (
      <div className="mt-3 grid grid-cols-2 gap-2">
        {[0, 1].map((i) => (
          <Skeleton key={i} className="h-9" />
        ))}
      </div>
    );
  }
  if (schema.variants.length === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className="mt-3 rounded-xl border border-border bg-surface-2/60 p-3"
    >
      <div className="mb-2 flex items-center gap-2 font-mono text-[9.5px] uppercase tracking-[0.18em] text-muted-foreground">
        <Sparkles className="h-3 w-3" />
        Pick a variant for {channelLabel(channelKey)}
      </div>
      <VariantList variants={schema.variants} picked={picked} onPick={onPick} />
    </motion.div>
  );
}

/* Render all variants. Auto-grouped by family when count > 5; flat 2-col grid otherwise. */
function VariantList({
  variants,
  picked,
  onPick,
}: {
  variants: { value: string; label: string; description?: string | null }[];
  picked: string | null;
  onPick: (v: string) => void;
}) {
  if (variants.length <= 5) {
    return (
      <div className="grid gap-1.5 sm:grid-cols-2">
        {variants.map((v) => (
          <VariantCard key={v.value} v={v} sel={v.value === picked} onPick={onPick} />
        ))}
      </div>
    );
  }
  const groups = groupVariants(variants);
  return (
    <div className="space-y-3">
      {groups.map((g) => (
        <div key={g.title}>
          <div className="mb-1.5 flex items-baseline justify-between px-0.5">
            <div className="text-[11px] font-medium tracking-tight text-foreground/85">{g.title}</div>
            <div className="font-mono text-[9.5px] tracking-[0.14em] text-muted-foreground/70">
              {g.items.length}
            </div>
          </div>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {g.items.map((v) => (
              <VariantCard key={v.value} v={v} sel={v.value === picked} onPick={onPick} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function VariantCard({
  v,
  sel,
  onPick,
}: {
  v: { value: string; label: string; description?: string | null };
  sel: boolean;
  onPick: (v: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onPick(v.value)}
      className={cn(
        "group relative flex h-full flex-col gap-1 rounded-md border px-2.5 py-2 text-left transition-colors",
        sel
          ? "border-foreground/45 bg-foreground/10 text-foreground"
          : "border-border bg-surface hover:border-border-strong",
      )}
    >
      <div className="flex items-start gap-2">
        <span
          className={cn(
            "mt-[3px] grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border transition-colors",
            sel
              ? "border-foreground bg-foreground text-background"
              : "border-border bg-surface text-transparent group-hover:border-border-strong",
          )}
          aria-hidden
        >
          <Check className="h-2.5 w-2.5" />
        </span>
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-[12px] font-medium tracking-tight",
            sel ? "text-foreground" : "text-foreground/90",
          )}
        >
          {v.label}
        </span>
      </div>
      {v.description ? (
        <p className="line-clamp-2 pl-[22px] text-[11px] leading-snug text-muted-foreground">
          {v.description}
        </p>
      ) : (
        <p className="pl-[22px] font-mono text-[9.5px] uppercase tracking-[0.14em] text-muted-foreground/70">
          {v.value}
        </p>
      )}
    </button>
  );
}

/* Auto-derive families from labels. Heuristic — falls back gracefully
 * when labels don't follow a known prefix. The order of FAMILIES drives
 * display order; "Other" catches the rest.
 */
const FAMILIES: { title: string; match: (label: string) => boolean }[] = [
  { title: "AITA Cliffhanger — Part 2", match: (l) => /aita.*cliffhanger.*part\s*2/i.test(l) },
  { title: "AITA Cliffhanger", match: (l) => /aita.*cliffhanger/i.test(l) },
  { title: "AITA", match: (l) => /^aita\b/i.test(l) },
  { title: "TIFU", match: (l) => /^tifu\b/i.test(l) },
];

function groupVariants(
  variants: { value: string; label: string; description?: string | null }[],
): { title: string; items: typeof variants }[] {
  const buckets = new Map<string, typeof variants>();
  const order: string[] = [];
  function push(title: string, v: typeof variants[number]) {
    if (!buckets.has(title)) {
      buckets.set(title, []);
      order.push(title);
    }
    buckets.get(title)!.push(v);
  }
  for (const v of variants) {
    const fam = FAMILIES.find((f) => f.match(v.label));
    push(fam ? fam.title : "Other", v);
  }
  // Move "Other" to the end if present, regardless of insertion order.
  const ordered = [
    ...order.filter((t) => t !== "Other"),
    ...(order.includes("Other") ? ["Other"] : []),
  ];
  return ordered.map((title) => ({ title, items: buckets.get(title)! }));
}

/* ----------------------------- Step 2: Customize + Review ----------------------------- */

function CustomizeReviewStep({
  channel,
  variant,
  schema,
  values,
  onChange,
}: {
  channel: ChannelSummary | null;
  variant: string | null;
  schema: CustomizationSchema | null;
  values: Record<string, unknown>;
  onChange: (k: string, v: unknown) => void;
}) {
  const [voices, setVoices] = useState<VoiceInfo[] | null>(null);
  const [music, setMusic] = useState<MusicBed[] | null>(null);

  useEffect(() => {
    voicesApi.list().then((r) => setVoices(r.voices)).catch(() => setVoices([]));
    musicApi.list().then((r) => setMusic(r.music)).catch(() => setMusic([]));
  }, []);

  if (!schema || !channel) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-32" />
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
      </div>
    );
  }

  const byKey = (k: string) => schema.fields.find((f) => f.key === k);
  const topicField = byKey("topic");
  const sourceKindField = byKey("source_kind");
  const sourceRefField = byKey("source_ref");
  const voiceField = byKey("voice");
  const audioModeField = byKey("audio_mode");
  const songStyleField = byKey("song_style");
  const songVocalGenderField = byKey("song_vocal_gender");
  const songModelField = byKey("song_model");
  const visualSourceField = byKey("visual_source");
  const musicField = byKey("music_bed");

  const handled = new Set([
    "topic", "length_s", "length_kind", "length_minutes",
    "source_kind", "source_ref", "voice", "music_bed",
    "audio_mode", "song_style", "song_vocal_gender", "song_model",
    "visual_source",
    "visibility", "schedule_at",
  ]);
  const advancedFields = schema.fields.filter((f) => !handled.has(f.key));

  const channelLanguage = schema.language;
  const compatibleVoices = (voices ?? []).filter((v) =>
    channelLanguage.startsWith("hi") ? v.language === "hi" : v.language === "en",
  );

  return (
    <div className="min-w-0">
      {/* Single-column knob deck — right-side review panel removed */}
      <div className="space-y-5 min-w-0">
        <div className="grid gap-5 md:grid-cols-2">
          <CardShell label="Form" hint="Short = ≤90s vertical · Long = multi-min horizontal">
            <div className="grid grid-cols-2 gap-2">
              {(["short", "long"] as const).map((kind) => {
                const sel = (values.length_kind ?? "short") === kind;
                return (
                  <button
                    key={kind}
                    type="button"
                    onClick={() => onChange("length_kind", kind)}
                    className={cn(
                      "rounded-md border px-3 py-3 text-left transition-colors",
                      sel
                        ? "border-foreground/45 bg-foreground/10 text-foreground"
                        : "border-border bg-surface hover:border-border-strong",
                    )}
                  >
                    <div className="text-[13px] font-medium tracking-tight">
                      {kind === "short" ? "Short form" : "Long form"}
                    </div>
                    <div className="mt-1 text-[11px] text-muted-foreground">
                      {kind === "short" ? "9:16 · ≤90s · Shorts feed" : "16:9 · multi-min · main feed"}
                    </div>
                  </button>
                );
              })}
            </div>
            {(values.length_kind ?? "short") === "long" && (
              <div className="mt-3">
                <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  Target duration
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2">
                  {([
                    { v: 30, label: "30 min" },
                    { v: 60, label: "60 min" },
                    { v: 120, label: "2 hr" },
                  ] as const).map((opt) => {
                    const cur = Number(values.length_minutes ?? 30);
                    const sel = cur === opt.v;
                    return (
                      <button
                        key={opt.v}
                        type="button"
                        onClick={() => onChange("length_minutes", opt.v)}
                        className={cn(
                          "rounded-md border px-3 py-2 text-center text-[12px] font-medium tracking-tight transition-colors",
                          sel
                            ? "border-foreground/45 bg-foreground/10 text-foreground"
                            : "border-border bg-surface hover:border-border-strong",
                        )}
                      >
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </CardShell>

          {sourceKindField && (
            <CardShell label="Source" hint={sourceKindField.help ?? undefined}>
              <Select
                value={
                  typeof values.source_kind === "string"
                    ? (values.source_kind as string)
                    : (sourceKindField.default as string | undefined)
                }
                onValueChange={(v) => onChange("source_kind", v)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Auto" />
                </SelectTrigger>
                <SelectContent>
                  {(sourceKindField.options ?? []).map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {(values.source_kind === "reddit_url" ||
                values.source_kind === "wikipedia_topic" ||
                values.source_kind === "youtube_video") &&
                sourceRefField && (
                  <Input
                    className="mt-2 bg-surface-2"
                    placeholder={sourceRefField.placeholder}
                    value={typeof values.source_ref === "string" ? (values.source_ref as string) : ""}
                    onChange={(e) => onChange("source_ref", e.target.value)}
                  />
                )}
            </CardShell>
          )}
        </div>

        {topicField && (
          <TopicCard
            channel={channel.key}
            variant={variant}
            language={schema.language}
            values={values}
            field={topicField}
            value={typeof values.topic === "string" ? (values.topic as string) : ""}
            lengthKind={(values.length_kind ?? "short") === "long" ? "long" : "short"}
            onChange={(v) => onChange("topic", v)}
            onPullSource={(item) => {
              onChange("topic", item.topic);
              if (item.source_kind) onChange("source_kind", item.source_kind);
              if (item.source_ref) onChange("source_ref", item.source_ref);
            }}
            sourceLabel={
              typeof values.source_ref === "string" && values.source_ref
                ? String(values.source_ref)
                : null
            }
          />
        )}

        {(voiceField || audioModeField) && (
          <AudioSection
            audioMode={
              typeof values.audio_mode === "string"
                ? (values.audio_mode as "voice" | "song")
                : ((audioModeField?.default as "voice" | "song" | undefined) ?? "voice")
            }
            onAudioModeChange={(m) => onChange("audio_mode", m)}
            voicePicker={
              voiceField ? (
                <VoicePicker
                  voices={compatibleVoices}
                  allLoaded={voices !== null}
                  currentValue={
                    typeof values.voice === "string"
                      ? (values.voice as string)
                      : ((voiceField.default as string | undefined) ?? null)
                  }
                  onChange={(v) => onChange("voice", v)}
                  onCloneAdded={(v) => {
                    setVoices((prev) => {
                      const list = prev ?? [];
                      if (list.some((x) => x.key === v.key)) return list;
                      return [v, ...list];
                    });
                  }}
                  fallbackOptions={voiceField.options ?? []}
                  channelLanguage={channel.language}
                />
              ) : null
            }
            songPicker={
              songStyleField && songVocalGenderField && songModelField ? (
                <SongPicker
                  values={{
                    style:
                      typeof values.song_style === "string"
                        ? (values.song_style as string)
                        : ((songStyleField.default as string | undefined) ?? ""),
                    vocal_gender:
                      ((typeof values.song_vocal_gender === "string"
                        ? values.song_vocal_gender
                        : songVocalGenderField.default) as "f" | "m") ?? "f",
                    model:
                      ((typeof values.song_model === "string"
                        ? values.song_model
                        : songModelField.default) as "V4_5" | "V5") ?? "V4_5",
                  }}
                  onChange={(k, v) => {
                    if (k === "style") onChange("song_style", v);
                    if (k === "vocal_gender") onChange("song_vocal_gender", v);
                    if (k === "model") onChange("song_model", v);
                  }}
                  channelLanguage={channel.language}
                />
              ) : null
            }
          />
        )}

        {visualSourceField && (
          <VisualSourceCard
            field={visualSourceField}
            value={
              typeof values.visual_source === "string"
                ? (values.visual_source as string)
                : ((visualSourceField.default as string | undefined) ?? "ai")
            }
            onChange={(v) => onChange("visual_source", v)}
          />
        )}

        {musicField && (
          <MusicPicker
            channelKey={channel.key}
            music={music}
            currentValue={
              typeof values.music_bed === "string"
                ? (values.music_bed as string)
                : ((musicField.default as string | undefined) ?? "ambient_low")
            }
            onChange={(v) => onChange("music_bed", v)}
            fallbackOptions={musicField.options ?? []}
          />
        )}

        {advancedFields.length > 0 && (
          <AdvancedDrawer fields={advancedFields} values={values} onChange={onChange} />
        )}
      </div>
    </div>
  );
}

function CardShell({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-3">{children}</div>
      {hint && <p className="mt-2 text-[11px] text-muted-foreground/80">{hint}</p>}
    </div>
  );
}

function TopicCard({
  channel,
  variant,
  language,
  values: ctxValues,
  field,
  value,
  lengthKind,
  onChange,
  onPullSource,
  sourceLabel,
}: {
  channel: string;
  variant: string | null;
  language: string;
  values: Record<string, unknown>;
  field: CustomizationField;
  value: string;
  lengthKind: "short" | "long";
  onChange: (v: string) => void;
  onPullSource: (item: DiscoverItem) => void;
  sourceLabel: string | null;
}) {
  const [pulling, setPulling] = useState(false);
  const [pulled, setPulled] = useState<DiscoverItem | null>(null);
  // Track topics already shown so successive clicks return variety.
  const [shownTopics, setShownTopics] = useState<string[]>([]);
  // Button is always available — the backend now blends a native source
  // (when one exists) with an LLM brainstorm fallback so every channel
  // returns a usable suggestion.
  const supportsAutoPull = true;

  async function pull() {
    setPulling(true);
    try {
      const item = await discoverApi.pickOne(channel, {
        variant,
        length_kind: lengthKind,
        language,
        niche_key: variant,
        values: ctxValues,
        avoid: shownTopics,
      });
      onPullSource(item);
      setPulled(item);
      setShownTopics((prev) =>
        prev.includes(item.topic) ? prev : [...prev, item.topic].slice(-20),
      );
      toast.success("Topic generated", { description: item.source_label });
    } catch (e) {
      toast.error("Auto-generate failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setPulling(false);
    }
  }

  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Topic
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">
            {lengthKind === "long" ? "What's the long-form video about?" : "What's the Short about?"}
          </div>
        </div>
        {supportsAutoPull && (
          <Button variant="outline" size="sm" onClick={pull} disabled={pulling}>
            {pulling ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                Generating
              </>
            ) : (
              <>
                <Sparkles className="h-3.5 w-3.5" />
                {pulled ? "Generate another" : "Auto-generate topic"}
              </>
            )}
          </Button>
        )}
      </div>

      <Textarea
        className="mt-4 min-h-[88px] bg-surface-2 text-[13.5px]"
        placeholder={field.placeholder}
        maxLength={field.maxLength ?? 500}
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          if (pulled) setPulled(null);
        }}
      />

      <AnimatePresence>
        {pulled && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.18 }}
            className="mt-3 rounded-md border border-emerald-500/25 bg-emerald-500/5 p-3"
          >
            <div className="flex items-center gap-2">
              <Sparkles className="h-3 w-3 text-emerald-300" />
              <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-emerald-200">
                Pulled from
              </span>
              <span className="text-[11.5px] text-foreground/90">{pulled.source_label}</span>
              {pulled.source_ref && (
                <a
                  href={pulled.source_ref}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-auto inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
                >
                  view source
                  <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </div>
            {pulled.source_excerpt && (
              <p className="mt-2 line-clamp-3 text-[12px] leading-relaxed text-muted-foreground">
                {pulled.source_excerpt}
              </p>
            )}
          </motion.div>
        )}
        {!pulled && sourceLabel && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="mt-2 truncate font-mono text-[10.5px] text-muted-foreground"
          >
            source · {sourceLabel}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function VoicePicker({
  voices,
  allLoaded,
  currentValue,
  onChange,
  onCloneAdded,
  fallbackOptions,
  channelLanguage,
}: {
  voices: VoiceInfo[];
  allLoaded: boolean;
  currentValue: string | null;
  onChange: (v: string) => void;
  onCloneAdded: (v: VoiceInfo) => void;
  fallbackOptions: { value: string; label: string }[];
  channelLanguage: string;
}) {
  const [cloneOpen, setCloneOpen] = useState(false);
  const [clonePreset, setClonePreset] = useState<{ name: string; language: string; hint?: string } | null>(null);
  const [famous, setFamous] = useState<FamousVoiceTemplate[] | null>(null);
  const [tab, setTab] = useState<"library" | "famous" | "clones">("library");
  const [search, setSearch] = useState("");
  // Multi-dimensional filters
  const [genderFilter, setGenderFilter] = useState<"any" | "male" | "female">("any");
  const [toneFilter, setToneFilter] = useState<string>("any");
  const [useCase, setUseCase] = useState<string>("all");
  const [showTTS, setShowTTS] = useState(false);

  useEffect(() => {
    voicesApi.famous().then((r) => setFamous(r.templates)).catch(() => setFamous([]));
  }, []);

  const optionList: VoiceInfo[] = voices.length > 0
    ? voices
    : fallbackOptions.map((o) => ({
        key: o.value,
        label: o.label,
        language: "",
        gender: "",
        style: "",
        sample_url: null,
      }));

  const library = optionList.filter((v) => !v.is_clone);
  const yourClones = optionList.filter((v) => v.is_clone);

  const TONE_LABELS: Record<string, string> = {
    deep: "Deep",
    warm: "Warm",
    intense: "Intense / Exciting",
    bright: "Bright",
    measured: "Measured",
    expressive: "Expressive",
    raspy: "Raspy",
    british: "British",
    indian: "Indian",
  };
  const USE_CASE_LABELS: Record<string, string> = {
    documentary: "Documentary",
    narrator: "Narrator",
    storyteller: "Storyteller",
    sports: "Sports",
    news: "News",
    trailer: "Trailer",
    audiobook: "Audiobook",
    historical: "Historical",
    drama: "Drama",
    conversational: "Conversational",
    podcast: "Podcast",
    explainer: "Explainer",
    asmr: "ASMR",
    kids: "Kids",
  };

  // Discover what tones / use-cases actually exist in the unfiltered library
  const tonesPresent = (() => {
    const s = new Set<string>();
    library.forEach((v) => (v.tones ?? []).forEach((t) => s.add(t)));
    // Stable, opinionated order — most common moods first.
    const order = ["deep", "warm", "intense", "bright", "measured", "expressive", "raspy", "british", "indian"];
    return order.filter((t) => s.has(t));
  })();
  const useCasesPresent = (() => {
    const s = new Set<string>();
    library.forEach((v) => (v.use_cases ?? []).forEach((uc) => s.add(uc)));
    return Array.from(s)
      .filter((uc) => USE_CASE_LABELS[uc])
      .sort();
  })();

  const matches = (text: string) =>
    !search.trim() || text.toLowerCase().includes(search.trim().toLowerCase());

  // Has the user actually narrowed the field?
  const filtersActive =
    genderFilter !== "any" ||
    toneFilter !== "any" ||
    useCase !== "all" ||
    search.trim().length > 0;

  const filteredLibrary = library
    .filter((v) => showTTS || v.provider === "human" || !v.provider)
    .filter((v) => genderFilter === "any" || v.gender === genderFilter)
    .filter((v) => toneFilter === "any" || (v.tones ?? []).includes(toneFilter))
    .filter((v) => useCase === "all" || (v.use_cases ?? []).includes(useCase))
    .filter((v) => matches(`${v.label} ${v.style} ${v.notes ?? ""}`))
    .sort((a, b) => {
      const rank = (v: VoiceInfo) =>
        v.provider === "human" || !v.provider ? 0 :
        v.provider === "kokoro" ? 1 :
        v.provider === "edge" ? 2 :
        v.provider === "openai" ? 3 :
        v.provider === "elevenlabs" ? 4 : 5;
      return rank(a) - rank(b) || a.label.localeCompare(b.label);
    });

  // Recommended starter set: the channel default + the top 3 channel-language
  // Default browse set: ALL human voices for this channel's language, sorted
  // with the channel default first. Wrapped in a scrollable container below
  // so the user can flip through everything without committing to a filter.
  const recommended = (() => {
    const langMatch = (v: VoiceInfo) =>
      channelLanguage.startsWith("hi") ? v.language === "hi" : v.language === "en";
    const playable = library.filter(
      (v) => langMatch(v) && (v.provider === "human" || !v.provider),
    );
    const def = playable.find((v) => v.key === currentValue);
    const rest = playable.filter((v) => v.key !== currentValue);
    return [...(def ? [def] : []), ...rest];
  })();

  const humanCount = library.filter((v) => v.provider === "human" || !v.provider).length;
  const ttsCount = library.length - humanCount;
  const filteredFamous = (famous ?? []).filter((t) =>
    (channelLanguage.startsWith("hi") ? t.language === "hi" : t.language === "en") &&
    matches(`${t.label} ${t.description}`),
  );
  const filteredClones = yourClones.filter((v) => matches(`${v.label} ${v.style}`));

  function openTemplate(t: FamousVoiceTemplate) {
    setClonePreset({ name: t.label, language: t.language, hint: t.hint });
    setCloneOpen(true);
  }

  function openBlankClone() {
    setClonePreset(null);
    setCloneOpen(true);
  }

  function clearFilters() {
    setGenderFilter("any");
    setToneFilter("any");
    setUseCase("all");
    setSearch("");
  }

  return (
    <div className="rounded-xl border border-border bg-surface p-5 min-w-0 max-w-full overflow-hidden">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Voice
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">
            Audition the narrator. Click ▶ to listen.
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <input
            type="search"
            placeholder="Search voices…"
            className="h-8 w-44 rounded-md border border-border bg-surface-2 px-3 text-[12px] outline-none placeholder:text-muted-foreground focus:border-border-strong"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Button
            variant="outline"
            size="sm"
            onClick={openBlankClone}
            className="text-muted-foreground hover:text-foreground"
          >
            <Wand2 className="h-3.5 w-3.5" />
            Clone a voice
          </Button>
        </div>
      </div>

      {tab === "library" && (
        <div className="mt-3">
          {/* Single horizontal-scroll row of compact filters */}
          <div className="flex items-center gap-2 overflow-x-auto pb-1">
            <FilterSelect
              label="Voice"
              value={genderFilter}
              options={[
                { value: "any", label: "Any voice" },
                { value: "male", label: "Male" },
                { value: "female", label: "Female" },
              ]}
              onChange={(v) => setGenderFilter(v as typeof genderFilter)}
            />
            <FilterSelect
              label="Tone"
              value={toneFilter}
              options={[
                { value: "any", label: "Any tone" },
                ...tonesPresent.map((t) => ({ value: t, label: TONE_LABELS[t] ?? t })),
              ]}
              onChange={setToneFilter}
            />
            <FilterSelect
              label="Use case"
              value={useCase}
              options={[
                { value: "all", label: "All uses" },
                ...useCasesPresent.map((uc) => ({
                  value: uc,
                  label: USE_CASE_LABELS[uc] ?? uc,
                })),
              ]}
              onChange={setUseCase}
            />

            {filtersActive && (
              <button
                type="button"
                onClick={clearFilters}
                className="inline-flex h-8 shrink-0 items-center rounded-md border border-border bg-surface-2 px-2.5 text-[11px] font-mono uppercase tracking-[0.14em] text-muted-foreground hover:border-border-strong hover:text-foreground"
              >
                clear
              </button>
            )}

            {ttsCount > 0 && (
              <button
                type="button"
                onClick={() => setShowTTS((x) => !x)}
                className={cn(
                  "ml-auto inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border px-2.5 text-[11px] font-mono uppercase tracking-[0.14em] transition-colors",
                  showTTS
                    ? "border-foreground/40 bg-foreground/10 text-foreground"
                    : "border-border bg-surface-2 text-muted-foreground hover:border-border-strong hover:text-foreground",
                )}
              >
                <span
                  className={cn(
                    "h-1.5 w-1.5 rounded-full",
                    showTTS ? "bg-emerald-300" : "bg-border-strong",
                  )}
                />
                TTS · {ttsCount}
              </button>
            )}
          </div>
        </div>
      )}

      <div className="mt-4">
        {tab === "library" && (
          !allLoaded ? (
            <div className="grid gap-2 sm:grid-cols-2">
              {Array(4).fill(0).map((_, i) => <Skeleton key={i} className="h-16" />)}
            </div>
          ) : !filtersActive ? (
            // Default state: all human voices for this language, scrollable.
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  All voices · {channelLanguage.startsWith("hi") ? "Hindi" : "English"}
                </span>
                <span className="font-mono text-[10px] text-muted-foreground/70">
                  {recommended.length} voices · scroll or filter
                </span>
              </div>
              {recommended.length === 0 ? (
                <VoiceEmpty>No human voices for this language yet — try Famous voices or Clone a voice.</VoiceEmpty>
              ) : (
                <div className="overflow-x-auto rounded-md border border-border bg-surface-2/30 p-2 [scrollbar-width:thin]">
                  <div className="flex flex-nowrap gap-2">
                    {recommended.map((v) => (
                      <div key={v.key} className="w-56 shrink-0">
                        <PreviewableTile
                          selected={v.key === currentValue}
                          onSelect={() => onChange(v.key)}
                          title={v.label}
                          subtitle={v.style || undefined}
                          badge={v.language ? v.language.toUpperCase() : undefined}
                          preview={<AudioSampleButton src={v.sample_url} label={`Play ${v.label}`} />}
                        />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : filteredLibrary.length === 0 ? (
            <VoiceEmpty>
              No voices match these filters. Try{" "}
              <button onClick={clearFilters} className="text-foreground underline-offset-2 hover:underline">
                clearing them
              </button>
              {!showTTS && ttsCount > 0 && (
                <>
                  {" or "}
                  <button onClick={() => setShowTTS(true)} className="text-foreground underline-offset-2 hover:underline">
                    show TTS presets
                  </button>
                </>
              )}
              .
            </VoiceEmpty>
          ) : (
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                  {filteredLibrary.length} {filteredLibrary.length === 1 ? "match" : "matches"}
                </span>
              </div>
              <div className="overflow-x-auto rounded-md border border-border bg-surface-2/30 p-2 [scrollbar-width:thin]">
                <div className="flex flex-nowrap gap-2">
                  {filteredLibrary.map((v) => (
                    <div key={v.key} className="w-56 shrink-0">
                      <PreviewableTile
                        selected={v.key === currentValue}
                        onSelect={() => onChange(v.key)}
                        title={v.label}
                        subtitle={v.style || undefined}
                        badge={v.language ? v.language.toUpperCase() : undefined}
                        preview={<AudioSampleButton src={v.sample_url} label={`Play ${v.label}`} />}
                      />
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )
        )}

        {tab === "famous" && (
          famous === null ? (
            <div className="grid gap-2 sm:grid-cols-2">
              {Array(6).fill(0).map((_, i) => <Skeleton key={i} className="h-16" />)}
            </div>
          ) : filteredFamous.length === 0 ? (
            <VoiceEmpty>
              No matches in {famous.length} curated celebrity templates.
            </VoiceEmpty>
          ) : (
            <>
              <p className="mb-3 text-[12px] leading-relaxed text-muted-foreground">
                Pick a name → upload a 5–15s clip you find online (interview,
                speech, audiobook). The renderer clones the voice for your Short.
              </p>
              <div className="grid gap-2 sm:grid-cols-2">
                {filteredFamous.map((t) => (
                  <FamousVoiceCard key={t.key} template={t} onClick={() => openTemplate(t)} />
                ))}
              </div>
            </>
          )
        )}

        {tab === "clones" && (
          filteredClones.length === 0 ? (
            <VoiceEmpty>
              No clones yet — hit{" "}
              <span className="text-foreground">Clone a voice</span> top-right to
              upload or record one.
            </VoiceEmpty>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2">
              {filteredClones.map((v) => (
                <PreviewableTile
                  key={v.key}
                  selected={v.key === currentValue}
                  onSelect={() => onChange(v.key)}
                  title={v.label}
                  subtitle={v.style || undefined}
                  badge={v.language ? v.language.toUpperCase() : undefined}
                  preview={<AudioSampleButton src={v.sample_url} label={`Play ${v.label}`} />}
                />
              ))}
            </div>
          )
        )}
      </div>

      <CloneVoiceDialog
        open={cloneOpen}
        onOpenChange={setCloneOpen}
        defaultLanguage={channelLanguage.startsWith("hi") ? "hi" : "en"}
        preset={clonePreset}
        onCloned={(v) => {
          onCloneAdded(v);
          onChange(v.key);
          setTab("clones");
        }}
      />
    </div>
  );
}

function VoiceTab({
  active,
  onClick,
  count,
  children,
}: {
  active: boolean;
  onClick: () => void;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded px-3 text-[12px] tracking-tight transition-colors",
        active ? "bg-surface text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
      <span
        className={cn(
          "rounded-md px-1.5 py-px font-mono text-[10px] tabular-nums",
          active ? "bg-surface-2 text-muted-foreground" : "text-muted-foreground/70",
        )}
      >
        {count}
      </span>
    </button>
  );
}

function VoiceEmpty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-border bg-surface-2/40 px-3 py-6 text-center text-[12px] text-muted-foreground">
      {children}
    </div>
  );
}

function UseCaseChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex h-7 items-center rounded-full border px-3 text-[11.5px] tracking-tight transition-colors",
        active
          ? "border-foreground/40 bg-foreground/10 text-foreground"
          : "border-border bg-surface-2 text-muted-foreground hover:border-border-strong hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

function FilterRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mr-1 w-12 shrink-0 font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </span>
      {children}
    </div>
  );
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  // Compact labelled select — much tighter than long pill rows. The label
  // sits to the LEFT (not above) so the whole control fits in a single
  // row with the other filters.
  return (
    <div className="relative inline-flex h-8 shrink-0 items-stretch overflow-hidden rounded-md border border-border bg-surface-2 transition-colors hover:border-border-strong">
      <span className="flex shrink-0 items-center bg-surface px-2 font-mono text-[9.5px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-full appearance-none bg-transparent px-2 pr-6 text-[12px] font-medium tracking-tight text-foreground focus:outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
    </div>
  );
}

function FamousVoiceCard({
  template,
  onClick,
}: {
  template: FamousVoiceTemplate;
  onClick: () => void;
}) {
  return (
    <div className="group relative flex items-center gap-3 rounded-lg border border-border bg-surface-2/60 p-3 text-left transition-colors hover:border-border-strong">
      {/* ▶ inline preview button — plays the closest-voice's preview */}
      {template.closest_voice_url ? (
        <AudioSampleButton
          src={template.closest_voice_url}
          label={`Preview ${template.label} (closest match)`}
        />
      ) : (
        <span
          className="grid h-9 w-9 shrink-0 place-items-center rounded-full border border-border bg-surface-2 text-muted-foreground/40"
          title="No preview available — click Clone to upload your own clip"
        >
          <Play className="ml-0.5 h-3.5 w-3.5" />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full border border-border bg-surface font-mono text-[9px] font-medium tracking-tighter text-foreground">
            {template.initial_letters}
          </span>
          <span className="truncate text-[12.5px] font-medium tracking-tight">{template.label}</span>
          <span className="rounded-md border border-border bg-surface px-1.5 py-px font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground">
            {template.language}
          </span>
        </div>
        <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
          {template.description}
        </div>
        {template.closest_voice_url && (
          <div className="mt-1 truncate font-mono text-[9.5px] uppercase tracking-[0.14em] text-muted-foreground/70">
            ▶ closest match · clone for the real voice →
          </div>
        )}
      </div>
      {/* Clone CTA — primary action of the card */}
      <button
        type="button"
        onClick={onClick}
        className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 text-[11px] font-medium tracking-tight text-foreground transition-colors hover:border-border-strong hover:bg-surface-2"
      >
        <Wand2 className="h-3 w-3" />
        Clone
      </button>
    </div>
  );
}

/* ----------------------------- AudioSection (Voice / Song flip) ----------------------------- */

/**
 * Wraps the Voice picker and Song picker with a 2-way segmented tab so
 * the user can flip between spoken narration (TTS) and a sung song
 * (Suno) without leaving the Customize step. Default tab comes from the
 * channel YAML's audio_provider (read by the schema) — rhymetimejunction
 * lands on Song; everyone else on Voice. Both tabs are always present.
 */
function AudioSection({
  audioMode,
  onAudioModeChange,
  voicePicker,
  songPicker,
}: {
  audioMode: "voice" | "song";
  onAudioModeChange: (m: "voice" | "song") => void;
  voicePicker: React.ReactNode;
  songPicker: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-5 min-w-0 max-w-full overflow-hidden">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
            Audio
          </div>
          <div className="mt-0.5 text-[13px] font-medium tracking-tight">
            {audioMode === "song"
              ? "Generate a sung track via Suno."
              : "Audition the narrator. Click ▶ to listen."}
          </div>
        </div>
        <div
          role="tablist"
          aria-label="Audio mode"
          className="inline-flex shrink-0 rounded-md border border-border bg-surface-2 p-0.5"
        >
          {(
            [
              { v: "voice", label: "🎙 Voice" },
              { v: "song", label: "🎵 Song" },
            ] as const
          ).map((opt) => {
            const sel = audioMode === opt.v;
            return (
              <button
                key={opt.v}
                role="tab"
                aria-selected={sel}
                type="button"
                onClick={() => onAudioModeChange(opt.v)}
                className={cn(
                  "rounded px-3 py-1 text-[12px] font-medium tracking-tight transition-colors",
                  sel
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="mt-4">
        {audioMode === "song" ? songPicker : voicePicker}
      </div>
    </div>
  );
}

/* ----------------------------- VisualSourceCard (AI / Footage / Both) ----------------------------- */

/**
 * 3-way segmented picker for what plays in the video background.
 * Defaults are derived per-channel by the backend schema so the picker
 * pre-selects the channel's house style (animated/rhyme → AI,
 * footage_only → Real footage, split_screen → Both). Honored by
 * pipeline/render/shorts.py via cfg["visual_source"].
 */
function VisualSourceCard({
  field,
  value,
  onChange,
}: {
  field: CustomizationField;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
        Background visuals
      </div>
      <div className="mt-0.5 text-[13px] font-medium tracking-tight">
        What plays behind the audio.
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        {(field.options ?? []).map((opt) => {
          const sel = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChange(opt.value)}
              className={cn(
                "rounded-md border px-3 py-3 text-left transition-colors",
                sel
                  ? "border-foreground/45 bg-foreground/10 text-foreground"
                  : "border-border bg-surface hover:border-border-strong",
              )}
            >
              <div className="text-[12.5px] font-medium tracking-tight">
                {opt.label}
              </div>
              {opt.description && (
                <div className="mt-1 text-[10.5px] leading-relaxed text-muted-foreground">
                  {opt.description}
                </div>
              )}
            </button>
          );
        })}
      </div>
      {field.help && (
        <p className="mt-2 text-[11px] text-muted-foreground/80">{field.help}</p>
      )}
    </div>
  );
}

function MusicPicker({
  channelKey,
  music,
  currentValue,
  onChange,
  fallbackOptions,
}: {
  channelKey: string;
  music: MusicBed[] | null;
  currentValue: string | null;
  onChange: (v: string) => void;
  fallbackOptions: { value: string; label: string }[];
}) {
  // Match the schema's preset values (off / ambient_low / ambient_med /
  // cinematic / upbeat) against shared procedural beds in data/music/
  // so the chips are previewable, not dead "render-time placeholder".
  const sharedByKey = new Map<string, MusicBed>();
  (music ?? []).forEach((m) => {
    if (m.channel === "shared") sharedByKey.set(m.key, m);
  });
  const channelBeds = (music ?? []).filter((m) => m.channel === channelKey);

  return (
    <div className="rounded-xl border border-border bg-surface p-5">
      <div className="flex items-center gap-2">
        <Music className="h-3.5 w-3.5 text-muted-foreground" />
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          Music bed
        </div>
      </div>
      <div className="mt-0.5 text-[13px] font-medium tracking-tight">
        Pick a vibe. ▶ to audition.
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {fallbackOptions.map((o) => {
          const shared = sharedByKey.get(o.value);
          const sampleUrl = shared?.sample_url ?? null;
          return (
            <PreviewableTile
              key={o.value}
              selected={o.value === currentValue}
              onSelect={() => onChange(o.value)}
              title={o.label}
              subtitle={sampleUrl ? "Procedural · 12s loop" : "render-time placeholder"}
              preview={
                <AudioSampleButton
                  src={sampleUrl}
                  label={sampleUrl ? `Play ${o.label}` : "No audio sample for this preset"}
                />
              }
            />
          );
        })}
      </div>

      {channelBeds.length > 0 && (
        <div className="mt-5 border-t border-border pt-4">
          <div className="mb-2 font-mono text-[9.5px] uppercase tracking-[0.18em] text-muted-foreground">
            Curated for this channel
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {channelBeds.map((b) => (
              <PreviewableTile
                key={`${b.channel}:${b.key}`}
                selected={b.key === currentValue}
                onSelect={() => onChange(b.key)}
                title={b.label}
                subtitle={b.channel}
                preview={<AudioSampleButton src={b.sample_url} label={`Play ${b.label}`} />}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function AdvancedDrawer({
  fields,
  values,
  onChange,
}: {
  fields: CustomizationField[];
  values: Record<string, unknown>;
  onChange: (k: string, v: unknown) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-border bg-surface">
      <button
        type="button"
        onClick={() => setOpen((x) => !x)}
        className="flex w-full items-center justify-between gap-2 px-5 py-3 text-left transition-colors hover:bg-surface-2"
      >
        <div className="flex items-center gap-2">
          <Settings2 className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-muted-foreground">
            More options
          </span>
          <span className="text-[11.5px] text-muted-foreground/70">
            · captions, visibility, notes
          </span>
        </div>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 text-muted-foreground transition-transform",
            open && "rotate-180",
          )}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="grid gap-4 border-t border-border p-5 md:grid-cols-2">
              {fields.map((f) => (
                <FieldRenderer
                  key={f.key}
                  field={f}
                  value={values[f.key]}
                  onChange={(v) => onChange(f.key, v)}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function FieldRenderer({
  field,
  value,
  onChange,
}: {
  field: CustomizationField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const wide = field.kind === "textarea" || field.key === "topic" || field.key === "notes";
  return (
    <div className={wide ? "md:col-span-2" : undefined}>
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={field.key} className="text-[12.5px] tracking-tight">
          {field.label}
          {field.required && <span className="ml-1 text-rose-400/80">*</span>}
        </Label>
        {field.kind === "slider" && (
          <span className="num font-mono text-[11px] text-muted-foreground">
            {String(value ?? field.default ?? "")}
            {field.key === "length_s" ? "s" : ""}
          </span>
        )}
      </div>

      <div className="mt-2">
        {field.kind === "select" && (
          <Select
            value={value !== undefined && value !== null ? String(value) : undefined}
            onValueChange={(v) => onChange(v)}
          >
            <SelectTrigger id={field.key}>
              <SelectValue placeholder="Choose…" />
            </SelectTrigger>
            <SelectContent>
              {(field.options ?? []).map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  <div className="flex flex-col">
                    <span>{o.label}</span>
                    {o.description && (
                      <span className="font-mono text-[10px] text-muted-foreground">{o.description}</span>
                    )}
                  </div>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}

        {(field.kind === "text" || field.kind === "url") && (
          <Input
            id={field.key}
            type={field.kind === "url" ? "url" : "text"}
            placeholder={field.placeholder}
            maxLength={field.maxLength}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value)}
            className="bg-surface-2"
          />
        )}

        {field.kind === "textarea" && (
          <Textarea
            id={field.key}
            placeholder={field.placeholder}
            maxLength={field.maxLength}
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value)}
            className="min-h-[88px] bg-surface-2"
          />
        )}

        {field.kind === "number" && (
          <Input
            id={field.key}
            type="number"
            min={field.min}
            max={field.max}
            step={field.step}
            value={typeof value === "number" ? value : (value as string) ?? ""}
            onChange={(e) => onChange(Number(e.target.value))}
            className="bg-surface-2"
          />
        )}

        {field.kind === "slider" && (
          <Slider
            value={[Number(value ?? field.default ?? field.min ?? 0)]}
            min={field.min ?? 0}
            max={field.max ?? 100}
            step={field.step ?? 1}
            onValueChange={(v) => onChange(v[0])}
            className="mt-3"
          />
        )}

        {field.kind === "switch" && (
          <div className="mt-2 flex items-center gap-3">
            <Switch
              id={field.key}
              checked={Boolean(value)}
              onCheckedChange={(v) => onChange(v)}
            />
            <span className="text-[12px] text-muted-foreground">
              {value ? "Enabled" : "Disabled"}
            </span>
          </div>
        )}

        {field.kind === "datetime" && (
          <Input
            id={field.key}
            type="datetime-local"
            value={typeof value === "string" ? value : ""}
            onChange={(e) => onChange(e.target.value)}
            className="bg-surface-2"
          />
        )}
      </div>

      {field.help && (
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-muted-foreground/80">
          {field.help}
        </p>
      )}
    </div>
  );
}
