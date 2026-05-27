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
ARG IMMICH_GO_VERSION=0.22.2
ARG TARGETARCH=amd64
RUN ARCH="${TARGETARCH}" && \
    if [ "${ARCH}" = "amd64" ]; then ARCH_NAME="x86_64"; \
    elif [ "${ARCH}" = "arm64" ]; then ARCH_NAME="arm64"; \
    else ARCH_NAME="x86_64"; fi && \
    curl -fsSL "https://github.com/simulot/immich-go/releases/download/${IMMICH_GO_VERSION}/immich-go_Linux_${ARCH_NAME}.tar.gz" \
        -o /tmp/immich-go.tar.gz && \
    tar -xzf /tmp/immich-go.tar.gz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/immich-go && \
    rm /tmp/immich-go.tar.gz && \
    immich-go --help > /dev/null 2>&1 || true

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
