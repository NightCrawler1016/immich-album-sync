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

# Copy application
COPY app/ ./app/

# Create default appdata directories (will be overridden by volume mount)
# Note: running as root for Unraid compatibility — Unraid manages its own
# security model and mounts host directories as root-owned.
RUN mkdir -p /app/appdata/cache /app/appdata/logs

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
