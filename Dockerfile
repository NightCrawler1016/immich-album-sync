FROM python:3.12-slim

LABEL org.opencontainers.image.title="Immich Album Sync"
LABEL org.opencontainers.image.description="One-way album sync between two Immich servers with Web UI"
LABEL org.opencontainers.image.source="https://github.com/NightCrawler1016/immich-album-sync"
LABEL org.opencontainers.image.licenses="MIT"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    tar \
    && rm -rf /var/lib/apt/lists/*

# Install immich-go binary (pinned to a tested version to avoid breaking CLI changes).
# Use uname -m to detect the real running architecture — reliable under
# both native builds and QEMU cross-compilation (where ARG TARGETARCH
# can silently stay at its default value instead of being overridden).
# Syntax (unchanged since v0.31): immich-go upload from-folder --into-album NAME --recursive DIR
# v0.32.0 adds Immich v3 server compatibility (auto-detects server version;
# v0.31.0 uploads fail against Immich v3 with HTTP 400 — simulot/immich-go#1372)
# while remaining backward compatible with Immich v2.
# Downloads are verified against SHA-256s pinned from the release's checksums
# file — bumping a version means updating both per-arch hashes too.
ENV IMMICH_GO_VERSION=v0.32.0
RUN set -ex && \
    ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)  BIN_ARCH="x86_64"; SHA256="6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97" ;; \
        aarch64) BIN_ARCH="arm64";  SHA256="2c35d9284baae407ef9540bdac5f488971b0bdc7be758a4d7c05ab270af09fdb" ;; \
        *)       echo "unsupported architecture: $ARCH" >&2; exit 1 ;; \
    esac && \
    echo "Installing immich-go ${IMMICH_GO_VERSION} for arch=${ARCH} (${BIN_ARCH})" && \
    curl -fsSL "https://github.com/simulot/immich-go/releases/download/${IMMICH_GO_VERSION}/immich-go_Linux_${BIN_ARCH}.tar.gz" \
        -o /tmp/immich-go.tar.gz && \
    echo "${SHA256}  /tmp/immich-go.tar.gz" | sha256sum -c - && \
    mkdir -p /tmp/immich-go-extract && \
    tar -xzf /tmp/immich-go.tar.gz -C /tmp/immich-go-extract && \
    find /tmp/immich-go-extract -name "immich-go" -type f | head -1 | \
        xargs -I{} install -m 755 {} /usr/local/bin/immich-go && \
    rm -rf /tmp/immich-go.tar.gz /tmp/immich-go-extract && \
    immich-go --version

# Install gosu for safe privilege de-escalation in the entrypoint.
# The container starts as root only long enough to fix data-dir ownership,
# then drops to an unprivileged PUID:PGID for the actual app process.
ENV GOSU_VERSION=1.17
RUN set -ex && \
    ARCH=$(uname -m) && \
    case "$ARCH" in \
        x86_64)  GOSU_ARCH="amd64"; GOSU_SHA256="bbc4136d03ab138b1ad66fa4fc051bafc6cc7ffae632b069a53657279a450de3" ;; \
        aarch64) GOSU_ARCH="arm64"; GOSU_SHA256="c3805a85d17f4454c23d7059bcb97e1ec1af272b90126e79ed002342de08389b" ;; \
        *)       echo "unsupported architecture: $ARCH" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/tianon/gosu/releases/download/${GOSU_VERSION}/gosu-${GOSU_ARCH}" \
        -o /usr/local/bin/gosu && \
    echo "${GOSU_SHA256}  /usr/local/bin/gosu" | sha256sum -c - && \
    chmod +x /usr/local/bin/gosu && \
    gosu --version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application and entrypoint
COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Fail the build on any syntax error, broken import, or dependency
# incompatibility inside the real image (CI's tests/smoke_test.py runs outside it).
RUN python -m compileall -q app && python -c "import app.main"

# Pre-create appdata directories in the image as a fallback.
# These are overridden when Unraid mounts the appdata volume.
RUN mkdir -p /app/appdata/cache /app/appdata/logs

# App version, injected at build time (CI passes the git tag, e.g. 1.2.3).
# Placed late so it only busts the final layers, not the dependency layers.
ARG APP_VERSION=1.0.0
ENV APP_VERSION=${APP_VERSION}

EXPOSE 8080

# Works with or without the optional native HTTPS (self-signed certs accepted).
# HTTP first: a TLS probe of the plain-HTTP listener logs a uvicorn warning every
# run, while plain HTTP sent to the TLS listener fails silently.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fs http://localhost:8080/health || curl -fsk https://localhost:8080/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
