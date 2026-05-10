/**
 * Shared types for the /app/cloud admin tab.
 *
 * Mirrors the dataclasses in `pipeline/cloud/{health,cost,deploys}.py`
 * — keep in sync if the backend payloads change.
 */

export type CloudServiceKind = "tts" | "image" | "video" | "infra";
export type HealthStatus = "green" | "yellow" | "red" | "unconfigured";

export interface HealthRow {
  short: string;
  name: string;
  kind: CloudServiceKind;
  url: string;
  health_url: string;
  status: HealthStatus;
  http_status: number | null;
  latency_ms: number | null;
  warm_s: number | null;
  cold_loaded: boolean | null;
  model_repo: string | null;
  error: string | null;
  flags: string[];
  checked_at: number;
}

export interface HealthResponse {
  summary: Record<HealthStatus, number>;
  rows: HealthRow[];
}

export interface CostPoint {
  day: string; // YYYY-MM-DD
  service_short: string;
  cost_usd: number;
}

export interface CostResponse {
  available: boolean;
  reason?: string;
  days?: number;
  range_start?: string;
  range_end?: string;
  points?: CostPoint[];
  per_service_today?: Record<string, number>;
  per_service_mtd?: Record<string, number>;
  per_service_30d_median?: Record<string, number>;
  drift_flags?: Record<string, string>;
  total_today?: number;
  total_mtd?: number;
  fetched_at?: string;
  snapshot_at?: string;
}

export interface PrepStep {
  key: string;
  label: string;
  done: boolean;
  artifact: string | null;
}

export interface DeployRow {
  short: string;
  name: string;
  kind: CloudServiceKind;
  last_build_id: string | null;
  last_build_at: string | null;
  last_build_status: string | null;
  image_digest: string | null;
  log_url: string | null;
  prep_steps: PrepStep[];
  prep_done: number;
  prep_total: number;
}

export interface DeploysResponse {
  available: boolean;
  reason?: string;
  project?: string;
  rows?: DeployRow[];
  snapshot_at?: string;
}
