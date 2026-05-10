"use client";

import { useEffect, useState } from "react";
import {
  CheckCircle2,
  CircleDashed,
  FileWarning,
  GitBranch,
  Info,
  XCircle,
} from "lucide-react";

import { EmptyState } from "@/components/app/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import type { CloudServiceKind, DeployRow, DeploysResponse } from "@/lib/cloud-types";
import { cn, relativeTime } from "@/lib/utils";

const STATUS_TONES: Record<string, string> = {
  SUCCESS: "text-emerald-400 bg-emerald-500/10 border-emerald-500/30",
  FAILURE: "text-rose-400 bg-rose-500/10 border-rose-500/30",
  TIMEOUT: "text-rose-400 bg-rose-500/10 border-rose-500/30",
  CANCELLED: "text-amber-400 bg-amber-500/10 border-amber-500/30",
  WORKING: "text-sky-400 bg-sky-500/10 border-sky-500/30",
  QUEUED: "text-sky-400 bg-sky-500/10 border-sky-500/30",
  no_recent_build: "text-muted-foreground bg-surface-2 border-border",
  gcloud_unavailable: "text-muted-foreground bg-surface-2 border-border",
};

const KIND_ORDER: CloudServiceKind[] = ["tts", "image", "video", "infra"];
const KIND_LABEL: Record<CloudServiceKind, string> = {
  tts: "TTS",
  image: "Image",
  video: "Video",
  infra: "Infra",
};

export function CloudDeploysSection({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<DeploysResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openShort, setOpenShort] = useState<string | null>(null);

  async function load() {
    try {
      const res = await api.get<DeploysResponse>("/api/cloud/deploys");
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }

  useEffect(() => {
    load();
  }, [refreshKey]);

  return (
    <section className="flex flex-col gap-4">
      <header className="flex flex-col gap-1.5">
        <h2 className="text-[14px] font-medium tracking-tight">Deploys</h2>
        <p className="text-[12.5px] leading-relaxed text-muted-foreground">
          Last Cloud Build per service plus the 6-step{" "}
          <code className="font-mono text-[11px]">cloud_service_dep_playbook.md</code> prep status.
          Drives the gate that makes <span className="text-foreground">deploy-cloud-service</span>{" "}
          impossible to skip.
        </p>
      </header>

      {error ? (
        <EmptyState icon={FileWarning} title="Failed to load deploys" description={error} />
      ) : data === null ? (
        <Skeleton className="h-48 w-full" />
      ) : !data.available || !data.rows ? (
        <EmptyState
          icon={Info}
          title="No deploy snapshot yet"
          description={data?.reason ?? "Click Refresh."}
        />
      ) : (
        <div className="flex flex-col gap-6">
          {KIND_ORDER.map((k) => {
            const rows = data.rows!.filter((r) => r.kind === k);
            if (!rows.length) return null;
            return (
              <div key={k} className="overflow-hidden rounded-lg border border-border">
                <div className="flex items-center justify-between border-b border-border bg-surface/40 px-4 py-2.5">
                  <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                    {KIND_LABEL[k]} · {rows.length}
                  </span>
                </div>
                <table className="w-full text-left">
                  <thead className="bg-surface/20">
                    <tr className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                      <Th>Service</Th>
                      <Th>Last build</Th>
                      <Th>Status</Th>
                      <Th>Image digest</Th>
                      <Th className="text-right">Prep</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <DeployRowItem
                        key={r.short}
                        row={r}
                        open={openShort === r.short}
                        onToggle={() =>
                          setOpenShort(openShort === r.short ? null : r.short)
                        }
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function DeployRowItem({
  row,
  open,
  onToggle,
}: {
  row: DeployRow;
  open: boolean;
  onToggle: () => void;
}) {
  const status = row.last_build_status ?? "—";
  const tone = STATUS_TONES[status] ?? "text-muted-foreground bg-surface-2 border-border";
  const sha = row.image_digest ? row.image_digest.slice(0, 12) : "—";
  return (
    <>
      <tr
        className="cursor-pointer border-t border-border hover:bg-surface/40"
        onClick={onToggle}
      >
        <Td>
          <div className="font-mono text-[12px]">{row.short}</div>
          <div className="truncate font-mono text-[10.5px] text-muted-foreground">{row.name}</div>
        </Td>
        <Td className="font-mono text-[11.5px] text-muted-foreground">
          {row.last_build_at ? relativeTime(row.last_build_at) : "—"}
        </Td>
        <Td>
          <span
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em]",
              tone,
            )}
          >
            <GitBranch className="h-3 w-3" />
            {status}
          </span>
        </Td>
        <Td>
          <code className="font-mono text-[10.5px] text-muted-foreground">{sha}</code>
        </Td>
        <Td className="text-right">
          <span
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em]",
              row.prep_done === row.prep_total
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-400"
                : row.prep_done === 0
                  ? "border-border bg-surface-2 text-muted-foreground"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-400",
            )}
          >
            {row.prep_done === row.prep_total ? (
              <CheckCircle2 className="h-3 w-3" />
            ) : (
              <CircleDashed className="h-3 w-3" />
            )}
            {row.prep_done}/{row.prep_total}
          </span>
        </Td>
      </tr>
      {open && (
        <tr className="border-t border-border bg-surface/30">
          <td colSpan={5} className="px-4 py-4">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {row.prep_steps.map((step) => (
                <div
                  key={step.key}
                  className="flex items-start gap-2 rounded-md border border-border bg-surface px-3 py-2"
                >
                  {step.done ? (
                    <CheckCircle2 className="mt-[2px] h-3.5 w-3.5 flex-shrink-0 text-emerald-400" />
                  ) : (
                    <XCircle className="mt-[2px] h-3.5 w-3.5 flex-shrink-0 text-muted-foreground" />
                  )}
                  <div className="min-w-0">
                    <div className="font-mono text-[11px] text-foreground">{step.key}</div>
                    <div className="text-[12px] leading-snug text-muted-foreground">
                      {step.label}
                    </div>
                    {step.artifact && (
                      <div
                        className="mt-0.5 truncate font-mono text-[10.5px] text-muted-foreground/80"
                        title={step.artifact}
                      >
                        {step.artifact}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
            {row.log_url && (
              <a
                href={row.log_url}
                target="_blank"
                rel="noreferrer"
                className="mt-3 inline-block font-mono text-[11px] text-sky-400 hover:underline"
              >
                Cloud Build log →
              </a>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function Th({ children, className }: { children: React.ReactNode; className?: string }) {
  return <th className={cn("px-4 py-2.5 font-medium", className)}>{children}</th>;
}
function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("px-4 py-3 align-top", className)}>{children}</td>;
}
