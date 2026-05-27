# Immich Album Sync

A Docker container with a **web UI** for performing one-way album syncs between two [Immich](https://immich.app) servers. Built for Unraid but works on any Docker host.

```
Immich A (private)  ──────────────▶  Immich B (public/family)
    Master library                       Curated albums only
```

## Features

- 🖥️ **Web UI** — configure sync jobs, view status, stream live logs
- 🔐 **Password-protected** — username/password login
- 📅 **Cron scheduling** — configurable per-job, runs automatically
- 📸 **Preserves originals** — downloads and uploads raw files with EXIF intact
- 🍎 **Live Photo support** — pairs `.HEIC` + `.MOV` files automatically
- 🔁 **Duplicate-safe** — uses `immich-go` for smart duplicate detection
- 📱 **Mobile-responsive** — works on phones, tablets, and desktops
- 🚀 **Multi-job** — sync multiple albums with different schedules

---

## Quick Start (Unraid)

### 1. Using Unraid's Docker UI (Community Applications)

Add a custom container with these settings:

| Setting | Value |
|---|---|
| Repository | `ghcr.io/nightcrawler1016/immich-album-sync:latest` |
| Name | `immich-album-sync` |
| Port | `8080` → `8080` |
| Path | `/app/appdata` → `/mnt/user/appdata/immich-album-sync` |
| Variable: `SECRET_KEY` | A long random string (required) |
| Variable: `TZ` | Your timezone (e.g. `America/New_York`) |

### 2. Using docker-compose

```bash
# Clone or download the repo
git clone https://github.com/NightCrawler1016/immich-album-sync.git
cd immich-album-sync

# Edit the SECRET_KEY in docker-compose.yml first!
docker compose up -d
```

Then open **http://your-unraid-ip:8080** and log in with `admin` / `admin`.

> ⚠️ **Change the default password immediately** after first login (Settings page).

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | ✅ Yes | `change-me` | Secret for session cookies — use a long random string |
| `TZ` | No | `UTC` | Container timezone |
| `CLEANUP_CACHE` | No | `false` | Delete cached files after upload (`true`/`false`) |
| `DB_PATH` | No | `/app/appdata/config.db` | SQLite database path |
| `LOG_PATH` | No | `/app/appdata/logs/sync.log` | Sync log file path |
| `CACHE_PATH` | No | `/app/appdata/cache` | Download cache directory |

---

## Volume Mount

| Container path | Purpose |
|---|---|
| `/app/appdata` | All persistent data: database, cache, logs |

Map this to a path on your Unraid array, e.g. `/mnt/user/appdata/immich-album-sync`.

---

## Web UI Pages

| Page | URL | Description |
|---|---|---|
| Dashboard | `/` | Overview of all jobs, recent runs, status |
| Sync Jobs | `/jobs` | List, create, edit, delete, pause sync jobs |
| New Job | `/jobs/new` | Configure source server, dest server, album, schedule |
| Live Logs | `/logs` | Real-time streaming sync log |
| Settings | `/settings` | Change username and password |

---

## How It Works

1. At the scheduled time, the sync engine connects to **Immich A** via its API
2. Finds the configured album by name
3. Downloads all original files (including Live Photo `.MOV` companions) to the local cache
4. Uploads to **Immich B** using `immich-go`, which handles duplicate detection
5. Logs all activity to `/app/appdata/logs/sync.log`

Files already in the cache are skipped on re-download. `immich-go` skips files already on the destination.

---

## Docker Image Tags

| Tag | Branch | Description |
|---|---|---|
| `latest` | `main` | Stable production release |
| `dev` | `dev` | Development builds — may be unstable |
| `v1.2.3` | git tag | Pinned version releases |

```bash
# Pull latest stable
docker pull ghcr.io/nightcrawler1016/immich-album-sync:latest

# Pull dev build
docker pull ghcr.io/nightcrawler1016/immich-album-sync:dev
```

---

## Building Locally

```bash
git clone https://github.com/NightCrawler1016/immich-album-sync.git
cd immich-album-sync
docker build -t immich-album-sync:local .
docker run -p 8080:8080 \
  -e SECRET_KEY=my-local-dev-secret \
  -v $(pwd)/appdata:/app/appdata \
  immich-album-sync:local
```

---

## Notes on immich-go

This container uses [immich-go](https://github.com/simulot/immich-go) v0.22.x for uploads.
The upload command used internally:

```bash
immich-go --server DEST_URL --api-key DEST_KEY upload from-folder \
  --album "Album Name" --recursive /path/to/cache
```

If you encounter issues with a newer version of immich-go, check the
[immich-go releases page](https://github.com/simulot/immich-go/releases) for breaking changes
and open an issue in this repository.

---

## License

MIT
