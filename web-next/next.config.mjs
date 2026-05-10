/** @type {import('next').NextConfig} */

const nextConfig = {
  reactStrictMode: true,
  // Was: output: "standalone". Switched to default `next start` mode
  // because the standalone bundle was producing SSR responses missing
  // the <!DOCTYPE>/<html>/<body> wrappers on Cloud Run (locally fine,
  // remotely broken — likely a streaming-flush bug in the standalone
  // server's HTTP write path on alpine). next start works.
  eslint: {
    // Type safety is enforced via `npm run typecheck`. ESLint adds false-positive
    // jsx-no-undef diagnostics for hoisted helper components defined later in
    // the same file. Skip during prod builds.
    ignoreDuringBuilds: true,
  },
  // The /api/* proxy with auth-token injection lives in
  // app/api/[...path]/route.ts (Route Handler). We keep /healthz +
  // /agent/* as plain rewrites since those don't need auth on the
  // outbound call.
  async rewrites() {
    const API_BASE = (
      process.env.YTFACTORY_API_BASE || "http://127.0.0.1:8766"
    ).replace(/\/$/, "");
    return [
      { source: "/agent/:path*", destination: `${API_BASE}/agent/:path*` },
      { source: "/healthz", destination: `${API_BASE}/healthz` },
    ];
  },
};

export default nextConfig;
