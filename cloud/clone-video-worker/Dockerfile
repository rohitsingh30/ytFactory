# Slim CPU image — yt-dlp + ffmpeg + Node.js (for the bgutil PO token
# provider sidecar) + FastAPI for clone-video analysis.
FROM python:3.12-slim

# ffmpeg + ffprobe for keyframe + audio extraction.
# nodejs/npm to build & run the bgutil PO-token provider — yt-dlp
# talks to it via the matching plugin so we can download bot-protected
# YouTube videos from data-center IPs without cookies/proxy.
# git so we can clone bgutil source (it's not published on npm).
# python3-dev/build-essential because bgutil pulls `canvas` which
# compiles native bindings during npm install.
# tini reaps zombies cleanly when we run two processes via entrypoint.sh.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        nodejs \
        npm \
        git \
        tini \
        build-essential \
        python3-dev \
        libcairo2-dev \
        libpango1.0-dev \
        libjpeg-dev \
        libgif-dev \
        librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first for cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build the bgutil PO-token provider from source. Pinned to a known
# version (1.3.1) — bump as needed when YouTube changes signature.
ENV BGUTIL_VERSION=1.3.1
RUN git clone --branch ${BGUTIL_VERSION} --depth 1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
      /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci --no-audit --no-fund \
    && npx tsc

# Install the matching yt-dlp plugin so yt-dlp knows to talk to bgutil.
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider

# Install yt-jsc-youtubei — provides the JS Challenge Provider yt-dlp
# needs to unlock signed format URLs. Uses node (which we already
# installed for bgutil) under the hood. Without this, even with valid
# auth cookies + PO tokens, yt-dlp returns "Requested format is not
# available" because it can't solve the n-parameter signature
# challenge for any of the streaming format URLs.
RUN pip install --no-cache-dir 'git+https://github.com/alive4ever/yt-jsc-youtubei'

COPY server.py .
COPY cloud_run_json_exporter.py ./
COPY otel_init.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PORT=8080
# Internal port the bgutil HTTP token provider listens on; not exposed.
ENV PORT_BGUTIL=4416
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
