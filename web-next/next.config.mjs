/** @type {import('next').NextConfig} */

const nextConfig = {
  reactStrictMode: true,
  // Was: output: "standalone". Switched to default `next start` mode
  // because the standalone bundle was producing SSR responses missing
  // the <!DOCTYPE>/<html>/<body> wrappers on Cloud Run (locally fine,
  // remotely broken — likely a streaming-flush bug in the standalone
  // server's HTTP write path on alpine). next start works.
  // Audit Q2.55 — pre-fix the comment claimed "Type safety is
  // enforced via `npm run typecheck`" but the npm `build` script
  // didn't actually run tsc — prod builds could ship with hook-rule
  // violations and unused imports. Now `prebuild` runs both
  // `typecheck` and `lint` (see package.json). ESLint stays off
  // here because the false-positive jsx-no-undef-on-hoisted-helpers
  // is a real productivity tax during dev — the lint pass runs
  // explicitly via `npm run lint` instead.
  eslint: {
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
