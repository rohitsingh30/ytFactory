/**
 * Catch-all API proxy.
 *
 * Forwards every /api/* request to the FastAPI control plane.
 *
 * Why a Route Handler (instead of next.config.mjs rewrites): we need
 * to inject a Cloud Run-aware ID token on the outbound call so the
 * control service's IAM check accepts us as an authenticated invoker.
 * Rewrites just forward the request as-is — they can't mint tokens.
 *
 * Auth flow:
 *   - Local dev (no metadata server): no token added; control plane is
 *     public on localhost:8766.
 *   - Cloud Run: fetch an ID token from the metadata server with
 *     audience = control URL, attach as `Authorization: Bearer <jwt>`.
 *     Token is cached in-memory until expiry (default 1h).
 */

const API_BASE = (
  process.env.YTFACTORY_API_BASE || "http://127.0.0.1:8766"
).replace(/\/$/, "");

let _cachedToken: { audience: string; token: string; expiresAt: number } | null = null;

async function _getIdToken(audience: string): Promise<string | null> {
  const now = Date.now();
  if (
    _cachedToken
    && _cachedToken.audience === audience
    && _cachedToken.expiresAt > now + 60_000
  ) {
    return _cachedToken.token;
  }

  const url = `http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=${encodeURIComponent(audience)}`;
  try {
    const r = await fetch(url, {
      headers: { "Metadata-Flavor": "Google" },
      signal: AbortSignal.timeout(2000),
    });
    if (!r.ok) return null;
    const token = (await r.text()).trim();
    if (!token) return null;
    _cachedToken = { audience, token, expiresAt: now + 55 * 60_000 };
    return token;
  } catch {
    return null;
  }
}

async function _proxy(
  req: Request,
  ctx: { params: { path: string[] } },
): Promise<Response> {
  if (process.env.YTFACTORY_FRONTEND_ONLY === "1") {
    return Response.json(
      { error: "api_not_connected", message: "The website is online. The API is not connected yet." },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  const upstreamPath = "/api/" + (ctx.params.path?.join("/") ?? "");
  const url = new URL(req.url);
  const upstreamUrl = API_BASE + upstreamPath + (url.search || "");

  const outHeaders = new Headers();
  for (const [k, v] of req.headers.entries()) {
    if (k === "host" || k === "authorization") continue;
    outHeaders.set(k, v);
  }

  // Internal Kubernetes service: preserve the caller's bearer for the API
  // to validate. Never inject a privileged shared token for anonymous users.
  // Cloud Run retains its existing audience-scoped IAM identity flow.
  if (process.env.YTFACTORY_API_AUTH_MODE === "passthrough") {
    const authorization = req.headers.get("authorization");
    if (authorization) outHeaders.set("Authorization", authorization);
  } else {
    const aud = new URL(API_BASE).origin;
    const idToken = await _getIdToken(aud);
    if (idToken) outHeaders.set("Authorization", `Bearer ${idToken}`);
  }

  let body: BodyInit | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.arrayBuffer();
  }

  let upstreamRes: Response;
  try {
    upstreamRes = await fetch(upstreamUrl, {
      method: req.method,
      headers: outHeaders,
      body,
      redirect: "manual",
    });
  } catch (err) {
    // Upstream unreachable (control plane down, DNS, network, TLS, …).
    // Without this catch the runtime returns an opaque 500/502 with no body,
    // which surfaces in the browser console as a bare `502` and is impossible
    // to triage. Returning a structured payload makes it obvious that the
    // proxy reached us but the FastAPI control plane couldn't be reached.
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[api proxy] upstream unreachable for ${req.method} ${upstreamUrl}: ${message}`);
    return new Response(
      JSON.stringify({
        error: "upstream_unavailable",
        upstream: upstreamUrl,
        method: req.method,
        message,
        hint:
          API_BASE.startsWith("http://127.0.0.1") || API_BASE.startsWith("http://localhost")
            ? "The FastAPI control plane is not running. Start it with: uvicorn control.server_dev:app --port 8766"
            : "The control-plane Cloud Run service did not respond. Check its health endpoint.",
      }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      },
    );
  }

  const respHeaders = new Headers(upstreamRes.headers);
  for (const h of [
    "transfer-encoding", "content-encoding", "content-length", "connection",
  ]) {
    respHeaders.delete(h);
  }
  return new Response(upstreamRes.body, {
    status: upstreamRes.status,
    statusText: upstreamRes.statusText,
    headers: respHeaders,
  });
}

export const GET = _proxy;
export const POST = _proxy;
export const PUT = _proxy;
export const PATCH = _proxy;
export const DELETE = _proxy;
export const HEAD = _proxy;
export const OPTIONS = _proxy;

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
