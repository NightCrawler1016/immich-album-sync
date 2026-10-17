<div align="center">

<img src="icon.png" alt="Immich Album Sync" width="150" height="150" />

<h1>Immich Album Sync</h1>

<p>
  <strong>One-way album sync between two <a href="https://immich.app">Immich</a> servers — with a web UI.</strong><br/>
  Built for Unraid, runs on any Docker host.
</p>

<p>
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" />
  <img src="https://img.shields.io/badge/Unraid-Community%20App-f15a2c" alt="Unraid Community App" />
  <img src="https://img.shields.io/badge/Docker-ghcr.io-2496ed?logo=docker&logoColor=white" alt="Docker image on GHCR" />
  <img src="https://img.shields.io/badge/powered%20by-immich--go-0ea5e9" alt="Powered by immich-go" />
</p>

</div>

```
Immich A (private)  ──────────────▶  Immich B (public/family)
    Master library                       Curated albums only
```

## Features

- 🖥️ **Web UI** — configure sync jobs, view status, stream live logs from the browser
- 🔐 **Password-protected** — username/password login; forced password change on first login
- 🔑 **Encrypted API keys** — all API keys are AES-encrypted at rest; never stored or rendered in plaintext
- 📅 **Cron scheduling** — configurable per-job schedule, runs automatically in the background
- 📸 **Preserves originals** — downloads and uploads raw files with EXIF and GPS intact
- 🍎 **Live Photo support** — automatically pairs `.HEIC` + `.MOV` files
- 🔁 **Duplicate-safe** — uses `immich-go` for smart duplicate detection on the destination
- ⚡ **Incremental & bandwidth-friendly** — checks the destination by checksum before downloading, so re-syncing an unchanged album transfers almost nothing
- ➕ **Add-only by default** — photos removed from the source album are left alone on the destination unless you opt in to *Mirror deletes*
- 🪞 **Optional Mirror deletes** — per-job, opt-in (experimental): keeps the destination album's contents matching the source album. Photos are only taken out of the album — never deleted from the destination server
- 📱 **Mobile-responsive** — works on phones, tablets, and desktops
- 🚀 **Multi-job** — sync multiple albums with different schedules and servers
- 🔍 **API permission checker** — test and verify required Immich API key permissions per job
- 🔔 **Webhook notifications** — get pinged on sync start/success/partial/failure; generic JSON that auto-formats for Discord & Slack, global or per-job
- 📜 **Run history** — browse every sync over the last 30 days (or any window) and filter by job and status to pin down failures
- 📦 **Support bundle** — one-click download of logs, sanitized config, and run history for troubleshooting

---

## Screenshots

<div align="center">

<a href="screenshots/1-dashboard.png"><img src="screenshots/1-dashboard.png" alt="Dashboard" width="900" /></a>

<sub><b>Dashboard</b> — job overview, status tiles, and recent run history</sub>

</div>

<br/>

<details>
<summary>🔁 <b>Sync Jobs</b> — manage all album sync rules at a glance</summary>
<br/>
<div align="center">
  <a href="screenshots/2-sync-jobs.png"><img src="screenshots/2-sync-jobs.png" alt="Sync Jobs" width="900" /></a>
</div>
</details>

<details>
<summary>✏️ <b>Edit Job</b> — servers, API keys, schedule &amp; behavior</summary>
<br/>
<div align="center">
  <a href="screenshots/3-edit-sync-jobs.png"><img src="screenshots/3-edit-sync-jobs.png" alt="Edit Job" width="700" /></a>
</div>
</details>

<details>
<summary>📜 <b>Live Logs</b> — real-time, color-coded sync activity stream</summary>
<br/>
<div align="center">
  <a href="screenshots/4-live-logs.png"><img src="screenshots/4-live-logs.png" alt="Live Logs" width="900" /></a>
</div>
</details>

<details>
<summary>⚙️ <b>Settings</b> — credentials, account, and app info</summary>
<br/>
<div align="center">
  <a href="screenshots/5-settings.png"><img src="screenshots/5-settings.png" alt="Settings" width="900" /></a>
</div>
</details>

---

## Quick Start (Unraid)

### Option A — Community Applications XML Template

1. In Unraid, go to **Apps → My Apps** (or add a template manually)
2. Use the XML template from this repository: `immich-album-sync.xml`
3. Set a unique `SECRET_KEY` (32–64 random characters — see [Security](#security))
4. Start the container and open `http://your-unraid-ip:8080`
5. Log in with `admin` / `admin` — you will be prompted to set a new password immediately

### Option B — Unraid Docker UI (manual)

| Setting | Value |
|---|---|
| Repository | `ghcr.io/nightcrawler1016/immich-album-sync:latest` |
| Name | `immich-album-sync` |
| Port | `8080` → `8080` |
| Path `/app/appdata` | `/mnt/user/appdata/immich-album-sync` |
| Variable `SECRET_KEY` | A 32–64 character random string (required) |
| Variable `TZ` | Your timezone (e.g. `America/New_York`) — log timestamps only; schedules always run in UTC |

### Option C — docker-compose

```yaml
services:
  immich-album-sync:
    image: ghcr.io/nightcrawler1016/immich-album-sync:latest
    container_name: immich-album-sync
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./appdata:/app/appdata
    environment:
      SECRET_KEY: "change-me-to-a-unique-random-32-64-char-string"  # replace — the container won't start with this placeholder
      TZ: "America/New_York"
```

Then open `http://localhost:8080` and log in with `admin` / `admin`.

> **First-login password change is required.** You will be redirected automatically on first login.

---

## Security

### First-login Password Change

On first login with the default credentials (`admin` / `admin`), the application immediately redirects to a password-change screen. You **cannot access any other page** until a new password is set. The default password cannot be reused.

### API Key Encryption

All Immich API keys entered in sync job forms are encrypted before being stored in the SQLite database using **AES-128 (Fernet)** with a key derived from your `SECRET_KEY`. Keys are:

- Never stored in plaintext (keys saved in plaintext by very old versions are encrypted automatically at startup)
- Never rendered in HTML (form fields always show empty; a "Key stored securely" indicator is shown instead)
- Decrypted only in memory, when needed — to run a sync, to test a connection, or to scrub the keys out of a support bundle
- The destination key is handed to `immich-go` through its process environment (`IMMICH_GO_UPLOAD_API_KEY`), never on the command line, so it does not appear in process listings such as `ps`; `immich-go` output is also scrubbed of the key before it is written to the log

If a stored key can no longer be decrypted (for example because `SECRET_KEY` changed), the sync fails with a clear "re-enter the API keys" message rather than sending garbage to Immich.

### SECRET_KEY Requirements

| Constraint | Value |
|---|---|
| Minimum length | 16 characters |
| **Recommended length** | **32–64 characters** |
| Maximum length | 128 characters (longer provides no additional benefit) |

> **The container refuses to start** if `SECRET_KEY` is unset, left at a placeholder value (anything beginning with `change-me`), or shorter than 16 characters. This is deliberate: the key signs session cookies **and** encrypts stored API keys, so a known/default value would let anyone forge an admin session and decrypt your keys. Set a real key before starting.

Generate a strong key: [1Password Generator](https://1password.com/password-generator/) — select 32–64 characters with all character types.

> **Important:** Changing `SECRET_KEY` after the initial setup will **invalidate all stored API keys and webhook URLs** (they were encrypted with the old key) and log out all active sessions. You will need to re-enter the API keys for every sync job, plus the global webhook URL (Settings → Notifications) and any per-job custom webhook URLs — until then, notifications silently stop.

### Session Security

Sessions are signed with `SECRET_KEY` using `itsdangerous` and expire after 24 hours of inactivity. Changing the password or username signs out all other sessions. The session cookie is `SameSite=Lax` but is **not** marked `Secure`, so it works on a plain-HTTP LAN; if you expose the app beyond your LAN, put it behind an HTTPS reverse proxy.

### Clickjacking Protection

Every response carries `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`, so the UI cannot be embedded in another page's iframe (and therefore cannot be clickjacked). This also means it cannot be embedded in dashboard iframes such as Organizr or Homarr — open it in its own tab instead.

### Runs as Non-Root

The app process runs as an unprivileged user (`PUID:PGID`, default `99:100` = Unraid's `nobody:users`), **not root**. The container uses root only briefly at startup to fix data-directory ownership, then drops privileges. If your appdata is owned by a different user and you see permission errors, set `PUID`/`PGID` to match (see [Troubleshooting](#common-issues)).

### Brute-Force & CSRF Protection

- **Login rate limiting** — repeated failed logins from an IP are throttled (10 failures within 15 minutes triggers a 5-minute lockout). The limiter is in memory and resets when the container restarts. **Behind a reverse proxy**, every client appears to come from the proxy's IP, so they all share one limit (and one lockout) — unless you set uvicorn's `FORWARDED_ALLOW_IPS` environment variable to your proxy's IP or subnet so the real client IP is taken from `X-Forwarded-For`. Never set it to `*` if port 8080 is also reachable directly, or anyone can spoof their IP.
- **CSRF protection** — state-changing requests are rejected unless their `Origin`/`Referer` matches the app's own `Host`, on top of a `SameSite=Lax` session cookie. **Behind a reverse proxy**, the proxy must forward the original `Host` header (nginx: `proxy_set_header Host $http_host;` — Apache: `ProxyPreserveHost On`), otherwise every form submission is rejected with `403 CSRF validation failed`.

### Safe Support Bundles

The downloadable support bundle is built to be safe to share for troubleshooting: API keys are removed, and server hostnames (including bare hostnames of your configured servers) and IPv4/IPv6 addresses are masked in `sync.log`, the job configs, and run error messages.

Webhook notifications likewise mask server URL hosts and IP addresses in the error text they send.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | ✅ Yes | _none — must set_ | 32–64 char random string for session signing and API key encryption. Container refuses to start without a real value. |
| `TZ` | No | `UTC` | Container timezone (e.g. `America/New_York`). Affects `sync.log` timestamps only — cron schedules always run in UTC, and the web UI shows times in UTC |
| `PUID` | No | `99` | User ID the app runs as (non-root). `99` = Unraid `nobody` |
| `PGID` | No | `100` | Group ID the app runs as. `100` = Unraid `users` |
| `CLEANUP_CACHE` | No | `false` | `true` turns on *Clean up cache after upload* for every job: uploaded files are deleted right after upload instead of at the start of the next run (see [Download cache](#download-cache)) |
| `CACHE_PATH` | No | `/app/appdata/cache` | Download cache directory (see [Cache on external storage](#cache-on-external-storage-or-a-different-disk)) |
| `BATCH_SIZE_MB` | No | `10240` | Max MB to stage before uploading a batch (0 = unlimited) |
| `BATCH_FILE_COUNT` | No | `0` | Max files per batch (0 = unlimited, size limit still applies) |
| `DB_PATH` | No | `/app/appdata/config.db` | SQLite database path |
| `LOG_PATH` | No | `/app/appdata/logs/sync.log` | Sync log file path. Rotates at 10 MB, keeping 3 old files (`sync.log.1` … `sync.log.3`) |
| `RUN_HISTORY_RETENTION_DAYS` | No | `90` | Days of run history to keep; older runs are pruned daily (the last 10 runs per job are always kept). `0` = keep forever |

---

## Volume Mount

| Container path | Purpose |
|---|---|
| `/app/appdata` | All persistent data: database, cache, and logs |

Map this to a path on your Unraid array, e.g. `/mnt/user/appdata/immich-album-sync`.

### Cache on External Storage or a Different Disk

For large first-time syncs (hundreds of GB or multi-TB albums), you may want the download cache on a high-capacity array disk, a separate SSD pool, or a network share — rather than your primary Unraid cache pool.

**Unraid / Docker setup:**

1. Add a **second Path mapping** in the Docker template:
   - Container path: `/app/cache`
   - Host path: `/mnt/user/YourLargeDisk/immich-sync-cache` *(or any writable path)*
2. Set the `CACHE_PATH` environment variable to: `/app/cache`

The sync engine will write all downloaded originals to that path. The main `/app/appdata` volume (database, logs) is unaffected.

**docker-compose example with separate cache volume:**

```yaml
services:
  immich-album-sync:
    image: ghcr.io/nightcrawler1016/immich-album-sync:latest
    container_name: immich-album-sync
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./appdata:/app/appdata                     # database + logs (small, keep on fast drive)
      - /mnt/big-drive/sync-cache:/app/cache       # large cache on a different disk or share
    environment:
      SECRET_KEY: "change-me-to-a-unique-random-32-64-char-string"  # replace — the container won't start with this placeholder
      TZ: "America/New_York"
      CACHE_PATH: /app/cache
```

---

## Web UI Pages

| Page | URL | Description |
|---|---|---|
| Dashboard | `/` | Overview of all jobs, recent runs, and status |
| Sync Jobs | `/jobs` | List, create, edit, delete, and pause sync jobs |
| New Job | `/jobs/new` | Configure source server, destination server, album, and schedule |
| Live Logs | `/logs` | Real-time streaming sync log with copy and support bundle download |
| Run History | `/history` | Every run over a selectable window (default 30 days), filterable by job and status |
| Settings | `/settings` | Change username and password; configure notifications |

---

## Setting Up a Sync Job

1. Open **Sync Jobs → New Job**
2. Enter the **Source Server** (your private Immich) URL and API key
3. Enter the **Destination Server** (your family/public Immich) URL and API key
4. Use the **Test Connection** button to verify API key permissions before saving
5. Select the source album name (populated from the test result)
6. Set a cron schedule (e.g. `0 23 * * *` for 23:00 **UTC** daily) — see the schedule notes below
7. Optionally enable **Clean up cache after upload** and/or **Mirror deletes** (see [How It Works](#how-it-works))
8. Save — the job will run on schedule automatically. An invalid cron expression is rejected when you save

### Schedule notes

- Schedules are standard 5-field cron (`minute hour day month weekday`) and **always run in UTC**, regardless of the `TZ` setting (which only affects log timestamps). Convert your local time to UTC when choosing the hour.
- **Weekday numbers count from Monday:** `0` = Monday … `6` = Sunday — unlike standard cron, where `0` = Sunday. Use names to avoid surprises: `0 3 * * mon-fri`, `0 3 * * sun`.

### Required API Key Permissions

| Server | Required Permissions |
|---|---|
| Source (Immich A) | `album.read`, `asset.read`, `asset.download` |
| Destination (Immich B) | **All permissions** (full-access key — see note below). *Mirror deletes* additionally uses `albumAsset.delete`, which an all-permissions key already includes |

> **Source note:** `asset.download` is a separate scope from `asset.read` — it is what actually permits downloading original files. A key with only `album.read` + `asset.read` passes metadata checks but fails every file download with `403 Forbidden`. `user.read` is **not** required on the source.

> **Destination note:** Uploads run through [immich-go](https://github.com/simulot/immich-go), which requires a broad set of scopes and validates the connection via `GET /api/users/me` (so `user.read` is mandatory here). Its documented requirements include `user.read`, `asset.read`, `asset.statistics`, `asset.update`, `asset.upload`, `asset.copy`, `asset.replace`, `asset.delete`, `asset.download`, `album.create`, `album.read`, `albumAsset.create`, `server.about`, `stack.create`, `tag.asset`, and `tag.create`. Because this set changes between immich-go versions, the simplest reliable choice is an **all-permissions API key** on the destination. A narrowly-scoped key fails with `Missing required permission: …`. For family sharing, create a dedicated non-admin user on the destination and use *their* all-permissions key. If you enable *Mirror deletes*, the app also removes assets from the destination album itself, which needs `albumAsset.delete` (included in an all-permissions key).

See [Immich API Key documentation](https://docs.immich.app/features/command-line-interface/#obtain-the-api-key) for how to create keys with specific permissions.

---

## Notifications

Get notified when a sync runs — configured entirely in the browser under **Settings → Notifications**. No environment variables needed.

### How it works

- **One webhook URL.** Paste any URL that accepts a JSON `POST`. If it's a **Discord** or **Slack** incoming-webhook URL, the message is auto-formatted (Discord embeds with a color per status; Slack attachments). Any other URL receives a plain JSON body you can route however you like.
- **Pick your events.** Choose any of **Run started**, **Successful runs**, **Partial (some errors)**, and **Failed runs**. A common low-noise choice is just *Failed* + *Partial*.
- **Global default + per-job override.** The Settings page sets the default for every job. On a job's edit page you can leave it on **Use global settings**, turn it **Off** for that job, or set a **Custom** webhook/events just for that job.
- **Test button.** "Send test notification" posts a sample message so you can confirm the URL works before relying on it.

### Security & privacy

- Webhook URLs are **encrypted at rest** (same Fernet scheme as API keys) and are never rendered back into the page — the field shows "Saved securely" and stays blank; leave it blank to keep the current URL.
- Notification payloads **never contain secrets** — no API keys, no server URLs (only the job name, album names, run counts, status, timestamps, and, for failed/partial runs, the error message — with server URL hosts and IP addresses masked).
- A notification failure can never break a sync — the worst case is a warning in the log.

### Generic JSON payload

Non-Discord/Slack endpoints receive:

```json
{
  "service": "immich-album-sync",
  "event": "failed",
  "status": "failed",
  "job": "Family Shared Sync",
  "source_album": "Family Shared",
  "dest_album": "Family Shared",
  "message": "Family Shared Sync: Sync failed",
  "counts": { "found": 12, "downloaded": 0, "uploaded": 0, "skipped": 0, "failed": 12 },
  "error": "Album 'Family Shared' not found on the source server.",
  "started_at": "2026-07-04T02:00:00+00:00",
  "finished_at": "2026-07-04T02:00:07+00:00",
  "duration_seconds": 7,
  "timestamp": "2026-07-04T02:00:07+00:00"
}
```

---

## Batch Processing

The sync engine automatically processes large albums in rolling batches to prevent the local cache from filling up your disk.

### How batching works

Instead of downloading the entire album before uploading anything, the engine:

1. Downloads files until either `BATCH_SIZE_MB` or `BATCH_FILE_COUNT` is reached
2. Uploads that batch to the destination server
3. Clears the batch from the cache
4. Repeats until all files are processed

The **final batch** respects the job's *Clean up cache after upload* setting (or `CLEANUP_CACHE`) — intermediate batches are always cleared. See [Download cache](#download-cache).

### Defaults

| Setting | Default | Meaning |
|---|---|---|
| `BATCH_SIZE_MB` | `10240` (10 GB) | Flush every 10 GB of downloaded data |
| `BATCH_FILE_COUNT` | `0` | No file-count limit (size limit still applies) |

With the default 10 GB limit, syncs under 10 GB behave exactly as before (single batch). Syncs over 10 GB are automatically split — you'll see progress in the live log like:

```
─── Batch 1: 247 files (9.98 GB) — 831 items remaining ───
   Batch 1: 212 uploaded to destination
   Batch 1: cache cleared, ready for next batch
─── Batch 2: 231 files (10.01 GB) — 600 items remaining ───
...
```

### Tuning for your setup

| Scenario | Recommendation |
|---|---|
| Unraid cache pool is small (< 50 GB) | Lower `BATCH_SIZE_MB` to `5120` (5 GB) or `2048` (2 GB) |
| Cache on large array disk or NAS share | Raise `BATCH_SIZE_MB` or set to `0` (disable) |
| Very large files (4K video, RAW) | Lower `BATCH_FILE_COUNT` to `100–250` |
| Want to disable batching entirely | Set both `BATCH_SIZE_MB=0` and `BATCH_FILE_COUNT=0` |

---

## How It Works

1. At the scheduled time, the sync engine connects to **Immich A** via its REST API
2. Finds the configured source album by name and lists all assets (including Live Photo `.MOV` companions)
3. **Checksum pre-check** — asks **Immich B** which of those assets it already has (by SHA-1). Assets already present are added straight to the destination album via the API, with **no download or upload**; only genuinely-new assets continue to the next step
4. Prunes the job's download cache to just the files that still need uploading (see [Download cache](#download-cache))
5. Downloads the new originals to the local cache in rolling batches — once a batch reaches `BATCH_SIZE_MB` (default 10 GB) or `BATCH_FILE_COUNT`, it is immediately uploaded and cleared before the next batch begins
6. Uploads each batch to **Immich B** using [`immich-go`](https://github.com/simulot/immich-go) (v0.32.0), which performs a second layer of duplicate detection on the destination
7. If the job has **Mirror deletes** enabled, removes from the destination album anything that is no longer in the source album (see [Mirror deletes](#mirror-deletes))
8. Logs all activity to `/app/appdata/logs/sync.log` (rotated at 10 MB, 3 old files kept), viewable live in the browser

Re-syncing an unchanged album is nearly free: the checksum pre-check skips every already-synced asset before any bytes are transferred, so there is no wasted bandwidth or disk. The pre-check is best-effort — if it ever fails, the engine falls back to downloading everything and lets `immich-go` de-dupe on upload, so a failed check never causes an asset to be skipped.

**Server B is a replica, not a primary library.** It's meant to mirror whatever albums you sync to it and can be wiped and rebuilt at any time — just re-run your jobs. So the sync never restores anything from B's trash: a photo you trash on B counts as already present until B's trash is emptied, and the next sync after that uploads it fresh. If you remove a photo from A's album and later add it back, the next sync puts it back in B's album like any other new photo.

A run finishes as **success**, **partial** (some files failed to download or upload, or Mirror deletes failed — whatever did transfer is kept), or **failed** (the run could not proceed at all, e.g. album not found or a bad API key).

### Download cache

- Each asset is cached in its own folder (`<CACHE_PATH>/job_<id>/files/<asset-id>/<filename>`), so two different photos that share a filename never overwrite each other. A Live Photo's still and motion file share one folder.
- Downloads are written to a temporary `.part` file and renamed only once complete, so an interrupted download never leaves a truncated file behind.
- At the start of every run, the job's cache is pruned to only the files that still need uploading. Files left over from an interrupted run are reused without re-downloading; anything else (already on the destination, or since removed from the source album) is deleted, so stale cached files are never uploaded again.
- **Clean up cache after upload** (per job, or `CLEANUP_CACHE=true` for all jobs) only changes *when* uploaded files are deleted: right after a successful upload when on, or at the start of the next run when off. Files already on the destination are never re-downloaded either way.
- Deleting a job also deletes its cache folder (deleting is refused while that job is syncing).

### Mirror deletes

By default the sync is **add-only**: removing a photo from the source album does not touch the destination. Enabling **Mirror deletes** on a job (opt-in, marked experimental) makes the destination album match the source album:

- After uploading, every asset in the destination album that doesn't match an asset in the source album is **removed from the destination album**. That includes photos removed from the source album *and* photos someone added directly to the destination album.
- An asset matches when its checksum is identical, **or** it has the same filename and a capture time within 5 seconds — the same rule `immich-go` uses to decide the destination already has a photo (e.g. a different-quality copy), so the two never undo each other.
- It changes **album membership only** — nothing is trashed or deleted; the photos remain in the destination owner's library. Every removal is logged by filename and asset id, so it can be undone by re-adding those assets to the album.
- **Upgrading:** in older versions this checkbox did nothing. To avoid surprise removals, the first start of this version switches it **off** on every existing job (logged as a warning) — tick it again on the jobs you want mirrored.
- The destination album is matched by its **exact** name (case-sensitive), the same way `immich-go` picks the album to upload into.
- **Safety guards:** it skips itself (with a warning in the log) if the source album listing is empty or lacks checksums, or if it would remove *every* asset in the destination album — a sign that something is wrong with a listing. As a consequence, emptying the source album completely will not empty the destination album; remove those photos by hand.
- It needs the `albumAsset.delete` scope on the destination key (included in an all-permissions key). If the removal fails, the run is marked **partial**.

### Immich Server Compatibility

Works with **Immich v1/v2 and v3**. Immich v3.0.0 removed the embedded `assets` array from album responses ([v3 migration guide](https://immich.app/blog/v3-migration)); the sync engine detects this automatically and lists album contents via the paginated search API instead, with no configuration needed. Uploads use immich-go v0.32.0, which auto-detects the server version.

---

## Docker Image Tags

| Tag | Branch | Description |
|---|---|---|
| `latest` | `main` | Stable production release |
| `dev` | `dev` | Development builds — may be unstable |
| `1.2.3`, `1.2`, `1` | git tag `v1.2.3` | Pinned version releases — note the image tags have **no** leading `v` |

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
  -e SECRET_KEY=my-local-dev-secret-32-chars-long \
  -v $(pwd)/appdata:/app/appdata \
  immich-album-sync:local
```

---

## Troubleshooting

### Download Support Bundle

In the **Live Logs** page, click **Download Support Bundle** to get a ZIP containing:
- `sync.log` — the current sync log (API keys removed; server hostnames and IPv4/IPv6 addresses masked)
- `jobs.json` — job configurations (API keys redacted; URL hosts masked)
- `sync_runs.json` — last 100 run records (error messages redacted the same way)
- `system_info.json` — Python version, immich-go version, paths

> The bundle is safe to share for troubleshooting: API keys are removed, and server hostnames (including bare hostnames of your configured servers) and IP addresses are masked in the log, job configs, and run error messages.

### Common Issues

| Symptom | Likely Cause | Fix |
|---|---|---|
| Container starts but no logs | Wrong volume mapping | Ensure `/app/appdata` is mapped to a writable host path |
| Permission errors writing to appdata | App runs as non-root `PUID:PGID` (default `99:100`) but your appdata is owned by another user | Set `PUID`/`PGID` to match your appdata owner, or `chown` the appdata path to `99:100` (Unraid `nobody:users`) |
| Redirected to "Set Your Password" on login | First-login prompt (by design) | Set a new password — you cannot skip this step |
| "Invalid username or password" | Wrong credentials | Default is `admin` / `admin`; check Settings if you changed it |
| API key test fails | Insufficient permissions | Use Immich's API key settings to grant required roles (see table above) |
| Container won't start / exits immediately, log says "Refusing to start" | `SECRET_KEY` is unset, a `change-me…` placeholder, or under 16 chars | Set a unique 32–64 character `SECRET_KEY` and restart |
| Sync runs but 0 uploads | Duplicates already on dest | Normal — `immich-go` skips files already present |
| `Assets : 0 in album` after upgrading Immich to v3 | Older image predates the Immich v3 API changes | Update to the latest image — album listing and uploads are v3-compatible now |
| Album not visible after sync | Immich UI cache | Refresh your Immich browser tab or wait a moment |
| Cache fills up during large sync | Batch size too large for disk | Lower `BATCH_SIZE_MB` (e.g. `2048` for 2 GB batches) or route cache to a larger disk |
| Sync stops mid-way with "partial" status | Upload error during a batch, some files failed to download, or Mirror deletes failed | Check the live log for the cause. Cached files still waiting to upload are kept — fix the error and re-run; completed batches won't re-upload |
| Destination album has more photos than the source | The sync is add-only by default: photos removed from the source album, and photos added directly to the destination album, stay there | Enable **Mirror deletes** on the job (see [Mirror deletes](#mirror-deletes)) — it removes them from the destination album only, never from the server |
| `403` "CSRF validation failed" when saving forms behind a reverse proxy | The proxy is not forwarding the original `Host` header | nginx: `proxy_set_header Host $http_host;` — Apache: `ProxyPreserveHost On` |
| "Sync failed: a stored API key could not be decrypted" | `SECRET_KEY` changed since the keys were saved | Edit the job and re-enter the source and destination API keys (or restore the old `SECRET_KEY`) |
| "Invalid schedule" when saving a job, or when re-enabling a paused job | The cron expression isn't valid | Fix the 5-field cron expression (see [Schedule notes](#schedule-notes)) |
| Job runs at the wrong time of day | Schedules run in UTC, not your `TZ` | Convert the hour to UTC; use weekday names (`mon-fri`) rather than numbers (`0` = Monday here) |
| Want to use a network share for cache | Default cache is on main appdata | Add a second Docker volume mapping and set `CACHE_PATH` (see [Cache on external storage](#cache-on-external-storage-or-a-different-disk)) |

---

## Notes on immich-go

This container pins [`immich-go`](https://github.com/simulot/immich-go) to **v0.32.0** for stability. The upload command used internally:

```bash
IMMICH_GO_UPLOAD_API_KEY=DEST_KEY \
immich-go upload from-folder \
  --server DEST_URL \
  --into-album "Album Name" \
  --recursive /path/to/cache
```

The API key is supplied through the `IMMICH_GO_UPLOAD_API_KEY` environment variable of the `immich-go` process rather than `--api-key`, so it never appears on the command line.

> Note: flags must appear **after** `from-folder` (v0.31.0+ syntax). v0.32.0 adds **Immich v3 server compatibility** — it auto-detects the server version and remains backward compatible with Immich v2. (v0.31.0 uploads fail against Immich v3 with HTTP 400 — [immich-go#1372](https://github.com/simulot/immich-go/issues/1372).)

---

## Credits & Acknowledgements

This project is built on top of several excellent open-source tools — credit goes to their authors:

| Project | Author | Role in this project |
|---|---|---|
| [immich-go](https://github.com/simulot/immich-go) | [simulot](https://github.com/simulot) | Upload engine — handles duplicate detection and album creation on the destination server |
| [Immich](https://github.com/immich-app/immich) | [Immich Team](https://github.com/immich-app) | The self-hosted photo platform this tool syncs between |
| [FastAPI](https://fastapi.tiangolo.com) | Sebastián Ramírez | Web framework powering the UI and REST API |
| [APScheduler](https://github.com/agronholm/apscheduler) | Alex Grönholm | Cron-based scheduling for automated sync jobs |
| [SQLAlchemy](https://www.sqlalchemy.org) | Mike Bayer | ORM for SQLite job/run storage |
| [Tailwind CSS](https://tailwindcss.com) | Tailwind Labs | UI styling |
| [httpx](https://www.python-httpx.org) | Encode | Async HTTP client used to talk to Immich APIs |

A big thank you to the **immich-go** project in particular — it does the heavy lifting of uploading files with smart duplicate detection so this tool doesn't have to reinvent that wheel.

---

## License

MIT
