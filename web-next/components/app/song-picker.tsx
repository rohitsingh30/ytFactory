"use client";

import { Music } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * SongPicker — Suno-side counterpart of VoicePicker. Surfaces only the
 * three knobs the wrapper at pipeline/tts/song.py::synth_via_sunoapi
 * actually consumes: style (free-text), vocal_gender (f|m), model
 * (V4_5|V5). instrumental + title are intentionally not exposed
 * (rhymes need lyrics; title is auto-derived from slug).
 *
 * Rendered inside <AudioSection> when the user picks the "Song" tab.
 */
export interface SongPickerValues {
  style: string;
  vocal_gender: "f" | "m";
  model: "V4_5" | "V5";
}

interface Props {
  values: SongPickerValues;
  onChange: (k: keyof SongPickerValues, v: string) => void;
  channelLanguage: string;
}

export function SongPicker({ values, onChange, channelLanguage }: Props) {
  const isHindi = channelLanguage.startsWith("hi");
  return (
    <div className="space-y-5">
      <div>
        <div className="mb-1.5 flex items-baseline justify-between">
          <Label htmlFor="song-style" className="text-[12px] font-medium tracking-tight">
            Style
          </Label>
          <span className="text-[10.5px] text-muted-foreground">
            Genre · instruments · tempo
          </span>
        </div>
        <Textarea
          id="song-style"
          rows={3}
          value={values.style}
          onChange={(e) => onChange("style", e.target.value)}
          placeholder={
            isHindi
              ? "cheerful upbeat children's nursery rhyme, female lead with kids choir, gentle acoustic guitar + tabla, 120 BPM"
              : "uplifting cinematic pop, female lead, warm strings, 110 BPM"
          }
          maxLength={500}
          className="bg-surface-2"
        />
        <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
          Passed verbatim to Suno&apos;s <code className="font-mono text-[10.5px]">style</code>{" "}
          parameter. One sentence covering genre, lead instruments, and tempo works best.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <SegmentedField label="Vocal gender">
          {(
            [
              { v: "f", label: "Female" },
              { v: "m", label: "Male" },
            ] as const
          ).map((opt) => (
            <SegmentedOption
              key={opt.v}
              selected={values.vocal_gender === opt.v}
              onClick={() => onChange("vocal_gender", opt.v)}
            >
              {opt.label}
            </SegmentedOption>
          ))}
        </SegmentedField>

        <SegmentedField label="Suno model">
          {(
            [
              { v: "V4_5", label: "V4_5", hint: "Best for kids' / bilingual" },
              { v: "V5", label: "V5", hint: "Newer · richer production" },
            ] as const
          ).map((opt) => (
            <SegmentedOption
              key={opt.v}
              selected={values.model === opt.v}
              onClick={() => onChange("model", opt.v)}
              hint={opt.hint}
            >
              {opt.label}
            </SegmentedOption>
          ))}
        </SegmentedField>
      </div>

      <div className="rounded-md border border-border bg-surface-2/40 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
        <Music className="mr-1.5 inline h-3 w-3 align-[-2px]" />
        Suno generates a fresh take each render — same style + lyrics produce a
        different mix every time. Re-render to roll a new variation.
      </div>
    </div>
  );
}

function SegmentedField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 text-[11.5px] font-medium tracking-tight">{label}</div>
      <div className="grid grid-cols-2 gap-2">{children}</div>
    </div>
  );
}

function SegmentedOption({
  selected,
  onClick,
  children,
  hint,
}: {
  selected: boolean;
  onClick: () => void;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-md border px-3 py-2 text-left transition-colors",
        selected
          ? "border-foreground/45 bg-foreground/10 text-foreground"
          : "border-border bg-surface hover:border-border-strong",
      )}
    >
      <div className="text-[12.5px] font-medium tracking-tight">{children}</div>
      {hint && (
        <div className="mt-0.5 text-[10.5px] text-muted-foreground">{hint}</div>
      )}
    </button>
  );
}
