"use client";

import Link from "next/link";
import { ArrowLeft, MessageSquare, Sparkles, Wand2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/app/page-header";

/**
 * AI-assisted Short authoring — "coming soon" placeholder.
 *
 * The backend (control/chat_routes.py + control/chat_service.py) is live,
 * but the chat-driven create flow is being held back until the proposal
 * → render hand-off has been hardened. Wizard-driven creation works
 * today via /app/create.
 */
export default function CreateChatPage() {
  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="AI-generated"
        title="Describe your Short"
        description="Soon you'll be able to type what you want and the assistant will pick channel, niche, source, voice, and length for you."
        actions={
          <Button
            asChild
            variant="ghost"
            size="sm"
            className="text-muted-foreground hover:text-foreground"
          >
            <Link href="/app/create">
              <ArrowLeft className="h-3.5 w-3.5" />
              Back
            </Link>
          </Button>
        }
      />

      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col items-center justify-center px-6 py-12 md:px-8">
        <div className="w-full rounded-2xl border border-border bg-surface px-6 py-10 text-center shadow-sm">
          <div className="mx-auto inline-flex h-12 w-12 items-center justify-center rounded-xl bg-foreground/[0.04] ring-1 ring-border">
            <MessageSquare className="h-5 w-5 text-foreground/70" />
          </div>

          <div className="mt-4 inline-flex items-center gap-1.5 rounded-full bg-amber-300/15 px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.2em] text-amber-200 ring-1 ring-amber-300/30">
            <Sparkles className="h-2.5 w-2.5" />
            Coming soon
          </div>

          <h2 className="mt-3 text-[18px] font-medium tracking-tight text-foreground">
            AI chat is on its way.
          </h2>
          <p className="mx-auto mt-2 max-w-md text-[13px] leading-relaxed text-muted-foreground">
            We're polishing the conversation → render hand-off so the assistant always picks
            sane defaults (channel, voice, source, length) and never blocks on a missing field.
          </p>

          <div className="mt-7 flex flex-col items-center justify-center gap-2 sm:flex-row">
            <Button asChild className="px-4">
              <Link href="/app/create">
                <Wand2 className="h-3.5 w-3.5" />
                Use the wizard for now
              </Link>
            </Button>
            <Button asChild variant="ghost" size="sm">
              <Link href="/app/create/clone">Or clone a video</Link>
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
