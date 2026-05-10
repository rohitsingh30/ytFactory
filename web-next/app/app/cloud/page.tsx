"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw, Zap } from "lucide-react";
import { toast } from "sonner";

import { PageHeader } from "@/components/app/page-header";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api";

import { CloudCostSection } from "./cost-section";
import { CloudDeploysSection } from "./deploys-section";
import { CloudHealthSection } from "./health-section";

export default function CloudPage() {
  const [refreshing, setRefreshing] = useState(false);
  const [warming, setWarming] = useState(false);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await api.post<{ ok: boolean; paths: Record<string, string> }>(
        "/api/cloud/refresh?cost_days=30",
      );
      toast.success("Snapshot refreshed");
      setTick((t) => t + 1);
    } catch (e) {
      const msg =
        e instanceof ApiError ? `${e.status}: ${(e.body as any)?.detail ?? e.message}` : String(e);
      toast.error("Refresh failed", { description: msg });
    } finally {
      setRefreshing(false);
    }
  }, []);

  const warm = useCallback(async () => {
    setWarming(true);
    try {
      const res = await api.post<{
        fired: boolean;
        results: { target: string; ok: boolean }[];
        note?: string;
      }>("/api/cloud/warm");
      if (!res.fired) {
        toast.message("Warm skipped", { description: res.note ?? "no cloud configured" });
      } else {
        const ok = res.results.filter((r) => r.ok).length;
        toast.success(`Warmed ${ok}/${res.results.length} services`);
      }
      setTick((t) => t + 1);
    } catch (e) {
      const msg =
        e instanceof ApiError ? `${e.status}: ${(e.body as any)?.detail ?? e.message}` : String(e);
      toast.error("Warm failed", { description: msg });
    } finally {
      setWarming(false);
    }
  }, []);

  useEffect(() => {
    setTick((t) => t + 1);
  }, []);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="Infrastructure"
        title="Cloud"
        description="Live health, daily cost, and deploy status across every Cloud Run service we own."
        actions={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={warm}
              disabled={warming}
              className="h-8 gap-1.5"
              title="Pre-warm GPU containers using the default chatterbox + flux pair"
            >
              {warming ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Zap className="h-3.5 w-3.5" />
              )}
              Warm
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={refresh}
              disabled={refreshing}
              className="h-8 gap-1.5"
            >
              {refreshing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )}
              Refresh
            </Button>
          </div>
        }
      />

      <div className="flex flex-1 flex-col gap-12 px-6 py-8 md:px-8">
        <CloudHealthSection refreshKey={tick} />
        <CloudCostSection refreshKey={tick} />
        <CloudDeploysSection refreshKey={tick} />
      </div>
    </div>
  );
}
