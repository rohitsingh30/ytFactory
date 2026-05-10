# ytFactory web (Next.js) — Cloud Run SERVICE.
#
# CRITICAL: we do NOT run `next build` inside this image. Building on
# Linux with this dep set produces broken prerendered HTML — the
# index.html / login.html / etc. that `next build` writes to
# .next/server/app/ are missing the <!DOCTYPE><html><head><body>
# opening tags but still have </body></html> at the end. The exact
# same source builds correctly on macOS. Reproduced 2026-05-10:
# deployed image had `BUILD_ID=x_r4RNMhFxNCtA_NRAThA`,
# `.next/server/app/index.html` first 200 bytes were
# `<meta charSet="utf-8"/>...` (no doctype), local build wrote
# `<!DOCTYPE html><html lang="en" class="dark ...">`.
#
# Workaround: build .next/ on the laptop with `npm run build`, ship
# the built artifacts as part of the build context. Inside Docker we
# only install the runtime node_modules (deterministic via npm ci).
#
# Pre-build step (run before deploy.sh):
#   cd web-next && npm run build
#
# Deploy:
#   cloud/web-next/deploy.sh

FROM node:20-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=8080
ENV HOSTNAME=0.0.0.0

# Runtime-only install: lock-honoring, no build step. Linux-x64 binaries
# (e.g. @next/swc-linux-x64-gnu) get fetched here so `next start` works.
COPY web-next/package.json web-next/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
 && npm cache clean --force

# Ship the .next/ that was built on the laptop. The build context's
# .gcloudignore already excludes .next/cache/ so only server/, static/,
# types/, BUILD_ID, etc. are uploaded (~6 MB).
COPY web-next/.next ./.next
COPY web-next/public ./public
COPY web-next/next.config.mjs ./next.config.mjs

EXPOSE 8080
# Cloud Run sets PORT; next start binds to it via -p.
CMD ["sh", "-c", "node node_modules/next/dist/bin/next start -p ${PORT:-8080} -H 0.0.0.0"]
