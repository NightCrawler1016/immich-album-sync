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

# Install immich-go binary
# TARGETARCH is automatically set by Docker Buildx (amd64 / arm64).
# Falls back to amd64 for plain `docker build` without --platform.
ARG TARGETARCH=amd64
RUN set -ex && \
    case "${TARGETARCH}" in \
        amd64)  BIN_ARCH="x86_64" ;; \
        arm64)  BIN_ARCH="arm64" ;; \
        *)      BIN_ARCH="x86_64" ;; \
    esac && \
    # Fetch the latest release tag from GitHub API (e.g. "v0.23.1")
    LATEST_TAG=$(curl -fsSL "https://api.github.com/repos/simulot/immich-go/releases/latest" \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4) && \
    echo "Installing immich-go ${LATEST_TAG} for ${BIN_ARCH}" && \
    curl -fsSL "https://github.com/simulot/immich-go/releases/download/${LATEST_TAG}/immich-go_Linux_${BIN_ARCH}.tar.gz" \
        -o /tmp/immich-go.tar.gz && \
    mkdir -p /tmp/immich-go-extract && \
    tar -xzf /tmp/immich-go.tar.gz -C /tmp/immich-go-extract && \
    # Find the binary regardless of directory structure inside the tar
    find /tmp/immich-go-extract -name "immich-go" -type f | head -1 | \
        xargs -I{} install -m 755 {} /usr/local/bin/immich-go && \
    rm -rf /tmp/immich-go.tar.gz /tmp/immich-go-extract && \
    # Verify — this will fail the build if the binary is missing
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
