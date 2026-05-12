"use client";

/**
 * CritiqueChatPanel — real-time chat UI on the render-detail page.
 *
 * Lives at the bottom of the render-detail page. Lets the user type
 * a critique like "the closer sounds robotic, fix the TTS prosody",
 * subscribes to the per-critique Firestore messages subcollection
 * via onSnapshot for sub-500ms updates, and renders the chat plus
 * agent-action chips streamed from the laptop runner.
 *
 * Architecture: see [`docs/critique_chat.md`](../../../docs/critique_chat.md).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot,
  CheckCircle2,
  Hammer,
  Loader2,
  MessageSquare,
  Send,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

// ---- Types matching pipeline/critique/messages.py exactly ----

type ChatRole = "user" | "agent" | "system";

type ChatAction =
  | "file_read"
  | "file_edited"
  | "test_added"
  | "gate_running"
  | "gate_passed"
  | "gate_failed"
  | "commit_created"
  | "push_pending"
  | "pushed"
  | "agent_thinking"
  | "agent_finished";

interface ChatMessage {
  message_id: string;
  role: ChatRole;
  text: string;
  ts?: { toMillis: () => number } | null;
  action?: ChatAction | null;
  action_data?: Record<string, unknown> | null;
}

type CritiqueStatus =
  | "queued"
  | "in_progress"
  | "done"
  | "failed"
  | "abandoned"
  | null;

interface CritiqueDoc {
  status: CritiqueStatus;
  agent: string;
  commit_sha?: string | null;
  summary?: string | null;
  error?: string | null;
}

interface Props {
  jobId: string;
  /** Render-detail page already knows the channel; lets us label the chat. */
  channel?: string | null;
}

export function CritiqueChatPanel({ jobId }: Props) {
  const [agent, setAgent] = useState<"claude" | "copilot">("claude");
  const [critiqueId, setCritiqueId] = useState<string | null>(null);
  const [doc, setDoc] = useState<CritiqueDoc | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<"idle" | "starting" | "active" | "error">("idle");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const unsubsRef = useRef<Array<() => void>>([]);

  // Cleanup any open subscriptions on unmount.
  useEffect(() => {
    return () => {
      unsubsRef.current.forEach((unsub) => unsub());
      unsubsRef.current = [];
    };
  }, []);

  // Auto-scroll the message list to the bottom on new messages.
  const messageListRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = messageListRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const startSession = useCallback(async () => {
    setStatus("starting");
    setErrorText(null);
    // Audit Q2.49 — pre-fix unsubsRef.current.push(...) added new
    // subscriptions WITHOUT first tearing down any existing ones.
    // Hitting the Retry button (or restart-on-different-agent) ran
    // startSession a second time → two parallel onSnapshot callbacks
    // for the same doc, double-firing setDoc/setMessages, plus a
    // permanent leak (the first pair was no longer reachable). Tear
    // down before re-subscribing.
    unsubsRef.current.forEach((unsub) => unsub());
    unsubsRef.current = [];
    try {
      // 1. Create or fetch the parent critique doc.
      const startRes = await fetch(`/api/jobs/${jobId}/critique/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent }),
      });
      if (!startRes.ok) {
        const body = await startRes.json().catch(() => ({}));
        throw new Error(body.detail ?? `start failed (${startRes.status})`);
      }
      const startBody = await startRes.json();
      const cid = startBody.critique_id as string;

      // 2. Mint Firebase custom token for the signed-in user so we
      //    can read+write the messages subcollection directly.
      const tokenRes = await fetch(`/api/jobs/${jobId}/critique/token`, { method: "POST" });
      if (!tokenRes.ok) {
        const body = await tokenRes.json().catch(() => ({}));
        throw new Error(body.detail ?? `token failed (${tokenRes.status})`);
      }
      const tokenBody = await tokenRes.json();

      // 3. Lazy-load firebase + sign in. Doing this AFTER the
      //    fetches keeps the bundle out of the critical path on
      //    pages where chat is never opened.
      const { signInWithCritiqueToken, getCritiqueFirestore } = await import("@/lib/firebase");
      await signInWithCritiqueToken(tokenBody.token);

      const db = getCritiqueFirestore();
      const { collection, doc, onSnapshot, orderBy, query } = await import("firebase/firestore");

      // 4. Subscribe to the parent doc — status updates flow through here.
      const parentRef = doc(db, "critiques", cid);
      const unsubParent = onSnapshot(parentRef, (snap) => {
        if (!snap.exists()) {
          setDoc(null);
          return;
        }
        const d = snap.data() as Record<string, unknown>;
        setDoc({
          status: (d.status as CritiqueStatus) ?? null,
          agent: String(d.agent ?? agent),
          commit_sha: (d.commit_sha as string | null) ?? null,
          summary: (d.summary as string | null) ?? null,
          error: (d.error as string | null) ?? null,
        });
      });

      // 5. Subscribe to messages subcollection, ordered by server timestamp.
      const messagesRef = query(collection(parentRef, "messages"), orderBy("ts"));
      const unsubMessages = onSnapshot(messagesRef, (snap) => {
        const next: ChatMessage[] = [];
        snap.forEach((m) => {
          const d = m.data() as Record<string, unknown>;
          next.push({
            message_id: m.id,
            role: String(d.role) as ChatRole,
            text: String(d.text ?? ""),
            ts: (d.ts as { toMillis: () => number } | undefined) ?? null,
            action: (d.action as ChatAction | null) ?? null,
            action_data: (d.action_data as Record<string, unknown> | null) ?? null,
          });
        });
        setMessages(next);
      });

      unsubsRef.current.push(unsubParent, unsubMessages);
      setCritiqueId(cid);
      setStatus("active");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorText(msg);
      setStatus("error");
      toast.error("Couldn't start critique chat", { description: msg });
    }
  }, [jobId, agent]);

  const sendMessage = useCallback(async () => {
    if (!critiqueId || !draft.trim()) return;
    setSending(true);
    try {
      const { getCritiqueFirestore } = await import("@/lib/firebase");
      const db = getCritiqueFirestore();
      const { collection, doc, addDoc, serverTimestamp } = await import("firebase/firestore");
      const parentRef = doc(db, "critiques", critiqueId);
      await addDoc(collection(parentRef, "messages"), {
        role: "user",
        text: draft.trim(),
        ts: serverTimestamp(),
      });
      setDraft("");
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error("Couldn't send message", { description: msg });
    } finally {
      setSending(false);
    }
  }, [critiqueId, draft]);

  // Pre-session UI: idle / error states.
  if (status === "idle") {
    return (
      <Section>
        <SectionHeader />
        <div className="flex flex-col gap-3 px-5 py-4">
          <p className="text-[12.5px] leading-relaxed text-muted-foreground">
            Tell the agent what's wrong with the rendered video. It'll find a
            class-of-bug fix in the pipeline, add tests, run hard gates, and
            push direct to <span className="font-mono text-[11px]">main</span>{" "}
            when green.
          </p>
          <div className="flex items-center gap-2">
            <Select value={agent} onValueChange={(v) => setAgent(v as "claude" | "copilot")}>
              <SelectTrigger className="h-8 w-[140px] text-[12px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="claude">Claude</SelectItem>
                <SelectItem value="copilot">Copilot</SelectItem>
              </SelectContent>
            </Select>
            <Button onClick={startSession} size="sm">
              <MessageSquare className="h-3.5 w-3.5" />
              Start critique
            </Button>
          </div>
        </div>
      </Section>
    );
  }

  if (status === "starting") {
    return (
      <Section>
        <SectionHeader />
        <div className="flex items-center gap-2 px-5 py-4">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-violet-300" />
          <span className="text-[12.5px] text-muted-foreground">
            Connecting to runner…
          </span>
        </div>
      </Section>
    );
  }

  if (status === "error") {
    return (
      <Section tone="error">
        <SectionHeader />
        <div className="flex flex-col gap-2 px-5 py-4">
          <div className="flex items-center gap-2">
            <XCircle className="h-3.5 w-3.5 text-rose-300" />
            <span className="text-[12.5px] text-rose-200">
              {errorText ?? "Something went wrong"}
            </span>
          </div>
          <Button onClick={startSession} size="sm" variant="outline">
            Retry
          </Button>
        </div>
      </Section>
    );
  }

  // Active session UI.
  return (
    <Section className="flex flex-col lg:h-[calc(100vh-3rem)]">
      <SectionHeader doc={doc} />
      <div
        ref={messageListRef}
        className="flex flex-1 min-h-[280px] flex-col gap-3 overflow-y-auto px-5 py-4"
      >
        {messages.length === 0 && (
          <div className="text-center text-[12px] text-muted-foreground">
            Waiting for your first message…
          </div>
        )}
        {messages.map((m) => (
          <MessageRow key={m.message_id} message={m} />
        ))}
        {messages.length > 0 &&
          doc?.status === "queued" && (
            <RunnerHint />
          )}
      </div>
      <div className="border-t border-border px-5 py-3">
        <div className="flex items-end gap-2">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Describe the bug, ask a follow-up, or paste a stack trace…"
            className="min-h-[64px] flex-1 text-[13px]"
            disabled={sending || doc?.status === "done" || doc?.status === "failed"}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void sendMessage();
              }
            }}
          />
          <Button
            onClick={() => void sendMessage()}
            disabled={!draft.trim() || sending || doc?.status === "done" || doc?.status === "failed"}
            size="sm"
          >
            {sending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Send className="h-3.5 w-3.5" />
            )}
            Send
          </Button>
        </div>
        <div className="mt-1 text-[10px] font-mono text-muted-foreground">
          ⌘/Ctrl+Enter to send
        </div>
      </div>
    </Section>
  );
}

function Section({
  children,
  tone = "default",
  className,
}: {
  children: React.ReactNode;
  tone?: "default" | "error";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border bg-surface",
        tone === "error" ? "border-rose-500/30 bg-rose-500/5" : "border-border",
        className,
      )}
    >
      {children}
    </div>
  );
}

function RunnerHint() {
  // Surfaces the most common "stuck" cause: the user sent a message
  // but the laptop runner isn't subscribed (not started, ADC expired,
  // network drop, …). Without this the chat just sits on `queued`
  // forever with no indication of WHY.
  return (
    <div className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-[11.5px] leading-relaxed text-amber-200">
      <div className="font-mono text-[10px] uppercase tracking-[0.14em]">
        runner not picking up
      </div>
      <p className="mt-1">
        Your message landed in Firestore but the laptop hasn't claimed it yet.
        On your laptop, run:
      </p>
      <pre className="mt-1 rounded bg-black/30 px-2 py-1 font-mono text-[10.5px] text-amber-100/90">
        cd /Users/rohit/ytFactory && make critique-runner
      </pre>
      <p className="mt-1 text-[11px] text-amber-200/70">
        First-time setup may also need{" "}
        <code className="rounded bg-black/30 px-1 font-mono text-[10.5px]">
          gcloud auth application-default login
        </code>
        .
      </p>
    </div>
  );
}

function SectionHeader({ doc }: { doc?: CritiqueDoc | null }) {
  return (
    <div className="flex items-center justify-between border-b border-border px-5 py-3">
      <div className="flex items-center gap-2">
        <Bot className="h-3.5 w-3.5 text-violet-300" />
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          Pipeline-fix chat
        </div>
      </div>
      {doc && <CritiqueStatusPill doc={doc} />}
    </div>
  );
}

function CritiqueStatusPill({ doc }: { doc: CritiqueDoc }) {
  const map: Record<string, { tone: string; label: string; icon?: React.ReactNode }> = {
    queued: { tone: "border-blue-500/30 text-blue-200", label: "queued" },
    in_progress: {
      tone: "border-violet-500/30 text-violet-200",
      label: "agent working…",
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
    },
    done: {
      tone: "border-emerald-500/30 text-emerald-200",
      label: doc.commit_sha ? `pushed ${doc.commit_sha.slice(0, 8)}` : "done",
      icon: <CheckCircle2 className="h-3 w-3" />,
    },
    failed: {
      tone: "border-rose-500/30 text-rose-200",
      label: "failed",
      icon: <XCircle className="h-3 w-3" />,
    },
  };
  const s = doc.status ?? "queued";
  const e = map[s] ?? map.queued!;
  return (
    <span
      className={cn(
        "flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em]",
        e.tone,
      )}
    >
      {e.icon}
      {e.label}
    </span>
  );
}

function MessageRow({ message }: { message: ChatMessage }) {
  if (message.action && !message.text) {
    return <ActionChip action={message.action} text={message.text} data={message.action_data ?? null} />;
  }
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[78%] rounded-lg bg-violet-500/10 border border-violet-500/20 px-3 py-2 text-[12.5px] leading-relaxed text-foreground">
          {message.text}
        </div>
      </div>
    );
  }
  if (message.role === "agent") {
    return (
      <div className="flex flex-col gap-1">
        {message.action && (
          <div className="ml-1">
            <ActionChip action={message.action} text="" data={message.action_data ?? null} />
          </div>
        )}
        {message.text && (
          <div className="max-w-[88%] rounded-lg bg-surface-2 border border-border px-3 py-2 text-[12.5px] leading-relaxed text-foreground whitespace-pre-wrap">
            {message.text}
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="text-center text-[11px] font-mono text-muted-foreground">
      {message.text}
    </div>
  );
}

function ActionChip({
  action,
  text,
  data,
}: {
  action: ChatAction;
  text: string;
  data: Record<string, unknown> | null;
}) {
  const map: Record<ChatAction, { tone: string; label: string; icon: React.ReactNode }> = {
    file_read: { tone: "border-slate-500/30 text-slate-200", label: "read", icon: <span>·</span> },
    file_edited: { tone: "border-amber-500/30 text-amber-200", label: "edit", icon: <Hammer className="h-3 w-3" /> },
    test_added: { tone: "border-amber-500/30 text-amber-200", label: "test", icon: <Hammer className="h-3 w-3" /> },
    gate_running: { tone: "border-violet-500/30 text-violet-200", label: "gate", icon: <Loader2 className="h-3 w-3 animate-spin" /> },
    gate_passed: { tone: "border-emerald-500/30 text-emerald-200", label: "gate ok", icon: <ShieldCheck className="h-3 w-3" /> },
    gate_failed: { tone: "border-rose-500/30 text-rose-200", label: "gate fail", icon: <XCircle className="h-3 w-3" /> },
    commit_created: { tone: "border-emerald-500/30 text-emerald-200", label: "commit", icon: <CheckCircle2 className="h-3 w-3" /> },
    push_pending: { tone: "border-violet-500/30 text-violet-200", label: "pushing", icon: <Loader2 className="h-3 w-3 animate-spin" /> },
    pushed: { tone: "border-emerald-500/30 text-emerald-200", label: "pushed", icon: <CheckCircle2 className="h-3 w-3" /> },
    agent_thinking: { tone: "border-violet-500/30 text-violet-200", label: "thinking", icon: <Loader2 className="h-3 w-3 animate-spin" /> },
    agent_finished: { tone: "border-slate-500/30 text-slate-200", label: "done", icon: <CheckCircle2 className="h-3 w-3" /> },
  };
  const e = map[action];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em]",
        e.tone,
      )}
    >
      {e.icon}
      <span>{e.label}</span>
      {text && <span className="ml-1 normal-case tracking-normal text-muted-foreground">{text}</span>}
      {!text && data && typeof data.detail === "string" && (
        <span className="ml-1 normal-case tracking-normal text-muted-foreground">
          {String(data.detail).slice(0, 60)}
        </span>
      )}
    </span>
  );
}
