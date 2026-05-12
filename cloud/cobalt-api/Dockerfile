# Cobalt downloader API as a Cloud Run service.
# Re-uses the official cobalt image; only purpose of this Dockerfile is
# to expose an entrypoint that respects $PORT (Cloud Run requirement).
FROM ghcr.io/imputnet/cobalt:11

# cobalt picks up its bind port from API_PORT (defaults 9000). Cloud
# Run injects $PORT — wire it through.
ENV API_PORT=8080
ENV API_URL=https://localhost/
ENV API_LISTEN_ADDRESS=0.0.0.0
ENV API_INSTANCE_COUNT=1
# Disable rate limiting (we're invoking from a single trusted backend).
ENV RATELIMIT_WINDOW=60
ENV RATELIMIT_MAX=100000

EXPOSE 8080

COPY otel_init.py ./
