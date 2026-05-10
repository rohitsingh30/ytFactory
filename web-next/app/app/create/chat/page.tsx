"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  ArrowRight,
  Loader2,
  MessageSquare,
  Send,
  Sparkles,
  Wand2,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { PageHeader } from "@/components/app/page-header";
import { ApiError, chatApi, type ShortProposal } from "@/lib/api";
import { channelLabel } from "@/components/app/channel-meta";
import { cn } from "@/lib/utils";

type Role = "user" | "assistant" | "system";

interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  proposal?: ShortProposal | null;
  createdAt: number;
}

const INTRO: ChatMessage = {
  id: "intro",
  role: "assistant",
  content:
    "Tell me what you want to make. I'll figure out the channel, niche, source, voice, and length, then queue the render.\n\nExamples:\n  • \"30s short on the LIGO discovery\"\n  • \"AITA story about ruining a cake — animated\"\n  • \"Iniesta's silencer at Stamford Bridge — sports short\"",
  createdAt: 0,
};

function newId(): string {
  return Math.random().toString(36).slice(2);
}

/**
 * AI-assisted Short authoring — the chat assistant figures out channel,
 * niche, source, voice, and length from natural language and emits a
 * ShortProposal. The user reviews, confirms, and the proposal becomes a
 * queued render job (same path as the form-driven create flow).
 *
 * Backed by control/chat_routes.py (POST /api/chat + POST /api/chat/confirm).
 */
export default function CreateChatPage() {
  const router = useRouter();
  const [sessionId, setSessionId] = useState<string>("");
  const [messages, setMessages] = useState<ChatMessage[]>([INTRO]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [configured, setConfigured] = useState(true);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const latestProposal = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const p = messages[i].proposal;
      if (p) return p;
    }
    return null;
  })();

  // Stick to bottom on new messages.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length]);

  // Autofocus on mount.
  useEffect(() => {
    taRef.current?.focus();
  }, []);

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;

    const userMsg: ChatMessage = {
      id: newId(),
      role: "user",
      content: text,
      createdAt: Date.now(),
    };
    setMessages((m) => [...m, userMsg]);
    setDraft("");
    setSending(true);

    try {
      const res = await chatApi.send(text, sessionId);
      if (res.session_id && res.session_id !== sessionId) {
        setSessionId(res.session_id);
      }
      setConfigured(res.configured);
      setMessages((m) => [
        ...m,
        {
          id: newId(),
          role: "assistant",
          content: res.response || "(empty response)",
          proposal: res.proposal ?? null,
          createdAt: Date.now(),
        },
      ]);
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setMessages((m) => [
        ...m,
        {
          id: newId(),
          role: "system",
          content: `Chat failed: ${detail}`,
          createdAt: Date.now(),
        },
      ]);
      toast.error("Chat failed", { description: detail });
    } finally {
      setSending(false);
      // Refocus the input so the user can keep typing.
      requestAnimationFrame(() => taRef.current?.focus());
    }
  }

  async function confirmRender() {
    if (!latestProposal || !sessionId || confirming) return;
    setConfirming(true);
    try {
      const r = await chatApi.confirm(sessionId);
      toast.success("Render queued", { description: `Job ${r.job_id.slice(0, 8)}…` });
      router.push(`/app/render/${r.job_id}`);
    } catch (e) {
      const detail =
        e instanceof ApiError
          ? typeof e.body === "object" && e.body && "detail" in (e.body as Record<string, unknown>)
            ? String((e.body as Record<string, unknown>).detail)
            : e.message
          : e instanceof Error
            ? e.message
            : String(e);
      toast.error("Couldn't queue render", { description: detail });
    } finally {
      setConfirming(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter inserts a newline (textarea default).
    if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="AI-generated"
        title="Describe your Short"
        description="Tell the assistant what you want and it figures out channel, niche, source, voice, and length. When you're happy with the proposal, hit Render."
        actions={
          <Button asChild variant="ghost" size="sm" className="text-muted-foreground hover:text-foreground">
            <Link href="/app/create">
              <ArrowLeft className="h-3.5 w-3.5" />
              Back
            </Link>
          </Button>
        }
      />

      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-4 px-6 py-6 md:px-8">
        {!configured && (
          <div className="rounded-lg border border-amber-300/40 bg-amber-100/5 px-4 py-3 text-[12px] text-amber-200">
            <div className="flex items-center gap-2">
              <Sparkles className="h-3.5 w-3.5" />
              <span className="font-medium">Chat AI is not configured.</span>
            </div>
            <p className="mt-1 text-muted-foreground">
              Set <code className="font-mono text-[11px]">AZURE_OPENAI_ENDPOINT</code> and{" "}
              <code className="font-mono text-[11px]">AZURE_OPENAI_API_KEY</code> on the control plane and restart it.
            </p>
          </div>
        )}

        <div
          ref={scrollRef}
          className="flex-1 min-h-[320px] overflow-y-auto rounded-xl border border-border bg-surface px-4 py-4"
        >
          <div className="space-y-3">
            {messages.map((m) => (
              <Bubble key={m.id} message={m} onConfirm={confirmRender} confirming={confirming} />
            ))}
            {sending && (
              <div className="flex items-center gap-2 px-1 text-[12px] text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" />
                Thinking…
              </div>
            )}
          </div>
        </div>

        <div className="rounded-xl border border-border bg-surface p-3">
          <div className="flex items-end gap-2">
            <Textarea
              ref={taRef}
              rows={2}
              placeholder="Type your idea — Enter to send, Shift+Enter for a newline."
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={sending}
              className="resize-none border-0 bg-transparent px-1 py-1.5 text-[13.5px] focus-visible:ring-0"
            />
            <Button onClick={send} disabled={sending || !draft.trim()} size="sm" className="shrink-0">
              {sending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <>
                  <Send className="h-3.5 w-3.5" />
                  Send
                </>
              )}
            </Button>
          </div>
          <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <MessageSquare className="h-3 w-3" />
              {sessionId ? `Session ${sessionId.slice(0, 8)}…` : "New session"}
            </span>
            {latestProposal && (
              <span className="text-foreground/80">
                Proposal ready · pick <span className="font-mono">Render</span> on the card above.
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ----------------------------- Bubbles ----------------------------- */

function Bubble({
  message,
  onConfirm,
  confirming,
}: {
  message: ChatMessage;
  onConfirm: () => void;
  confirming: boolean;
}) {
  if (message.role === "system") {
    return (
      <div className="rounded-md border border-rose-300/30 bg-rose-100/5 px-3 py-2 text-[12px] text-rose-200">
        {message.content}
      </div>
    );
  }
  const isUser = message.role === "user";
  return (
    <div className={cn("flex flex-col gap-1.5", isUser ? "items-end" : "items-start")}>
      <div
        className={cn(
          "max-w-[88%] whitespace-pre-wrap rounded-2xl px-3.5 py-2 text-[13px] leading-relaxed shadow-sm",
          isUser
            ? "rounded-br-sm bg-foreground text-background"
            : "rounded-bl-sm bg-background/90 text-foreground ring-1 ring-border",
        )}
      >
        {stripProposalJson(message.content)}
      </div>
      {message.proposal && (
        <ProposalCard proposal={message.proposal} onConfirm={onConfirm} confirming={confirming} />
      )}
    </div>
  );
}

/** Hide the raw {"type":"short_proposal", ...} JSON block from the
 *  assistant text — we render it as a card below the bubble. */
function stripProposalJson(text: string): string {
  const start = text.indexOf('{"type":"short_proposal"');
  const altStart = start === -1 ? text.indexOf('"type": "short_proposal"') : -1;
  const realStart = start !== -1 ? start : altStart !== -1 ? text.lastIndexOf("{", altStart) : -1;
  if (realStart === -1) return text.trim();
  // Walk balanced braces forward to find the JSON end.
  let depth = 0;
  let end = -1;
  for (let i = realStart; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}") {
      depth--;
      if (depth === 0) {
        end = i + 1;
        break;
      }
    }
  }
  if (end === -1) return text.trim();
  // Strip ``` fences around the block too if the model wrapped them.
  let before = text.slice(0, realStart);
  let after = text.slice(end);
  before = before.replace(/```(?:json)?\s*$/m, "").trimEnd();
  after = after.replace(/^\s*```/m, "").trimStart();
  return [before, after].filter(Boolean).join("\n\n").trim();
}

function ProposalCard({
  proposal,
  onConfirm,
  confirming,
}: {
  proposal: ShortProposal;
  onConfirm: () => void;
  confirming: boolean;
}) {
  return (
    <div className="max-w-[88%] rounded-xl border border-emerald-400/40 bg-emerald-400/[0.06] px-3.5 py-3 text-[12.5px] text-foreground">
      <div className="flex items-center justify-between gap-2">
        <div className="font-mono text-[10px] uppercase tracking-[0.2em] text-emerald-200">
          Proposal
        </div>
        <Button size="sm" onClick={onConfirm} disabled={confirming} className="px-3">
          {confirming ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Queueing
            </>
          ) : (
            <>
              <Wand2 className="h-3.5 w-3.5" />
              Render
              <ArrowRight className="h-3.5 w-3.5" />
            </>
          )}
        </Button>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5">
        <Field label="Channel" value={prettyChannel(proposal.channel)} />
        <Field label="Format" value={proposal.format} />
        <Field label="Length" value={`${proposal.length_s}s`} />
        <Field
          label="Source"
          value={
            proposal.source_kind === "auto"
              ? "auto"
              : proposal.source_ref
                ? `${proposal.source_kind} · ${truncate(proposal.source_ref, 32)}`
                : proposal.source_kind
          }
        />
      </div>
      <div className="mt-2.5">
        <div className="font-mono text-[9.5px] uppercase tracking-[0.18em] text-muted-foreground">
          Topic
        </div>
        <div className="mt-0.5 leading-snug">{proposal.topic}</div>
      </div>
      {proposal.notes && (
        <div className="mt-2">
          <div className="font-mono text-[9.5px] uppercase tracking-[0.18em] text-muted-foreground">
            Notes
          </div>
          <div className="mt-0.5 text-[12px] leading-snug text-muted-foreground">
            {proposal.notes}
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="font-mono text-[9.5px] uppercase tracking-[0.18em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-0.5 truncate text-[12px]">{value}</div>
    </div>
  );
}

function prettyChannel(key: string): string {
  if (!key || key === "auto") return "auto";
  return channelLabel(key);
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
