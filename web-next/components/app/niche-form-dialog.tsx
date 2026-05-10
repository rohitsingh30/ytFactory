"use client";

import { useEffect, useState } from "react";
import { Loader2, Sparkles, Save, X } from "lucide-react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { nichesApi } from "@/lib/api";
import type {
  NicheDoc,
  NicheFormat,
  NicheLengthKind,
  NicheSourceKind,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const FORMATS: NicheFormat[] = [
  "animated",
  "text",
  "cooking",
  "footage",
  "split_screen",
  "rhyme",
  "footage_only",
  "long_form",
  "sports_doc",
];

const SOURCE_KINDS: NicheSourceKind[] = [
  "reddit",
  "wikipedia",
  "manual",
  "x_twitter",
  "youtube",
  "rss",
];

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  channel: string;
  /** When provided, edit-mode for this existing niche. Otherwise create-mode. */
  existing?: NicheDoc | null;
  onSaved: (doc: NicheDoc) => void;
}

const EMPTY: NicheDoc = {
  key: "",
  label: "",
  description: "",
  prompt_style_guide: "",
  length_kind: "short",
  voice: "sarah",
  format: "animated",
  source_kind: "manual",
  source_ref: null,
  hook_template: "",
  closer_template: "",
  image_style: "",
  music_bed: null,
  created_at: "",
  created_by: "user",
};

export function NicheFormDialog({ open, onOpenChange, channel, existing, onSaved }: Props) {
  const editMode = !!existing;
  const [seedDescription, setSeedDescription] = useState("");
  const [generating, setGenerating] = useState(false);
  const [aiConfigured, setAiConfigured] = useState<boolean | null>(null);
  const [doc, setDoc] = useState<NicheDoc>(existing ?? EMPTY);
  const [showForm, setShowForm] = useState(editMode);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setDoc(existing ?? EMPTY);
      setSeedDescription("");
      setShowForm(editMode);
      setAiConfigured(null);
    }
  }, [open, existing, editMode]);

  function update<K extends keyof NicheDoc>(k: K, v: NicheDoc[K]) {
    setDoc((d) => ({ ...d, [k]: v }));
  }

  async function generate() {
    if (!seedDescription.trim()) return;
    setGenerating(true);
    try {
      const r = await nichesApi.draft(channel, seedDescription.trim());
      setDoc(r.draft);
      setAiConfigured(r.ai_configured);
      setShowForm(true);
    } catch (e) {
      toast.error("Generate failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setGenerating(false);
    }
  }

  async function save() {
    if (!doc.key.trim() || !doc.label.trim()) {
      toast.error("Key and label are required");
      return;
    }
    setSaving(true);
    try {
      const saved = editMode
        ? await nichesApi.update(channel, doc.key, doc)
        : await nichesApi.create(channel, doc);
      toast.success(editMode ? "Niche updated" : "Niche created");
      onSaved(saved);
      onOpenChange(false);
    } catch (e) {
      toast.error("Save failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[88vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{editMode ? `Edit niche · ${doc.label}` : "Add a new niche"}</DialogTitle>
          <DialogDescription>
            {editMode
              ? "Tweak any field. Saved as JSON in the channel's niches folder."
              : "Describe the niche in one line — AI will pre-fill the structured spec for you to review."}
          </DialogDescription>
        </DialogHeader>

        {!editMode && !showForm && (
          <div className="space-y-3">
            <Label htmlFor="niche-seed" className="text-[12px] font-medium">
              Describe the niche
            </Label>
            <Textarea
              id="niche-seed"
              rows={3}
              placeholder="e.g. Animated retelling of horror reddit stories with creepy whispered narration over candle-lit visuals."
              value={seedDescription}
              onChange={(e) => setSeedDescription(e.target.value)}
              autoFocus
            />
            <div className="flex items-center justify-between">
              <p className="text-[11px] text-muted-foreground">
                One-sentence intent is enough — pre-fill is a starting point you can edit before saving.
              </p>
              <Button onClick={generate} disabled={generating || !seedDescription.trim()}>
                {generating ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" /> Generating
                  </>
                ) : (
                  <>
                    <Sparkles className="h-3.5 w-3.5" /> Generate
                  </>
                )}
              </Button>
            </div>
          </div>
        )}

        {showForm && (
          <div className="space-y-5">
            {!editMode && aiConfigured === false && (
              <div className="rounded-md border border-amber-400/30 bg-amber-400/[0.05] px-3 py-2 text-[11.5px] text-amber-300">
                Azure OpenAI not configured — used a deterministic stub. Edit fields below to customize before saving.
              </div>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Key" hint="snake_case slug" required>
                <Input
                  value={doc.key}
                  disabled={editMode}
                  onChange={(e) => update("key", e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
                />
              </Field>
              <Field label="Label" hint="Human title shown in lists" required>
                <Input value={doc.label} onChange={(e) => update("label", e.target.value)} />
              </Field>
            </div>

            <Field label="Description" hint="One-sentence blurb">
              <Textarea
                rows={2}
                value={doc.description}
                onChange={(e) => update("description", e.target.value)}
              />
            </Field>

            <Field label="Prompt style guide" hint="Voice/tone passed to the LLM script writer">
              <Textarea
                rows={3}
                value={doc.prompt_style_guide}
                onChange={(e) => update("prompt_style_guide", e.target.value)}
              />
            </Field>

            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Length" hint="short = ≤90s · long = multi-min">
                <Select value={doc.length_kind} onValueChange={(v) => update("length_kind", v as NicheLengthKind)}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="short">Short form</SelectItem>
                    <SelectItem value="long">Long form</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Voice">
                <Input value={doc.voice} onChange={(e) => update("voice", e.target.value)} />
              </Field>
              <Field label="Format">
                <Select value={doc.format} onValueChange={(v) => update("format", v as NicheFormat)}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {FORMATS.map((f) => (
                      <SelectItem key={f} value={f}>{f}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Source kind">
                <Select
                  value={doc.source_kind}
                  onValueChange={(v) => update("source_kind", v as NicheSourceKind)}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {SOURCE_KINDS.map((s) => (
                      <SelectItem key={s} value={s}>{s}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Source ref" hint="e.g. r/AmItheAsshole">
                <Input
                  value={doc.source_ref ?? ""}
                  onChange={(e) => update("source_ref", e.target.value || null)}
                />
              </Field>
            </div>

            <Field label="Hook template" hint="e.g. AITA for {action}?">
              <Input value={doc.hook_template} onChange={(e) => update("hook_template", e.target.value)} />
            </Field>

            <Field label="Closer template" hint="CTA / sign-off line">
              <Input value={doc.closer_template} onChange={(e) => update("closer_template", e.target.value)} />
            </Field>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Image style" hint="e.g. 2D crayon, warm palette">
                <Input value={doc.image_style} onChange={(e) => update("image_style", e.target.value)} />
              </Field>
              <Field label="Music bed" hint="optional named bed">
                <Input
                  value={doc.music_bed ?? ""}
                  onChange={(e) => update("music_bed", e.target.value || null)}
                />
              </Field>
            </div>

            <div className="flex items-center justify-end gap-2 pt-2 border-t border-border">
              <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={saving}>
                <X className="h-3.5 w-3.5" /> Cancel
              </Button>
              <Button onClick={save} disabled={saving}>
                {saving ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" /> Saving
                  </>
                ) : (
                  <>
                    <Save className="h-3.5 w-3.5" /> {editMode ? "Save changes" : "Create niche"}
                  </>
                )}
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  hint,
  required,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between">
        <Label className={cn("text-[11.5px] font-medium")}>
          {label}
          {required && <span className="ml-1 text-rose-400">*</span>}
        </Label>
        {hint && <span className="text-[10.5px] text-muted-foreground">{hint}</span>}
      </div>
      {children}
    </div>
  );
}
