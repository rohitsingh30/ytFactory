"use client";

import { useEffect, useState } from "react";
import { Cloud, Cpu, Eye, KeyRound, Laptop, Pause, Play, Server, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/app/page-header";
import { healthApi } from "@/lib/api";

interface HealthData {
  ok: boolean;
  azure_spend_usd_today: number;
  azure_spend_cap_usd: number;
  render_backend?: string;
  cloudrun?: {
    backend?: string;
    project?: string;
    region?: string;
    job_name?: string;
    sdk_available?: boolean;
    cli_available?: boolean;
  };
  sim_worker?: {
    enabled: boolean;
    running: boolean;
    speed_multiplier: number;
    placeholder_exists: boolean;
  };
}

const BACKEND_LABEL: Record<string, { label: string; icon: typeof Cloud; tone: string }> = {
  sim: { label: "Sim · in-process", icon: Server, tone: "text-foreground" },
  cloudrun: { label: "Cloud Run Job", icon: Cloud, tone: "text-emerald-300" },
  laptop: { label: "Laptop agent · DEPRECATED", icon: Laptop, tone: "text-amber-300" },
};

export default function SettingsPage() {
  const [health, setHealth] = useState<HealthData | null>(null);
  const [apiBase, setApiBase] = useState<string>("");
  useEffect(() => {
    (healthApi.get() as unknown as Promise<HealthData>).then(setHealth).catch(() => {});
    // Audit Q2.53 — pre-fix the "API base" row hard-coded
    // http://127.0.0.1:8766 always. On Cloud Run prod that's a lie
    // — the actual base lives behind window.location's origin (the
    // /api/* proxy in app/api/[...path]/route.ts forwards to
    // YTFACTORY_API_BASE on the server side; the browser only ever
    // talks to this origin). Read window.location at runtime so the
    // displayed value matches reality on every environment.
    if (typeof window !== "undefined") {
      setApiBase(window.location.origin);
    }
  }, []);

  const backend = (health?.render_backend ?? "sim") as string;
  const meta = BACKEND_LABEL[backend] ?? BACKEND_LABEL.sim;

  return (
    <div className="flex min-h-full flex-col">
      <PageHeader
        eyebrow="Studio · Settings"
        title="Settings"
        description="Operator profile, system status, and the knobs that aren't per-channel."
      />
      <div className="grid gap-5 px-6 py-6 md:px-8 lg:grid-cols-2">
        <Card title="Operator">
          <Row label="Name" value="Operator" />
          <Row label="Mode" value="Single-tenant" />
          <Row label="API base" value={apiBase || "(loading)"} mono />
        </Card>

        <Card title="System">
          {health ? (
            <>
              <Row
                icon={meta.icon}
                label="Render backend"
                value={meta.label}
                valueClassName={meta.tone}
              />
              <Row
                icon={health.sim_worker?.running ? Play : Pause}
                label="Sim worker"
                value={
                  health.sim_worker?.running
                    ? "running"
                    : health.sim_worker?.enabled
                      ? "idle"
                      : "off"
                }
              />
              <Row
                icon={Cpu}
                label="Sim speed"
                value={
                  health.sim_worker
                    ? `${health.sim_worker.speed_multiplier.toFixed(2)}×`
                    : "—"
                }
              />
              <Row
                icon={Eye}
                label="Azure spend (today)"
                value={`$${health.azure_spend_usd_today.toFixed(2)} / $${health.azure_spend_cap_usd.toFixed(0)}`}
              />
              <Row icon={Sparkles} label="Health" value={health.ok ? "OK" : "—"} />
            </>
          ) : (
            <Skeleton className="h-32" />
          )}
        </Card>

        {backend === "cloudrun" && health?.cloudrun && (
          <Card title="Cloud Run Job" className="lg:col-span-2">
            <Row label="Project" value={health.cloudrun.project ?? "—"} mono />
            <Row label="Region" value={health.cloudrun.region ?? "—"} mono />
            <Row label="Job name" value={health.cloudrun.job_name ?? "—"} mono />
            <Row
              label="Trigger path"
              value={
                health.cloudrun.sdk_available
                  ? "google-cloud-run SDK"
                  : health.cloudrun.cli_available
                    ? "gcloud CLI fallback"
                    : "(no transport available)"
              }
              valueClassName={
                health.cloudrun.sdk_available || health.cloudrun.cli_available
                  ? "text-emerald-300"
                  : "text-destructive"
              }
            />
            <p className="mt-2 text-[12px] leading-relaxed text-muted-foreground">
              Renders are dispatched to the Cloud Run Job{" "}
              <span className="font-mono text-foreground">
                {health.cloudrun.job_name}
              </span>
              . Deploy with{" "}
              <span className="font-mono text-foreground">
                ./cloud/render-worker-v2/deploy.sh
              </span>
              . Full runbook in{" "}
              <span className="font-mono text-foreground">
                docs/cloudrun_render_worker.md
              </span>
              .
            </p>
          </Card>
        )}

        <Card title="API tokens" className="lg:col-span-2">
          <Row icon={KeyRound} label="Agent token" value="env: YTFACTORY_AGENT_TOKEN" mono />
          <Row label="YouTube API key" value="env: YOUTUBE_API_KEY" mono />
          <Row label="Azure endpoint" value="env: AZURE_OPENAI_ENDPOINT" mono />
          <p className="mt-3 text-[12px] text-muted-foreground">
            Tokens are read from the FastAPI control plane environment. Edit them
            in your shell profile or systemd unit; the studio never stores them.
          </p>
        </Card>

        <Card title="Render backend modes" className="lg:col-span-2">
          <ul className="space-y-3 text-[12.5px] leading-relaxed text-muted-foreground">
            <li>
              <span className="font-mono text-foreground">YTFACTORY_RENDER_BACKEND=sim</span>{" "}
              <span className="text-muted-foreground/80">(default)</span> — the in-process
              simulated worker walks the seven stages in seconds and drops a
              placeholder mp4. Perfect for UX iteration. No GCP needed.
            </li>
            <li>
              <span className="font-mono text-foreground">YTFACTORY_RENDER_BACKEND=cloudrun</span>{" "}
              <span className="text-emerald-300/80">(production)</span> — the control plane
              triggers a Cloud Run Job execution per render. Removes the laptop from
              the loop entirely. Set{" "}
              <span className="font-mono text-foreground">YTFACTORY_CLOUDRUN_JOB</span> +{" "}
              <span className="font-mono text-foreground">GOOGLE_CLOUD_PROJECT</span>.
            </li>
            <li>
              <span className="font-mono text-foreground">YTFACTORY_RENDER_BACKEND=laptop</span>{" "}
              <span className="text-amber-300/80">(deprecated)</span> — falls back to the
              old queue + agent path. Kept for one release while the cloud worker
              proves out.
            </li>
          </ul>
          <div className="mt-4 flex items-center gap-2">
            <Button variant="outline" size="sm" disabled>
              Toggle (set in env)
            </Button>
            <span className="font-mono text-[11px] text-muted-foreground">
              currently · {backend}
            </span>
          </div>
        </Card>
      </div>
    </div>
  );
}

function Card({
  title,
  className,
  children,
}: {
  title: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section className={`rounded-xl border border-border bg-surface ${className ?? ""}`}>
      <div className="border-b border-border px-5 py-3.5">
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
          {title}
        </div>
      </div>
      <div className="space-y-2.5 p-5">{children}</div>
    </section>
  );
}

function Row({
  label,
  value,
  mono,
  icon: Icon,
  valueClassName,
}: {
  label: string;
  value: string;
  mono?: boolean;
  icon?: typeof Play;
  valueClassName?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface-2/40 px-3 py-2">
      <div className="flex items-center gap-2">
        {Icon && <Icon className="h-3.5 w-3.5 text-muted-foreground" />}
        <span className="font-mono text-[10.5px] uppercase tracking-[0.15em] text-muted-foreground">
          {label}
        </span>
      </div>
      <span
        className={
          valueClassName
            ? `text-[12.5px] ${valueClassName}`
            : mono
              ? "truncate font-mono text-[11.5px] text-foreground/90"
              : "text-[12.5px] text-foreground/90"
        }
      >
        {value}
      </span>
    </div>
  );
}
