"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  AlertOctagon,
  BarChart3,
  Clapperboard,
  Coins,
  ExternalLink,
  ListChecks,
  Loader2,
  RefreshCw,
  Server,
} from "lucide-react";
import { toast } from "sonner";

import { PageHeader } from "@/components/app/page-header";
import { Button } from "@/components/ui/button";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

import { TelemetryOverviewSection } from "./overview-section";
import { TelemetryServicesSection } from "./services-section";
import { TelemetryTimelineSection } from "./timeline-section";
import { TelemetryStageLatencySection } from "./stage-latency-section";
import { TelemetryRendersSection } from "./renders-section";
import { TelemetryJobsSection } from "./jobs-section";
import { TelemetryLLMCostsSection } from "./llm-costs-section";
import { TelemetryErrorsSection } from "./errors-section";
import { TelemetryLinksSection } from "./links-section";

export default function TelemetryPage() {
  const [tick, setTick] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [tab, setTab] = useState<string>("jobs");

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
        description="Live span / event / metric stream from every laptop pipeline stage and Cloud Run service. Switch tabs to drill into jobs, renders, stages, services, LLM cost, errors, or activity. Deep-link into Cloud Trace / Logging / Monitoring at the bottom."
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

      <div className="flex flex-1 flex-col gap-6 px-6 py-6 md:px-8">
        <TelemetryOverviewSection refreshKey={tick} />

        <Tabs value={tab} onValueChange={setTab} className="flex flex-col gap-4">
          <TabsList className="flex w-full flex-wrap justify-start gap-1 bg-transparent p-0">
            <TabsTrigger
              value="jobs"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <ListChecks className="h-3.5 w-3.5" />
              Jobs
              <span className="rounded bg-muted-foreground/15 px-1 py-0.5 text-[9px] font-mono uppercase tracking-wider">
                Firestore
              </span>
            </TabsTrigger>
            <TabsTrigger
              value="renders"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <Clapperboard className="h-3.5 w-3.5" />
              Renders
              <span className="rounded bg-muted-foreground/15 px-1 py-0.5 text-[9px] font-mono uppercase tracking-wider">
                Cloud Logging
              </span>
            </TabsTrigger>
            <TabsTrigger
              value="stages"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <BarChart3 className="h-3.5 w-3.5" />
              Stage latency
            </TabsTrigger>
            <TabsTrigger
              value="services"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <Server className="h-3.5 w-3.5" />
              Services
            </TabsTrigger>
            <TabsTrigger
              value="llm"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <Coins className="h-3.5 w-3.5" />
              LLM cost
            </TabsTrigger>
            <TabsTrigger
              value="errors"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <AlertOctagon className="h-3.5 w-3.5" />
              Errors
            </TabsTrigger>
            <TabsTrigger
              value="activity"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <Activity className="h-3.5 w-3.5" />
              Activity
            </TabsTrigger>
            <TabsTrigger
              value="links"
              className="gap-1.5 data-[state=active]:bg-surface/40 data-[state=active]:text-foreground"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              Open in GCP
            </TabsTrigger>
          </TabsList>

          <TabsContent value="jobs" className="mt-0">
            <TelemetryJobsSection refreshKey={tick} />
          </TabsContent>

          <TabsContent value="renders" className="mt-0">
            <TelemetryRendersSection refreshKey={tick} />
          </TabsContent>

          <TabsContent value="stages" className="mt-0">
            <TelemetryStageLatencySection refreshKey={tick} />
          </TabsContent>

          <TabsContent value="services" className="mt-0">
            <TelemetryServicesSection refreshKey={tick} />
          </TabsContent>

          <TabsContent value="llm" className="mt-0">
            <TelemetryLLMCostsSection refreshKey={tick} />
          </TabsContent>

          <TabsContent value="errors" className="mt-0">
            <TelemetryErrorsSection refreshKey={tick} />
          </TabsContent>

          <TabsContent value="activity" className="mt-0">
            <TelemetryTimelineSection refreshKey={tick} />
          </TabsContent>

          <TabsContent value="links" className="mt-0">
            <TelemetryLinksSection refreshKey={tick} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}

