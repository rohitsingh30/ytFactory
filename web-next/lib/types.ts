/**
 * Shared types mirroring the FastAPI control plane.
 *
 * Hand-written for now; can codegen from OpenAPI later (FastAPI exposes
 * /openapi.json automatically). Keeping them tight and well-commented
 * avoids drift in practice.
 */

export type ChannelKey =
  | "mystoriesanimated"
  | "sportsrecapped"
  | "hindutavaanimated"
  | "historyrecapped"
  | "rhymetimejunction"
  | "scrollpulse"
  | "cosmosdecoded";

export type JobStatus =
  | "pending"
  | "rendering"
  | "uploading"
  | "researching"
  | "done"
  | "failed"
  | "cancelled";

export interface RecentVideo {
  video_id: string;
  title: string;
  thumbnail?: string | null;
  views?: number | null;
  watch_url: string;
}

export interface ChannelSummary {
  key: ChannelKey | string;
  label: string;
  tagline: string;
  language: string;
  default_format: string;
  default_voice?: string | null;
  default_length_s?: number | null;
  image_provider?: string | null;
  tts_provider?: string | null;
  /**
   * Audio backend declared by the channel YAML — drives the Customize
   * step's Voice/Song flip default. Values: "tts" (default) | "sunoapi"
   * | "external_song". Pre-selects the Song tab for sunoapi/external_song
   * channels (rhymetimejunction today).
   */
  audio_provider?: string;
  has_overrides?: boolean;
  variants_count?: number;

  // Personality (populated from cached YT data; null when channel
  // hasn't been auth'd yet — UI falls back to monogram/gradient).
  avatar_url?: string | null;
  banner_url?: string | null;
  youtube_url?: string | null;
  custom_url?: string | null;
  subscribers?: number | null;
  youtube_video_count?: number | null;
  total_views?: number | null;
  recent_videos?: RecentVideo[];
}

export type FieldKind =
  | "select"
  | "text"
  | "textarea"
  | "number"
  | "slider"
  | "switch"
  | "url"
  | "datetime"
  | "hidden";

export interface FieldOption {
  value: string;
  label: string;
  description?: string | null;
}

export interface CustomizationField {
  key: string;
  label: string;
  kind: FieldKind;
  help?: string;
  default?: string | number | boolean | null;
  required?: boolean;
  options?: FieldOption[];
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string;
  maxLength?: number;
}

export interface CustomizationSchema {
  channel: string;
  label: string;
  tagline: string;
  language: string;
  variants: FieldOption[];
  fields: CustomizationField[];
  /** Sidecar-saved niche (variant key); fed by the channel /defaults Niche tab. */
  default_variant?: string | null;
}

export interface ShortProposal {
  channel: ChannelKey | string;
  format: string;
  topic: string;
  source_kind: string;
  source_ref: string | null;
  length_s: number;
  notes: string;
  channel_overrides?: Record<string, unknown>;
}

export interface TimelineEntry {
  stage: string;
  label?: string;
  status: "pending" | "running" | "done" | "failed";
  ts?: string;
  msg?: string;
}

export interface CritiqueResult {
  verdict: "SHIP" | "FIX" | "BLOCK";
  weakest_param?: string;
  notes?: string;
}

export interface ArtifactEntry {
  status: "pending" | "ready" | "failed";
  uri?: string | null;
  version?: number;
  // Free-form per-kind extras (duration_s for narration, n_beats for
  // beats, hook for script, etc). Render-detail page may pluck whichever
  // it wants; unknown keys are passed through harmlessly.
  [k: string]: unknown;
}

export interface Job {
  job_id: string;
  channel?: ChannelKey | string;
  topic?: string;
  status: JobStatus;
  stage?: string;
  short_uri?: string | null;
  preview_url?: string | null;
  youtube_url?: string | null;
  thumb_uri?: string | null;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
  proposal?: ShortProposal | Record<string, unknown>;
  timeline?: TimelineEntry[];
  critique?: CritiqueResult | null;
  log_tail?: string;
  publish_meta?: Record<string, unknown>;
  // Slice 4 — live artifact previews. Server-side
  // pipeline.render.artifacts.emit_artifact populates this map as each
  // intermediate is produced. Single-entry kinds (script, narration,
  // beats, video, thumb, envelope) are objects; list-typed kinds
  // (images, panels) are arrays.
  artifacts?: Record<string, ArtifactEntry | ArtifactEntry[]> | null;
  // Slice 1 — resolved RenderSpec. Lets the UI show "the system
  // interpreted your inputs as kind=long_form, aspect=16:9".
  render_spec?: Record<string, unknown> | null;
}

export interface JobListResponse {
  jobs: Job[];
  total: number;
}

export interface QueueHeldEntry {
  channel: string;
  slug: string;
  reason?: string;
  set_at?: string | null;
  // Kept for backwards compatibility with any older consumer that read
  // `held_at`; the backend now always populates both with the same value.
  held_at?: string | null;
  source_critique?: string | null;
}

export interface QueueState {
  queued: Job[];
  running: Job[];
  completed: Job[];
  held: QueueHeldEntry[];
  // Per-section error strings populated when the backend's underlying
  // Firestore query failed (e.g. missing composite index). The UI
  // surfaces these so a half-broken backend never silently presents
  // itself as "Empty" — historically a missing jobs(status,updated_at)
  // composite index made the Completed column always show 0 even when
  // 18 real terminal jobs existed in Firestore.
  warnings?: { queued?: string; running?: string; completed?: string };
}

export interface RenderRequest {
  channel: string;
  topic: string;
  notes?: string;
  format?: string;
  source_kind?: string;
  source_ref?: string | null;
  length_s?: number;
  channel_overrides?: Record<string, unknown>;
}

export interface RenderResponse {
  job_id: string;
  task_id: string;
  proposal: ShortProposal;
}

export interface PublishRequest {
  visibility: "public" | "unlisted" | "private";
  schedule_at?: string | null;
  title?: string;
  description?: string;
  tags?: string[];
  upload_method?: "auto" | "api" | "playwright";
}

export interface PublishResponse {
  job_id: string;
  status: "submitted" | "scheduled" | "uploading" | "done" | "failed";
  youtube_url?: string;
  error?: string | null;
}

export interface DashboardData {
  renders_7d?: number;
  uploads_7d?: number;
  queued?: number;
  held?: number;
  channels?: ChannelSummary[];
  recent_jobs?: Job[];
  top_performer?: {
    youtube_url?: string;
    title?: string;
    thumb_url?: string;
    views?: number;
    channel?: string;
  } | null;
}

export interface CritiqueDetail {
  channel: string;
  slug: string;
  path: string;
  exists: boolean;
  markdown?: string | null;
  verdict?: "SHIP" | "FIX" | "BLOCK" | string | null;
  total?: number | null;
  avg?: number | null;
  weakest_param?: string | null;
  weakest_score?: number | null;
  critical_failures?: [string, number][];
  fix_instructions?: string | null;
  gut_check?: string | null;
  date?: string | null;
}

export type ResolveHoldAction = "operator_verdict" | "request_recritique" | "clear";
export type OperatorVerdict = "SHIP" | "FIX" | "BLOCK";

export interface ResolveHoldRequest {
  action: ResolveHoldAction;
  // Required when action="operator_verdict"; ignored otherwise.
  verdict?: OperatorVerdict | null;
  notes?: string;
}

export interface ResolveHoldResponse {
  channel: string;
  slug: string;
  action: ResolveHoldAction;
  hold_cleared: boolean;
  new_hold_reason?: string | null;
  operator_critique_path?: string | null;
  archived_critique_path?: string | null;
  next_step_hint?: string | null;
}


/* ----------------------------- Niche v2 (JSON-doc niches) ----------------------------- */

export type NicheSourceKind =
  | "reddit"
  | "wikipedia"
  | "manual"
  | "x_twitter"
  | "youtube"
  | "rss";

export type NicheFormat =
  | "animated"
  | "text"
  | "cooking"
  | "footage"
  | "split_screen"
  | "rhyme"
  | "footage_only"
  | "long_form"
  | "sports_doc";

export type NicheCreatedBy = "backfill" | "user" | "ai_chat";

export type NicheLengthKind = "short" | "long";

export interface NicheDoc {
  key: string;
  label: string;
  description: string;
  prompt_style_guide: string;
  length_kind: NicheLengthKind;
  voice: string;
  format: NicheFormat;
  source_kind: NicheSourceKind;
  source_ref: string | null;
  hook_template: string;
  closer_template: string;
  image_style: string;
  music_bed: string | null;
  created_at: string;
  created_by: NicheCreatedBy;
}

export interface NicheDraftResponse {
  draft: NicheDoc;
  ai_configured: boolean;
}

/* ----------------------------- Burner channels ----------------------------- */

export interface BurnerChannel {
  slug: string;
  channel_id: string;
  title: string;
  discovered_at: string;
  has_token: boolean;
  email: string | null;
  profile_known: boolean;
  // Hydrated by the list endpoint when state exists
  running?: boolean;
  phase?: BurnerEngagePhase | null;
  last_action_at?: string | null;
  last_action_msg?: string | null;
}

export type BurnerEngagePhase =
  | "initializing"
  | "engaging"
  | "watching"
  | "stopped"
  | "failed"
  | "blocked";

export interface BurnerCatalogEntry {
  video_id: string;
  channel: string;
  channel_label: string;
  slug: string;
  title: string;
  url: string;
  uploaded_at: string;
}

export interface BurnerEngageVideo {
  video_id: string;
  channel: string;
  channel_label: string;
  title: string;
  url: string;
  liked: boolean;
  subscribed: boolean;
  tab_open: boolean;
  last_focused_at: string;
  watch_seconds: number;
  error: string;
}

export interface BurnerEngageState {
  slug: string;
  channel_id: string;
  started_at: string;
  phase: BurnerEngagePhase;
  last_action_at: string;
  last_action_msg: string;
  stop_requested: boolean;
  videos: BurnerEngageVideo[];
  running?: boolean;
}
