# Immich Album Sync — Claude Code working notes

One-way album sync between two [Immich](https://immich.app) servers, with a web UI.
Private/master Immich A → curated/family Immich B. Photos are pulled from A via the
Immich REST API and pushed to B with the [`immich-go`](https://github.com/simulot/immich-go)
CLI (pinned **v0.31.0**), which does the duplicate detection and album creation on the
destination.

- **Public project.** Published on GitHub as
  [`NightCrawler1016/immich-album-sync`](https://github.com/NightCrawler1016/immich-album-sync)
  and listed on **Unraid Community Applications**. Treat everything here as
  externally visible — READMEs, security claims, the Unraid template, and image tags
  are all consumed by strangers. Don't overpromise in docs; if a security control is
  claimed, the code must actually implement it.
- **Single operator.** One admin user, password login. Typically runs on an Unraid box
  on a home LAN, sometimes behind a reverse proxy.
- **Not** the LaunchPad project. This repo is a completely separate app that happens to
  be open in the same session as an additional working directory.

---

## Two-computer / OneDrive workflow (read this first)

This working tree lives inside a **OneDrive-synced folder** and is edited from **two
different computers** (at least one of them Windows). That has one big consequence:

- **Line endings must stay LF.** A Windows-side edit once rewrote every text file to
  CRLF, which produced a phantom "everything changed" diff and — more seriously — would
  break `entrypoint.sh` inside the Linux container (the shell chokes on `\r` after the
  shebang). This is now pinned by:
  - `.gitattributes` — `* text=auto eol=lf` plus explicit `eol=lf` for `.py/.sh/.html/…`
    and `binary` for images. Git checks out LF on **both** machines.
  - `.editorconfig` — editors on both machines write LF, UTF-8, final newline.
- If you ever see the whole tree show as modified again, it's almost certainly line
  endings, not real edits. Confirm with `git diff --ignore-cr-at-eol` (empty output =
  it's purely CRLF) and renormalize with
  `git diff --name-only -z | xargs -0 perl -pi -e 's/\r\n/\n/g'` rather than assuming
  work was lost.
- `.claude/settings.local.json` is machine-local and git-ignored — don't commit it.
- **Don't** `git restore .` to "fix" a whitespace diff without checking first; if there
  ever *is* real uncommitted work from the other computer, that would throw it away.

---

## Architecture

Python web app: **FastAPI + uvicorn**, **Jinja2** server-rendered templates (Tailwind
loaded from the `cdn.tailwindcss.com` **CDN** in `base.html` — note the browser needs
internet access, and that CDN is officially "not for production"), **SQLAlchemy** ORM on
**SQLite**, **APScheduler** for cron,
**starlette SessionMiddleware** signed-cookie sessions, **bcrypt** password hashing,
**Fernet** (from `cryptography`) for API-key encryption, **httpx** for Immich API calls.
No client-side build step — templates are shipped as-is.

```
app/
  main.py          # FastAPI app + EVERY route (~1060 lines). Auth, CSRF middleware,
                   # jobs CRUD, log SSE stream, support bundle, settings, test-connection.
  sync.py          # run_sync_job(): the whole sync flow + immich-go subprocess wrapper.
  immich_client.py # ImmichClient: async httpx wrapper for the Immich REST API.
  crypto.py        # Fernet encrypt/decrypt of secrets, key derived from SECRET_KEY.
  database.py      # SQLAlchemy engine/session, init_db() seeds default admin.
  models.py        # SyncJob, SyncRun, Settings (key/value) tables.
  notify.py        # Webhook notifications: resolve config, build/format payload, send.
  scheduler.py     # APScheduler wrapper: schedule_job / remove_job / runner.
  templates/*.html # base, login, change_password, dashboard, jobs, job_form, logs, settings
  static/icon.png  # served unauthenticated at /static (login page needs it)
Dockerfile         # python:3.12-slim; installs immich-go + gosu; runs entrypoint.sh
entrypoint.sh      # root → fix ownership → gosu drop to PUID:PGID → uvicorn
docker-compose.yml # local/example compose
immich-album-sync.xml  # Unraid Community Applications template
ca_profile.xml     # Unraid CA profile blurb (Profile/Icon/WebPage)
.github/workflows/ # docker-dev.yml (dev branch → :dev), docker-main.yml (main/tags → :latest/:x.y.z)
requirements.txt   # pinned deps
```

### Request flow / routes (all in `main.py`)

Auth gate: every page checks `_logged_in(request)` (session has `user`) and redirects to
`/login` if not. GET pages that render the app shell **also** redirect to
`/change-password` when `session["must_change_password"]` is set. JSON/API endpoints
return `401` instead of redirecting.

| Route | Method | Purpose | Auth |
|---|---|---|---|
| `/health` | GET | Liveness for Docker HEALTHCHECK | none (intentional) |
| `/static/*` | GET | App icon/favicon | none (intentional) |
| `/login` | GET/POST | Login; rate-limited; sets session | public |
| `/logout` | GET | `session.clear()` | any |
| `/change-password` | GET/POST | Forced first-login change | login |
| `/` | GET | Dashboard (10 most-recent runs) | login + pw-change gate |
| `/history` | GET | Run history: window (`?days=`, default 30, `0`=all) + `?job=`/`?status=` filters, 500-row cap | login + gate |
| `/jobs` | GET | Job list | login + gate |
| `/jobs/new` | GET/POST | Create job | login |
| `/jobs/{id}/edit` | GET/POST | Edit job (blank key = keep existing) | login |
| `/jobs/{id}/delete` | POST | Delete job | login |
| `/jobs/{id}/toggle` | POST | Enable/disable (JSON) | login (401) |
| `/jobs/{id}/run` | POST | Run now (spawns bg task, JSON) | login (401) |
| `/logs` | GET | Live-log page | login + gate |
| `/logs/stream` | GET | SSE tail of sync.log | login (401) |
| `/logs/support-bundle` | GET | ZIP of redacted logs/config/runs/sysinfo | login |
| `/settings` | GET | Settings page | login + gate |
| `/settings/username` | POST | Change username | login |
| `/settings/password` | POST | Change password (verifies current) | login |
| `/settings/webhook` | POST | Save global webhook (blank URL = keep) | login |
| `/api/test-webhook` | POST | Send a sample notification | login (401) |
| `/api/test-connection` | POST | Probe an Immich server + check API-key scopes | login (401) |
| `/api/jobs/{id}/status` | GET | Job status JSON | login (401) |

### Data model (`models.py`)

- `SyncJob` — name, `source_url`/`source_key`/`source_album_name`,
  `dest_url`/`dest_key`/`dest_album_name`, `schedule` (5-field cron), `delete_sync`,
  `cleanup_cache`, `enabled`, timestamps, plus notification columns
  `notify_override` (`inherit`|`off`|`custom`), `webhook_url` (Fernet ciphertext),
  `webhook_events` (CSV). **`source_key`/`dest_key`/`webhook_url` are Fernet
  ciphertext**, not plaintext.
- `SyncRun` — per-run counters (found/downloaded/uploaded/skipped/failed), `status`
  (`running|success|partial|failed`), `error_message`, timestamps. Cascade-deleted with
  the job.
- `Settings` — key/value store. Holds `admin_username`, `admin_password_hash` (bcrypt),
  `password_changed` (`"true"`/`"false"`), and the global webhook config
  `webhook_enabled` / `webhook_url` (encrypted) / `webhook_events` (CSV).

> **⚠️ Adding a column to an existing table needs a migration.** SQLite `create_all`
> only creates missing *tables*, never missing *columns*, so a model edit alone leaves
> older databases broken. `database.py::_migrate_schema()` runs `ALTER TABLE … ADD COLUMN`
> (idempotent, guarded by `PRAGMA table_info`) after `create_all`. When you add a `SyncJob`
> column, add it to `_migrate_schema`'s `new_columns` map too. The notify columns above
> were the first users of this.

### Sync flow (`sync.py::run_sync_job`)

1. Decrypt both API keys (`decrypt_secret`, falls back to treating value as legacy
   plaintext on any failure).
2. Locate source album by name (case-insensitive), list assets, pair Live Photo `.MOV`
   companions via `livePhotoVideoId`.
3. **Checksum pre-check** against the destination (`/api/assets/bulk-upload-check` by
   SHA-1): assets already on B are added straight to the album (no download/upload);
   only new assets are queued. Best-effort — on failure it downloads everything and lets
   immich-go de-dupe.
4. Download new originals into `${CACHE_PATH}/job_{id}/files` in **rolling batches**
   (`BATCH_SIZE_MB` / `BATCH_FILE_COUNT`); each full batch is uploaded then cleared.
5. Upload each batch via `immich-go upload from-folder --server URL --api-key KEY
   --into-album NAME --recursive DIR` (flags **must** come after `from-folder` in v0.31).
6. Cache clearing: intermediate batches always cleared; final batch cleared only if
   `cleanup_cache` and the run succeeded. Mid-batch upload error → stop early, keep cache
   for the next run (status `partial`).

Two triggers reach `run_sync_job`: the APScheduler cron (`scheduler.py::
_scheduled_sync_runner`) and the "Run now" button (`main.py::_run_background` via
`asyncio.create_task`). See the concurrency gotcha below.

### Notifications (`notify.py`)

- Both run paths (`_run_background`, `_scheduled_sync_runner`) fire a `start` event when
  the run begins and a final event mapped from status via `notify.STATUS_TO_EVENT`
  (`success`/`partial`/`failed`).
- `notify.notify(db, job, event, results=, run=)` is the entry point. It loads the
  `webhook_*` Settings rows, calls `_resolve_target(global_settings, job)` to pick the
  effective `(url, events)`, gates on `event in events`, then POSTs. **It never raises** —
  a failure is a logged warning, so notifications can't break a sync.
- Config resolution: `notify_override == "off"` → silent; `"custom"` → the job's own
  (decrypted) `webhook_url` + events; `"inherit"` (default) → the global config, but only
  if `webhook_enabled == "true"`. Empty url or empty events → no send.
- `build_payload(url, …)` picks the shape by URL host: Discord (`discord.com`/
  `discordapp.com`) → embeds; Slack (`hooks.slack.com`) → attachments; anything else →
  generic JSON. **Payloads carry no secrets** — job name, album names, counts, status,
  timestamps, and (failures only) the error string. Never the API keys or webhook URL.
- `send_test(url)` powers the "Send test" buttons via `POST /api/test-webhook` (which
  falls back to the stored per-job/global URL when the field is left blank).
- `_SECRET_KEY` is read from env at import (same as `sync.py`) to decrypt stored URLs.

---

## SECRET_KEY (the one env var that matters)

- `SECRET_KEY` signs session cookies **and** derives the Fernet key that encrypts API
  keys. The placeholder in code/template/compose is
  `change-me-to-a-unique-random-32-64-char-string`.
- **Startup refuses to boot** (`main.py` `startup()` raises `RuntimeError`) if the key is
  unset, begins with `change-me` (any placeholder, case-insensitive), or is under 16
  chars. This is the guard that keeps the public default from ever being used. 16–31 chars
  boots with a warning; 32–128 is the recommended range.
- Changing `SECRET_KEY` invalidates all stored API keys (can't decrypt) and logs out all
  sessions. This is documented in README + the Unraid template — keep those in sync if
  the behavior changes.
- Key derivation is a single `SHA-256(SECRET_KEY)` → base64 → Fernet key (`crypto.py::
  _fernet_key`). No salt/KDF; acceptable only because SECRET_KEY is meant to be
  high-entropy, not a human password.

---

## Publishing & release (GitHub + GHCR + Unraid CA)

Image: `ghcr.io/nightcrawler1016/immich-album-sync`.

| Branch / ref | Workflow | Tags pushed | APP_VERSION baked |
|---|---|---|---|
| push to `dev` | `docker-dev.yml` | `:dev`, `:dev-<sha>` | `dev-<sha7>` |
| push to `main` | `docker-main.yml` | `:latest`, `:<sha>` | `main-<sha7>` |
| tag `vX.Y.Z` | `docker-main.yml` | `:X.Y.Z`, `:X.Y`, `:X`, `:<sha>` | `X.Y.Z` (tag minus `v`) |

- **App version** is baked at build time: CI passes `--build-arg APP_VERSION=…`, the
  Dockerfile turns it into `ENV APP_VERSION`, and `main.py` reads `os.getenv("APP_VERSION",
  "1.0.0")` and shows it in the UI (About/sidebar). There is **no version string to bump
  by hand** — cutting a `vX.Y.Z` git tag is the release action. (Contrast with LaunchPad's
  manual version-bump ritual — this repo does *not* work that way.)
- Both workflows build `linux/amd64,linux/arm64`, use GHA cache, and log in with the
  built-in `GITHUB_TOKEN` (`permissions: contents:read, packages:write`). No
  `pull_request_target`, no external PAT.
- **Unraid Community Applications:** `immich-album-sync.xml` is the template CA ships.
  `<TemplateURL>` points at the raw `main` copy and **must** stay valid — CA re-fetches it.
  `<Icon>` and the compose label both point at `raw.githubusercontent.com/.../main/icon.png`,
  so **don't rename or move `icon.png`** without updating every reference (README,
  compose labels, `immich-album-sync.xml`, `ca_profile.xml`). Keep the env-var list in the
  XML in sync with what the code actually reads.

### Release checklist

1. Land changes on `dev`, let `:dev` build, test the dev image.
2. Merge to `main` (ships `:latest`).
3. For a versioned release, push a `vX.Y.Z` tag (this is what stamps the human version).
4. If any env var / volume / port / default changed: update **README.md**,
   **docker-compose.yml**, and **immich-album-sync.xml** together — they duplicate the
   same facts and drift is the most common bug here.
5. If a security control changed, re-check the README "Security" section — it makes
   specific claims (see below) that must remain true.

### Runtime env vars (keep XML ⇄ code ⇄ README in sync)

`SECRET_KEY` (required), `TZ`, `PUID`/`PGID` (default 99:100), `CLEANUP_CACHE`,
`CACHE_PATH` (default `/app/appdata/cache`), `BATCH_SIZE_MB` (10240), `BATCH_FILE_COUNT`
(0), `DB_PATH` (`/app/appdata/config.db`), `LOG_PATH` (`/app/appdata/logs/sync.log`),
`RUN_HISTORY_RETENTION_DAYS` (90; `0` = forever; in README + XML), `APP_VERSION`
(build-time). Read in code but **not** documented in README/XML: `DOWNLOAD_TIMEOUT_SECONDS`
(300, `immich_client.py`) and `WEBHOOK_TIMEOUT_SECONDS` (10, `notify.py`).

---

## Security posture & README claims (verify before you touch these)

The README's "Security" section makes concrete promises. Each is implemented as noted —
if you change the code, keep the claim honest:

- **Forced first-login change** — enforced app-wide by the `_auth_gate` dependency
  (`main.py`, wired via `FastAPI(dependencies=[...])`): every non-public route (all
  POST/`/api/*`/`/logs/*` included) requires a session AND blocks until the change is done.
  Default password `admin` is blocked from reuse (min 8 chars). *(Fixed 2026-07 — this used
  to be enforced only on GET pages, so POST/API routes could bypass it.)*
- **API-key encryption** — Fernet (AES-128-CBC + HMAC), key from SECRET_KEY. Keys are
  never rendered into HTML (forms post blank = "keep existing"; edit shows a "stored"
  indicator). `crypto.encrypt/decrypt` **fail open to plaintext** if the `cryptography`
  package is missing or a token doesn't decrypt — by design for legacy/roll-forward, but
  it means "encrypted at rest" depends on the package being present and SECRET_KEY being
  stable.
- **Sessions** — signed cookie, `max_age=86400` (24h). Cookie is **not** marked `Secure`
  (fine on plain-HTTP LAN; relevant if fronted by HTTPS — still open, see below).
- **Login rate limiting** — in-memory per-IP, 10 fails / 15 min → 5-min lockout. Resets
  on restart; behind a reverse proxy all clients share the proxy IP unless real IPs are
  forwarded (there's no `X-Forwarded-For` handling — `request.client.host` is used).
- **CSRF** — `csrf_protect` middleware compares `Origin` (then `Referer`) host to the
  `Host` header on unsafe methods and **fails closed** (rejects when neither header is
  present or the host mismatches), backed by the `SameSite=Lax` cookie. No CSRF token.
  *(Fixed 2026-07 — used to fail open on missing headers.)*
- **Non-root** — `entrypoint.sh` starts as root, fixes ownership, `gosu`-drops to
  PUID:PGID before uvicorn.
- **Safe support bundle** — `/logs/support-bundle` redacts API keys (`[redacted]`) and
  masks URL hosts + bare IPv4 in both `sync.log` and job configs. IPv6 addresses are
  **not** masked by the current regexes.

### Security review status (verified review, 2026-07)

**Fixed and shipped to `:latest` (commit `36da370`)**
- ✅ **`must_change_password` bypass** — closed via the app-wide `_auth_gate` dependency.
- ✅ **Default/weak `SECRET_KEY`** — startup now refuses to boot on any `change-me*`
  placeholder / unset / <16-char key (was warn-only).
- ✅ **Per-job concurrency** — `try_acquire_job`/`release_job` guard (`sync.py`); manual run
  returns 409, cron fire skips, so no two runs share `job_{id}/files`.
- ✅ **CSRF fail-open** — now fails closed on missing/mismatched `Origin`/`Referer`.
- ✅ **Interrupted runs stuck `running`** — reconciled to `failed` on startup.

**Still open — Medium (safe to do; no UX trade-off)**
- **immich-go gets the API key as `--api-key` argv** (`sync.py` `_run_immich_go_upload`) →
  readable via `/proc`. Pass via child-process env instead. **This currently makes the
  README's "keys decrypted only in memory at sync time" claim untrue — fix the code or
  soften the README.**
- **Dockerfile downloads immich-go + gosu with no checksum/signature check** (`Dockerfile`).
  Pin SHA-256 per arch and `sha256sum -c` before install.
- **Support-bundle redaction (`_redact_text`) masks hosts/IPv4 but not API-key tokens** and
  not IPv6; if immich-go ever echoes the key it lands in the shareable `sync.log`. **This
  makes the README's "support bundle never contains API keys" claim not fully guaranteed.**
  Do an exact-string replace of the known key value at log-write time.

**Still open — Medium (needs a decision — interacts with plain-HTTP LAN use)**
- **Session cookie not `Secure`** (starlette default `https_only=False`) — add
  `https_only`/`SESSION_COOKIE_SECURE`, but gate it so plain-HTTP LAN deploys still work.
- **Password change doesn't invalidate old sessions** — cookies are stateless `{user}`; add a
  credential-version in the session payload and bump it on password change.

**Still open — Low / polish**
- Rate limiter keys on `request.client.host` → collapses to one bucket (and a DoS lever)
  behind a reverse proxy; no `X-Forwarded-For` handling.
- `download_original` has no per-asset size ceiling — a hostile source can fill the disk.
- `http://` server URLs accepted with no warning → API key sent cleartext. (TLS verify is
  correctly **on** for `https://` — don't add an insecure toggle.)
- CI actions pinned by mutable tag, not commit SHA.
- `sync_runs` now has retention: `scheduler.prune_run_history()` deletes runs older than
  `RUN_HISTORY_RETENTION_DAYS` (default 90; `0` = keep forever), always keeping the last
  `_HISTORY_PER_JOB_FLOOR` (10) per job. Runs once on startup and daily at 03:30 UTC (an
  APScheduler job id `maintenance_prune_history`). Reads stay fast via
  `ix_sync_runs_started_at` / `ix_sync_runs_job_started`. **`sync.log` still grows
  unbounded** (no rotation) — that's the remaining follow-up here.
- Same-`originalFileName` assets collide in cache (one silently skipped) — namespace the
  cache filename with the asset id.
- `decrypt_secret` swallows all exceptions and returns ciphertext as if plaintext; invalid
  cron saved silently (job never runs); `job_toggle` swallows scheduling errors; deprecated
  `asyncio.get_event_loop()` in the progress callback; `ImmichClient.test_connection` is dead
  code; a fresh `httpx.AsyncClient` per call (no pooling).
- **SSRF by design**: `test-connection` and the sync engine fetch operator-supplied URLs —
  acceptable for a single-admin tool, just be aware.

---

## Local dev & test

```bash
# From the repo root (mind the spaces in the path):
docker build -t immich-album-sync:local .
docker run -p 8080:8080 \
  -e SECRET_KEY=local-dev-secret-at-least-32-characters-long \
  -v "$(pwd)/appdata:/app/appdata" \
  immich-album-sync:local
# → http://localhost:8080  (admin / admin, forced password change)
```

`appdata/`, `*.db`, `*.log`, `cache/` are git-ignored, as are design sources
(`*.psd`, `screenshots/psd/`, `screenshots/image source files/`).

---

## Conventions for the next agent

- **Keep line endings LF.** See the two-computer section — this is the single most likely
  way to create a mess in this repo.
- **When you add/rename an env var**, update all of: the code default, `README.md`,
  `docker-compose.yml`, and `immich-album-sync.xml`. They restate the same facts.
- **Don't hand-bump a version string** — there isn't one to bump. Cut a `vX.Y.Z` tag.
- **Don't move `icon.png`** or change the repo/owner slug without fixing every
  `raw.githubusercontent.com/.../main/icon.png` and `ghcr.io/nightcrawler1016/…` reference.
- **The README is a public security contract.** If you weaken or remove a control, update
  the README claim in the same change.
- `main.py` is large — read the section you need; it's organized with clear banner
  comments per area (auth, CSRF, jobs, logs, settings, api).
