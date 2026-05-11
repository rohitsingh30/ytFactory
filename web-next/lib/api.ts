/**
 * Typed wrapper around the FastAPI control-plane `/api/*` surface.
 *
 * The dev server proxies `/api/*` to the Python backend (see
 * `next.config.mjs`), so callers always use relative URLs. The
 * production deploy fronts both behind the same domain.
 */

import type {
  BurnerCatalogEntry,
  BurnerChannel,
  BurnerEngageState,
  ChannelSummary,
  CritiqueDetail,
  CustomizationSchema,
  Job,
  JobListResponse,
  NicheDoc,
  NicheDraftResponse,
  PublishRequest,
  PublishResponse,
  QueueState,
  RenderRequest,
  ResolveHoldRequest,
  ResolveHoldResponse,
} from "@/lib/types";

export class ApiError extends Error {
  public readonly status: number;
  public readonly body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  let body: BodyInit | undefined = init.body ?? undefined;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  if (!headers.has("Accept")) headers.set("Accept", "application/json");

  const res = await fetch(path, { ...init, headers, body, credentials: "include" });

  if (!res.ok) {
    let parsed: unknown;
    try {
      parsed = await res.json();
    } catch {
      parsed = await res.text().catch(() => "");
    }
    throw new ApiError(`${res.status} ${res.statusText} on ${path}`, res.status, parsed);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("Content-Type") ?? "";
  if (ct.includes("application/json")) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path, { method: "GET", cache: "no-store" }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", json: body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", json: body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", json: body }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

/** Polling helper used by render-detail for live timeline updates.
 *  Returns an unsubscribe function. */
export function pollJob(
  jobId: string,
  onUpdate: (job: Job) => void,
  opts: { intervalMs?: number; stopWhen?: (job: Job) => boolean } = {},
): () => void {
  let cancelled = false;
  const interval = opts.intervalMs ?? 750;
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function tick() {
    if (cancelled) return;
    try {
      const job = await api.get<Job>(`/api/jobs/${jobId}`);
      if (cancelled) return;
      onUpdate(job);
      if (opts.stopWhen?.(job)) return;
    } catch {
      /* swallow — keep polling */
    }
    timer = setTimeout(tick, interval);
  }

  tick();
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
  };
}

export const channelsApi = {
  list: () => api.get<{ channels: ChannelSummary[] }>("/api/channels"),
  get: (channel: string) => api.get<ChannelSummary>(`/api/channels/${channel}`),
  schema: (channel: string) =>
    api.get<CustomizationSchema>(`/api/channels/${channel}/customization_schema`),
  patchDefaults: (channel: string, defaults: Record<string, unknown>) =>
    api.patch<{ ok: boolean; channel: string }>(`/api/channels/${channel}/defaults`, {
      defaults,
    }),
  // Brand assets — served as image/jpeg from the mirrored cache. The
  // <img onError> handler in ChannelAvatar swaps to a monogram when 404.
  avatarUrl: (channel: string) => `/api/channels/${channel}/avatar.jpg`,
  bannerUrl: (channel: string) => `/api/channels/${channel}/banner.jpg`,
  inspiration: (channel: string, limit?: number) =>
    api.get<{
      channel: string;
      label: string;
      youtube_url: string | null;
      videos: {
        video_id: string;
        title: string;
        thumbnail: string | null;
        views: number | null;
        watch_url: string;
      }[];
    }>(
      `/api/channels/${channel}/inspiration${
        limit !== undefined ? `?limit=${encodeURIComponent(limit)}` : ""
      }`,
    ),
};

export const ENGAGE_MODES = [
  "subscribe_only",
  "like_subscribe",
  "like_subscribe_view",
  "complete",
] as const;
export type EngageMode = (typeof ENGAGE_MODES)[number];

export const ENGAGE_MODE_LABELS: Record<EngageMode, string> = {
  subscribe_only: "Subscribe only",
  like_subscribe: "Like + Subscribe",
  like_subscribe_view: "Like + Subscribe + Watch loop",
  complete: "Complete (with comments)",
};

export const ENGAGE_MODE_DESCRIPTIONS: Record<EngageMode, string> = {
  subscribe_only:
    "Subscribe to every channel once. No likes, no watch loop. Exits when done.",
  like_subscribe:
    "Like every video + subscribe to every channel. No watch loop. Exits when done.",
  like_subscribe_view:
    "Like + subscribe + permanent watch-time loop (default). Runs until you click Stop.",
  complete:
    "Like + subscribe + watch loop + LLM-generated comments on a random subset. Highest engagement, highest shadow-ban risk.",
};

export const burnerApi = {
  list: () =>
    api.get<{ burners: BurnerChannel[]; catalog_size: number }>("/api/burner_channels"),
  catalog: () =>
    api.get<{ videos: BurnerCatalogEntry[]; total: number }>("/api/burner_channels/catalog"),
  start: (slug: string, mode: EngageMode = "like_subscribe_view") =>
    api.post<{ started: boolean; pid?: number; reason?: string; log_path?: string; state?: BurnerEngageState; mode?: EngageMode }>(
      `/api/burner_channels/${slug}/engage`,
      { mode },
    ),
  poll: (slug: string) =>
    api.get<BurnerEngageState>(`/api/burner_channels/${slug}/engage`),
  stop: (slug: string) =>
    api.post<{ stop_requested: boolean; slug: string }>(
      `/api/burner_channels/${slug}/engage/stop`,
      {},
    ),
  subscribeAllBurners: () =>
    api.post<{
      enqueued_count: number;
      skipped_count: number;
      enqueued: { slug: string; task_id: string }[];
      skipped: { slug: string; reason: string }[];
      hint?: string;
    }>(`/api/burner_channels/subscribe_all_burners`, {}),
  createBulk: (count: number, opts?: { email?: string; oauth?: boolean }) =>
    api.post<{
      enqueued_count: number;
      enqueued: { task_id: string }[];
      cap_applied: boolean;
      hint?: string;
    }>(`/api/burner_channels/create_bulk`, {
      count,
      ...(opts?.email ? { email: opts.email } : {}),
      ...(opts?.oauth === false ? { oauth: false } : {}),
    }),
};

export const nichesApi = {
  list: (channel: string) =>
    api.get<{ niches: NicheDoc[] }>(`/api/channels/${channel}/niches`),
  get: (channel: string, key: string) =>
    api.get<NicheDoc>(`/api/channels/${channel}/niches/${key}`),
  create: (channel: string, doc: NicheDoc) =>
    api.post<NicheDoc>(`/api/channels/${channel}/niches`, doc),
  update: (channel: string, key: string, doc: NicheDoc) =>
    api.put<NicheDoc>(`/api/channels/${channel}/niches/${key}`, doc),
  delete: (channel: string, key: string) =>
    api.del<{ deleted: string }>(`/api/channels/${channel}/niches/${key}`),
  draft: (channel: string, description: string) =>
    api.post<NicheDraftResponse>(`/api/channels/${channel}/niches/draft`, { description }),
};

export const jobsApi = {
  list: (params: { channel?: string; status?: string; limit?: number; offset?: number } = {}) => {
    const qs = new URLSearchParams();
    if (params.channel) qs.set("channel", params.channel);
    if (params.status) qs.set("status", params.status);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    const q = qs.toString();
    return api.get<JobListResponse>(`/api/jobs${q ? `?${q}` : ""}`);
  },
  get: (jobId: string) => api.get<Job>(`/api/jobs/${jobId}`),
  cancel: (jobId: string) =>
    api.post<{ job_id: string; cancelled_tasks: number; job_status: string }>(
      `/api/jobs/${jobId}/cancel`,
    ),
  publish: (jobId: string, body: PublishRequest) =>
    api.post<PublishResponse>(`/api/jobs/${jobId}/publish`, body),
  previewUrl: (jobId: string) => `/api/jobs/${jobId}/preview.mp4`,
};

export const renderApi = {
  enqueue: (req: RenderRequest) =>
    api.post<{ job_id: string; task_id: string; proposal: Record<string, unknown> }>(
      "/api/render",
      req,
    ),
};

export interface DiscoverItem {
  topic: string;
  source_kind: string;
  source_ref: string | null;
  source_label: string;
  source_excerpt?: string | null;
  metadata?: Record<string, unknown>;
}

export interface DiscoverFeed {
  channel: string;
  adapter: string;
  items: DiscoverItem[];
}

export interface DiscoverContext {
  variant?: string | null;
  length_kind?: string | null;
  language?: string | null;
  niche_key?: string | null;
  values?: Record<string, unknown>;
  avoid?: string[];
}

function discoverFeedQuery(ctx?: DiscoverContext): string {
  if (!ctx) return "";
  const params = new URLSearchParams();
  if (ctx.variant) params.set("variant", ctx.variant);
  if (ctx.length_kind) params.set("length_kind", ctx.length_kind);
  if (ctx.language) params.set("language", ctx.language);
  if (ctx.niche_key) params.set("niche_key", ctx.niche_key);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export const discoverApi = {
  feed: (channel: string, ctx?: DiscoverContext) =>
    api.get<DiscoverFeed>(`/api/discover/${channel}/feed${discoverFeedQuery(ctx)}`),
  pickOne: (channel: string, ctx?: DiscoverContext) =>
    api.post<DiscoverItem>(`/api/discover/${channel}`, ctx ?? undefined),
};

export interface VoiceInfo {
  key: string;
  label: string;
  language: string;
  gender: string;
  style: string;
  sample_url: string | null;
  notes?: string | null;
  is_clone?: boolean;
  duration_s?: number | null;
  category?: string;
  use_cases?: string[];
  provider?: string;
  tones?: string[];
}

export interface FamousVoiceTemplate {
  key: string;
  label: string;
  language: string;
  description: string;
  hint: string;
  initial_letters: string;
  closest_voice_key?: string | null;
  closest_voice_url?: string | null;
}

export interface CloneResult {
  voice: VoiceInfo;
  duration_s: number;
  note?: string;
}

export const voicesApi = {
  list: () => api.get<{ voices: VoiceInfo[] }>("/api/voices/catalog"),
  famous: () => api.get<{ templates: FamousVoiceTemplate[] }>("/api/voices/famous"),
  sampleUrl: (key: string) => `/api/voices/sample/${key}.wav`,
  /** Multipart upload — bypasses our JSON wrapper. */
  clone: async (params: {
    name: string;
    language: string;
    transcript: string;
    audio: File;
  }): Promise<CloneResult> => {
    const fd = new FormData();
    fd.set("name", params.name);
    fd.set("language", params.language);
    fd.set("transcript", params.transcript);
    fd.set("audio", params.audio);
    const res = await fetch("/api/voices/clone", {
      method: "POST",
      body: fd,
      credentials: "include",
    });
    if (!res.ok) {
      let detail: string | undefined;
      try {
        detail = (await res.json()).detail;
      } catch {
        detail = await res.text().catch(() => undefined);
      }
      throw new ApiError(`${res.status} ${res.statusText}`, res.status, detail);
    }
    return (await res.json()) as CloneResult;
  },
  delete: (key: string) => api.del<{ ok: boolean; key: string }>(`/api/voices/clone/${key}`),
};

export interface MusicBed {
  key: string;
  label: string;
  channel: string;
  filename: string;
  sample_url: string;
}

export const musicApi = {
  list: () => api.get<{ music: MusicBed[] }>("/api/music/catalog"),
  sampleUrl: (channel: string, filename: string) =>
    `/api/music/sample/${channel}/${filename}`,
};

export const queueApi = {
  get: () => api.get<QueueState>("/api/queue"),
};

export const critiquesApi = {
  // Fetch the markdown critique + parsed metadata for a held slug. Powers
  // the Queue page's "Held by critic" detail dialog.
  getHeld: (channel: string, slug: string) =>
    api.get<CritiqueDetail>(
      `/api/critiques/${encodeURIComponent(channel)}/${encodeURIComponent(slug)}`,
    ),
  // Resolve a hold via one of three workflows: operator verdict (SHIP /
  // FIX / BLOCK with notes), request fresh AI re-critique, or pure
  // dismiss. See backend `ResolveHoldRequest` for action semantics.
  resolve: (
    channel: string,
    slug: string,
    body: ResolveHoldRequest,
  ) =>
    api.post<ResolveHoldResponse>(
      `/api/critiques/${encodeURIComponent(channel)}/${encodeURIComponent(slug)}/resolve`,
      body,
    ),
};

export const healthApi = {
  get: () =>
    api.get<{
      ok: boolean;
      agents: { agent_id: string; seconds_ago: number; mlx_free_pct: number }[];
      azure_spend_usd_today: number;
      azure_spend_cap_usd: number;
      sim_worker?: {
        enabled: boolean;
        running: boolean;
        speed_multiplier: number;
        placeholder_exists: boolean;
      };
    }>("/api/health"),
};

export const dashboardApi = {
  videos: (refresh?: boolean) =>
    api.get<{
      channels: {
        account: string;
        video_count: number;
        subscribers: number | null;
        totals: { views: number; likes: number; comments: number };
        videos: {
          slug: string;
          video_id: string;
          title: string;
          uploaded_at: string | null;
          watch_url: string;
          thumbnail: string;
          stats: { views: number | null; likes: number | null; comments: number | null };
        }[];
      }[];
      totals: {
        videos: number;
        views: number;
        likes: number;
        comments: number;
        subscribers: number;
      };
      latest_fetch: string | null;
      warning?: string;
    }>(`/api/dashboard/videos${refresh ? "?refresh=true" : ""}`),
};

