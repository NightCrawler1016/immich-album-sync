FROM python:3.12-slim

LABEL org.opencontainers.image.title="Immich Album Sync"
LABEL org.opencontainers.image.description="One-way album sync between two Immich servers with Web UI"
LABEL org.opencontainers.image.source="https://github.com/NightCrawler1016/immich-album-sync"
LABEL org.opencontainers.image.licenses="MIT"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    ca-certificates \
    tar \
    && rm -rf /var/lib/apt/lists/*

# Install immich-go binary.
# Use uname -m to detect the real running architecture — reliable under
# both native builds and QEMU cross-compilation (where ARG TARGETARCH
# can silently stay at its default value instead of being overridden).
RUN set -ex && \
    ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)  BIN_ARCH="x86_64" ;; \
        aarch64) BIN_ARCH="arm64" ;; \
        *)       BIN_ARCH="x86_64" ;; \
    esac && \
    LATEST_TAG=$(curl -fsSL "https://api.github.com/repos/simulot/immich-go/releases/latest" \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4) && \
    echo "Installing immich-go ${LATEST_TAG} for arch=${ARCH} (${BIN_ARCH})" && \
    curl -fsSL "https://github.com/simulot/immich-go/releases/download/${LATEST_TAG}/immich-go_Linux_${BIN_ARCH}.tar.gz" \
        -o /tmp/immich-go.tar.gz && \
    mkdir -p /tmp/immich-go-extract && \
    tar -xzf /tmp/immich-go.tar.gz -C /tmp/immich-go-extract && \
    find /tmp/immich-go-extract -name "immich-go" -type f | head -1 | \
        xargs -I{} install -m 755 {} /usr/local/bin/immich-go && \
    rm -rf /tmp/immich-go.tar.gz /tmp/immich-go-extract && \
    immich-go --version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application and entrypoint
COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Pre-create appdata directories in the image as a fallback.
# These are overridden when Unraid mounts the appdata volume.
RUN mkdir -p /app/appdata/cache /app/appdata/logs

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
