"use client";

import { useEffect, useRef, useState } from "react";
import {
  AudioLines,
  Check,
  Loader2,
  Mic,
  Pause,
  Play,
  Square,
  Trash2,
  Upload,
  Wand2,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { voicesApi, type VoiceInfo } from "@/lib/api";
import { cn } from "@/lib/utils";

interface CloneVoiceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called after a successful clone. The dialog passes back the new voice. */
  onCloned: (voice: VoiceInfo) => void;
  defaultLanguage?: string;
  /** Optional preset that pre-fills name + language + sourcing hint. */
  preset?: {
    name: string;
    language: string;
    hint?: string;
  } | null;
}

const ACCEPTED_TYPES =
  ".wav,.mp3,.m4a,.ogg,.flac,.aac,.webm,audio/wav,audio/mpeg,audio/mp4,audio/ogg,audio/flac";

export function CloneVoiceDialog({
  open,
  onOpenChange,
  onCloned,
  defaultLanguage = "en",
  preset = null,
}: CloneVoiceDialogProps) {
  const [stage, setStage] = useState<"pick" | "ready" | "cloning" | "preview">("pick");
  const [file, setFile] = useState<File | null>(null);
  const [recordingBlob, setRecordingBlob] = useState<Blob | null>(null);
  const [name, setName] = useState("");
  const [language, setLanguage] = useState<string>(defaultLanguage);
  const [transcript, setTranscript] = useState("");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recElapsed, setRecElapsed] = useState(0);
  const [clonedVoice, setClonedVoice] = useState<VoiceInfo | null>(null);
  const [clonedNote, setClonedNote] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const inputRef = useRef<HTMLInputElement | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recChunksRef = useRef<BlobPart[]>([]);
  const recTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Apply preset whenever the dialog opens with one.
  useEffect(() => {
    if (open && preset) {
      setName(preset.name);
      setLanguage(preset.language || defaultLanguage);
    }
  }, [open, preset, defaultLanguage]);

  // Reset on close
  useEffect(() => {
    if (open) return;
    const t = setTimeout(() => {
      setStage("pick");
      setFile(null);
      setRecordingBlob(null);
      setName("");
      setLanguage(defaultLanguage);
      setTranscript("");
      setPreviewUrl((u) => {
        if (u) URL.revokeObjectURL(u);
        return null;
      });
      setPreviewPlaying(false);
      setRecording(false);
      setRecElapsed(0);
      setClonedVoice(null);
      setClonedNote(null);
      stopRecorder();
      audioRef.current?.pause();
      audioRef.current = null;
    }, 200);
    return () => clearTimeout(t);
  }, [open, defaultLanguage]);

  function pickFile(f: File) {
    setFile(f);
    setRecordingBlob(null);
    setPreviewUrl((u) => {
      if (u) URL.revokeObjectURL(u);
      return URL.createObjectURL(f);
    });
    if (!name) setName(f.name.replace(/\.[^.]+$/, ""));
    setStage("ready");
  }

  function pickRecording(blob: Blob) {
    setRecordingBlob(blob);
    setFile(null);
    setPreviewUrl((u) => {
      if (u) URL.revokeObjectURL(u);
      return URL.createObjectURL(blob);
    });
    if (!name) setName("My voice");
    setStage("ready");
  }

  async function startRecording() {
    if (recording) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mr = new MediaRecorder(stream);
      recChunksRef.current = [];
      mr.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) recChunksRef.current.push(e.data);
      };
      mr.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(recChunksRef.current, { type: mr.mimeType || "audio/webm" });
        pickRecording(blob);
      };
      mr.start();
      recorderRef.current = mr;
      setRecording(true);
      setRecElapsed(0);
      recTimerRef.current = setInterval(() => {
        setRecElapsed((s) => {
          if (s + 1 >= 15) {
            stopRecorder();
            return 15;
          }
          return s + 1;
        });
      }, 1000);
    } catch (e) {
      toast.error("Microphone unavailable", {
        description: e instanceof Error ? e.message : String(e),
      });
    }
  }

  function stopRecorder() {
    if (recTimerRef.current) {
      clearInterval(recTimerRef.current);
      recTimerRef.current = null;
    }
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      recorderRef.current.stop();
    }
    recorderRef.current = null;
    setRecording(false);
  }

  function togglePreview() {
    if (!previewUrl) return;
    if (!audioRef.current) {
      const el = new Audio(previewUrl);
      el.onended = () => setPreviewPlaying(false);
      el.onpause = () => setPreviewPlaying(false);
      audioRef.current = el;
    }
    if (previewPlaying) {
      audioRef.current.pause();
      setPreviewPlaying(false);
    } else {
      audioRef.current.currentTime = 0;
      audioRef.current.play().then(() => setPreviewPlaying(true)).catch(() => {});
    }
  }

  async function submit() {
    const audioFile =
      file ??
      (recordingBlob
        ? new File([recordingBlob], `${slugish(name)}.webm`, { type: recordingBlob.type })
        : null);
    if (!audioFile) {
      toast.error("Pick or record an audio sample first");
      return;
    }
    if (!name.trim()) {
      toast.error("Give the voice a name");
      return;
    }
    setStage("cloning");
    try {
      const result = await voicesApi.clone({
        name: name.trim(),
        language,
        transcript: transcript.trim(),
        audio: audioFile,
      });
      setClonedVoice(result.voice);
      setClonedNote(result.note || null);
      // Tear down the upload preview; we'll preview the SERVER copy now.
      setPreviewUrl((u) => {
        if (u) URL.revokeObjectURL(u);
        return null;
      });
      audioRef.current?.pause();
      audioRef.current = null;
      setStage("preview");
      toast.success("Voice cloned", {
        description: `${result.voice.label} · ${result.duration_s.toFixed(1)}s`,
      });
    } catch (e) {
      const msg =
        (e as { body?: string; message?: string })?.body ||
        (e instanceof Error ? e.message : String(e));
      toast.error("Clone failed", { description: typeof msg === "string" ? msg : "Unknown error" });
      setStage("ready");
    }
  }

  function useThisVoice() {
    if (!clonedVoice) return;
    onCloned(clonedVoice);
    onOpenChange(false);
  }

  function dropHandlers() {
    return {
      onDragOver: (e: React.DragEvent) => {
        e.preventDefault();
        setDragOver(true);
      },
      onDragLeave: () => setDragOver(false),
      onDrop: (e: React.DragEvent) => {
        e.preventDefault();
        setDragOver(false);
        const f = e.dataTransfer.files[0];
        if (f) pickFile(f);
      },
    };
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Wand2 className="h-4 w-4" />
            {preset ? `Clone ${preset.name}` : "Clone a voice"}
          </DialogTitle>
          <DialogDescription>
            Upload a 5–15 second clip of someone speaking clearly. We&apos;ll convert it
            to the renderer&apos;s reference format and let you preview the result.
          </DialogDescription>
        </DialogHeader>

        {preset?.hint && stage !== "preview" && stage !== "cloning" && (
          <div className="rounded-md border border-violet-500/25 bg-violet-500/5 p-3 text-[12px] leading-relaxed text-violet-100/90">
            <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-violet-300">
              Where to find a clip
            </span>
            <p className="mt-1">{preset.hint}</p>
          </div>
        )}

        {(stage === "pick" || stage === "ready") && (
          <div className="space-y-4">
            {/* Drop / pick / record */}
            <div
              {...dropHandlers()}
              className={cn(
                "relative rounded-xl border border-dashed bg-surface-2/40 px-5 py-6 text-center transition-colors",
                dragOver ? "border-foreground/50 bg-surface-2" : "border-border",
              )}
            >
              <input
                ref={inputRef}
                type="file"
                accept={ACCEPTED_TYPES}
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) pickFile(f);
                }}
              />
              {file || recordingBlob ? (
                <ClipPreview
                  label={file ? file.name : `Recording · ${recElapsed || 0}s`}
                  size={file ? file.size : recordingBlob!.size}
                  playing={previewPlaying}
                  onTogglePlay={togglePreview}
                  onClear={() => {
                    setFile(null);
                    setRecordingBlob(null);
                    setPreviewUrl((u) => {
                      if (u) URL.revokeObjectURL(u);
                      return null;
                    });
                    setStage("pick");
                  }}
                />
              ) : (
                <div className="flex flex-col items-center gap-3">
                  <div className="grid h-10 w-10 place-items-center rounded-full border border-border bg-surface text-muted-foreground">
                    <AudioLines className="h-4 w-4" />
                  </div>
                  <div className="text-[13px] tracking-tight text-foreground">
                    Drop an audio file, or
                  </div>
                  <div className="flex items-center gap-2">
                    <Button variant="outline" size="sm" onClick={() => inputRef.current?.click()}>
                      <Upload className="h-3.5 w-3.5" />
                      Choose file
                    </Button>
                    <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
                      or
                    </span>
                    {recording ? (
                      <Button variant="destructive" size="sm" onClick={stopRecorder}>
                        <Square className="h-3.5 w-3.5" />
                        Stop · {recElapsed}s
                      </Button>
                    ) : (
                      <Button variant="outline" size="sm" onClick={startRecording}>
                        <Mic className="h-3.5 w-3.5" />
                        Record
                      </Button>
                    )}
                  </div>
                  <div className="font-mono text-[10px] text-muted-foreground/80">
                    wav · mp3 · m4a · ogg · ≤ 25 MB · auto-trims to 15s
                  </div>
                </div>
              )}
            </div>

            {/* Metadata */}
            <div className="grid gap-3 sm:grid-cols-[1.4fr_1fr]">
              <div>
                <Label htmlFor="vc-name" className="text-[12px]">
                  Voice name
                </Label>
                <Input
                  id="vc-name"
                  className="mt-1.5 bg-surface-2"
                  placeholder="e.g. Operator male"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={60}
                />
              </div>
              <div>
                <Label htmlFor="vc-lang" className="text-[12px]">
                  Language
                </Label>
                <Select value={language} onValueChange={setLanguage}>
                  <SelectTrigger id="vc-lang" className="mt-1.5">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="en">English (en)</SelectItem>
                    <SelectItem value="hi">Hindi (hi)</SelectItem>
                    <SelectItem value="es">Spanish (es)</SelectItem>
                    <SelectItem value="fr">French (fr)</SelectItem>
                    <SelectItem value="de">German (de)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div>
              <Label htmlFor="vc-tx" className="text-[12px]">
                Transcript (optional but improves prosody)
              </Label>
              <Textarea
                id="vc-tx"
                className="mt-1.5 min-h-[64px] bg-surface-2"
                placeholder="Type exactly what is said in the clip…"
                value={transcript}
                onChange={(e) => setTranscript(e.target.value)}
                maxLength={500}
              />
            </div>
          </div>
        )}

        {stage === "cloning" && (
          <div className="flex flex-col items-center justify-center gap-4 py-10">
            <Loader2 className="h-6 w-6 animate-spin text-foreground" />
            <div className="text-[13px] tracking-tight text-foreground">Cloning voice…</div>
            <div className="font-mono text-[11px] text-muted-foreground">
              ffmpeg → 24 kHz mono · loudness-normalising · saving
            </div>
          </div>
        )}

        {stage === "preview" && clonedVoice && (
          <div className="space-y-4">
            <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/5 p-4">
              <div className="flex items-center gap-3">
                <div className="grid h-10 w-10 place-items-center rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-300">
                  <Check className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] font-medium tracking-tight">
                    Cloned · {clonedVoice.label}
                  </div>
                  <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                    key · {clonedVoice.key} · {clonedVoice.duration_s?.toFixed(1) ?? "—"}s
                  </div>
                </div>
              </div>
              {clonedNote && (
                <p className="mt-3 text-[12px] text-amber-200">{clonedNote}</p>
              )}
            </div>

            <ServerPreview voice={clonedVoice} />
          </div>
        )}

        <DialogFooter>
          {stage === "preview" ? (
            <>
              <Button
                variant="ghost"
                size="sm"
                onClick={async () => {
                  if (!clonedVoice) return;
                  try {
                    await voicesApi.delete(clonedVoice.key);
                    toast.success("Clone discarded");
                    onOpenChange(false);
                  } catch {
                    toast.error("Couldn't discard clone");
                  }
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
                Discard
              </Button>
              <Button size="sm" onClick={useThisVoice}>
                <Check className="h-3.5 w-3.5" />
                Use this voice
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={submit}
                disabled={stage === "cloning" || (!file && !recordingBlob) || !name.trim()}
              >
                {stage === "cloning" ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Cloning
                  </>
                ) : (
                  <>
                    <Wand2 className="h-3.5 w-3.5" />
                    Clone &amp; preview
                  </>
                )}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ClipPreview({
  label,
  size,
  playing,
  onTogglePlay,
  onClear,
}: {
  label: string;
  size: number;
  playing: boolean;
  onTogglePlay: () => void;
  onClear: () => void;
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-surface px-3 py-2.5 text-left">
      <button
        type="button"
        onClick={onTogglePlay}
        className="grid h-9 w-9 shrink-0 place-items-center rounded-full border border-border-strong bg-background text-foreground hover:bg-foreground hover:text-background"
        aria-label={playing ? "Pause" : "Play"}
      >
        {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="ml-0.5 h-3.5 w-3.5" />}
      </button>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12.5px] font-medium tracking-tight text-foreground">
          {label}
        </div>
        <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">
          {(size / 1024).toFixed(1)} KB · ready to clone
        </div>
      </div>
      <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground hover:text-rose-300" onClick={onClear}>
        <Trash2 className="h-3 w-3" />
      </Button>
    </div>
  );
}

function ServerPreview({ voice }: { voice: VoiceInfo }) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);

  function toggle() {
    if (!voice.sample_url) return;
    if (!audioRef.current) {
      const el = new Audio(voice.sample_url);
      el.onended = () => setPlaying(false);
      el.onpause = () => setPlaying(false);
      audioRef.current = el;
    }
    if (playing) {
      audioRef.current.pause();
      setPlaying(false);
    } else {
      audioRef.current.currentTime = 0;
      audioRef.current
        .play()
        .then(() => setPlaying(true))
        .catch(() => {});
    }
  }

  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={toggle}
          className="grid h-10 w-10 shrink-0 place-items-center rounded-full border border-border-strong bg-background text-foreground hover:bg-foreground hover:text-background"
          aria-label={playing ? "Pause preview" : "Play preview"}
        >
          {playing ? <Pause className="h-4 w-4" /> : <Play className="ml-0.5 h-4 w-4" />}
        </button>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium tracking-tight">
            Preview · {voice.label}
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
            24 kHz mono · loudness-normalised reference
          </div>
        </div>
      </div>
    </div>
  );
}

function slugish(s: string): string {
  return s.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "voice";
}
