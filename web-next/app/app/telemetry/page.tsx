"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { PageHeader } from "@/components/app/page-header";
import { Button } from "@/components/ui/button";

import { TelemetryOverviewSection } from "./overview-section";
import { TelemetryServicesSection } from "./services-section";
import { TelemetryTimelineSection } from "./timeline-section";
import { TelemetryErrorsSection } from "./errors-section";
import { TelemetryLinksSection } from "./links-section";

export default function TelemetryPage() {
  const [tick, setTick] = useState(0);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      setTick((t) => t + 1);
      toast.success("Telemetry refreshed");
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    setTick((t) => t + 1);
  }, []);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        eyebrow="Observability"
        title="Telemetry"
        description="Live span / event / metric stream from every laptop pipeline stage and Cloud Run service. Deep-link into Cloud Trace / Logging / Monitoring for the full waterfall."
        actions={
          <div className="flex items-center gap-2">
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
        <TelemetryOverviewSection refreshKey={tick} />
        <TelemetryTimelineSection refreshKey={tick} />
        <TelemetryServicesSection refreshKey={tick} />
        <TelemetryErrorsSection refreshKey={tick} />
        <TelemetryLinksSection refreshKey={tick} />
      </div>
    </div>
  );
}
