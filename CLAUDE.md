# Immich Album Sync — Claude Code working notes

One-way album sync between two [Immich](https://immich.app) servers, with a web UI.
Private/master Immich A → curated/family Immich B. Photos are pulled from A via the
Immich REST API and pushed to B with the [`immich-go`](https://github.com/simulot/immich-go)
CLI (pinned **v0.32.0** — adds Immich v3 server support, backward compatible with v2;
v0.31.0 uploads 400 against v3), which does the duplicate detection and album creation
on the destination.

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
  main.py          # FastAPI app + EVERY route (~1340 lines). Auth gate, CSRF + anti-framing
                   # middleware, jobs CRUD, log SSE stream, support bundle, settings,
                   # test-connection.
  sync.py          # run_sync_job(): the whole sync flow + immich-go subprocess wrapper,
                   # per-job run guard, rotating sync.log logger.
  immich_client.py # ImmichClient: async httpx wrapper for the Immich REST API.
  crypto.py        # Fernet encrypt/decrypt of secrets, key derived from SECRET_KEY.
  database.py      # SQLAlchemy engine/session, init_db() seeds default admin.
  models.py        # SyncJob, SyncRun, Settings (key/value) tables.
  notify.py        # Webhook notifications: resolve config, build/format payload, send;
                   # also redact_text() (URL hosts/IPv4/IPv6), shared with the bundle.
  scheduler.py     # APScheduler wrapper: cron_trigger / schedule_job / remove_job,
                   # execute_run (shared run lifecycle), run-history pruning.
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

Auth gate: the app-wide `_auth_gate` dependency (`FastAPI(dependencies=[...])`) is the
**single** access control. For every path not in `_PUBLIC_PATHS` (`/health`, `/login`,
`/logout`, `/change-password`) or under `/static`, it requires `_logged_in(request)`
(session has `user` **and** its `epoch` matches the `session_epoch` setting) and blocks
until a forced password change is done. HTML paths get a 302 to `/login` /
`/change-password`; JSON/API paths (`_path_wants_json`) get `401` (or `403` while a
password change is pending). **Route handlers do not
re-check the session** — don't add per-route checks back, and don't add a new route
outside the gate without thinking about it. (The two `/change-password` handlers check
`_logged_in` themselves because that path is in `_PUBLIC_PATHS`.)

> **⚠️ The gate must read `request.scope["path"]`, never `request.url.path`.** Starlette
> (≤1.0.0, incl. the pinned 0.49.3) rebuilds `request.url` from the unvalidated `Host`
> header, so `Host: x/static?` makes any path look public (CVE-2026-48710). Since handlers
> no longer re-check, a regression here opens every GET route to anonymous users.

Request bodies are capped at 64 KiB by the outermost `_BodySizeLimit` ASGI middleware
(counts streamed bytes, so chunked bodies can't bypass it; over-limit → 413). It exists
because Starlette 0.49's urlencoded form parser ignores `max_fields` (CVE-2026-54283,
reachable unauthenticated via `POST /login`). Raise the cap if a route ever needs bigger
bodies.

| Route | Method | Purpose | Auth |
|---|---|---|---|
| `/health` | GET | Liveness for Docker HEALTHCHECK | none (intentional) |
| `/static/*` | GET | App icon/favicon | none (intentional) |
| `/login` | GET/POST | Login; rate-limited; always runs bcrypt; clears + sets session (with epoch) | public |
| `/logout` | GET | `session.clear()` | any |
| `/change-password` | GET/POST | Forced first-login change (no current-pw prompt). Redirects to `/settings` unless `must_change_password` is set | login (own check) |
| `/` | GET | Dashboard (10 most-recent runs) | gate |
| `/history` | GET | Run history: window (`?days=`, default 30, `0`=all) + `?job=`/`?status=` filters, 500-row cap | gate |
| `/jobs` | GET | Job list | gate |
| `/jobs/new` | GET/POST | Create job; invalid cron / webhook URL → form re-rendered with 400 | gate |
| `/jobs/{id}/edit` | GET/POST | Edit job (blank key = keep existing); same validation | gate |
| `/jobs/{id}/delete` | POST | Delete job + its `job_{id}` cache dir; refused (redirect to `/jobs?error=…`) while that job is syncing | gate |
| `/jobs/{id}/toggle` | POST | Enable/disable (JSON); **400** + stays paused if the stored schedule is invalid | gate (401) |
| `/jobs/{id}/run` | POST | Run now (spawns `execute_run` task, JSON); 409 if already running | gate (401) |
| `/logs` | GET | Live-log page | gate |
| `/logs/stream` | GET | SSE: last 200 lines (reads only a 256 KB tail), then follows; reopens on rotation | gate (401) |
| `/logs/support-bundle` | GET | ZIP of redacted logs/config/runs/sysinfo (plain `def` → threadpool) | gate |
| `/settings` | GET | Settings page | gate |
| `/settings/username` | POST | Change username (blank rejected); bumps session epoch | gate |
| `/settings/password` | POST | Change password (verifies current); bumps session epoch | gate |
| `/settings/webhook` | POST | Save global webhook (blank URL = keep) | gate |
| `/api/test-webhook` | POST | Send a sample notification | gate (401) |
| `/api/test-connection` | POST | Probe an Immich server + check API-key scopes | gate (401) |
| `/api/jobs/{id}/status` | GET | Job status JSON | gate (401) |

### Data model (`models.py`)

- `SyncJob` — name, `source_url`/`source_key`/`source_album_name`,
  `dest_url`/`dest_key`/`dest_album_name`, `schedule` (5-field cron), `delete_sync`,
  `cleanup_cache`, `enabled`, timestamps, plus notification columns
  `notify_override` (`inherit`|`off`|`custom`), `webhook_url` (Fernet ciphertext),
  `webhook_events` (CSV). **`source_key`/`dest_key`/`webhook_url` are Fernet
  ciphertext**, not plaintext. `last_run_at`/`next_run_at` columns still exist but are
  **no longer written** (UI reads next-run from APScheduler via `get_next_run_time`);
  left in place rather than dropped, since SQLite column drops need a table rebuild.
  `delete_sync` drives the Mirror deletes step (see sync flow).
- `SyncRun` — per-run counters (found/downloaded/uploaded/skipped/failed), `status`
  (`running|success|partial|failed`), `error_message`, timestamps. Cascade-deleted with
  the job.
- `Settings` — key/value store. Holds `admin_username`, `admin_password_hash` (bcrypt),
  `password_changed` (`"true"`/`"false"`), `session_epoch` (integer string; see
  Sessions below), and the global webhook config `webhook_enabled` / `webhook_url`
  (encrypted) / `webhook_events` (CSV).

> **⚠️ Adding a column to an existing table needs a migration.** SQLite `create_all`
> only creates missing *tables*, never missing *columns*, so a model edit alone leaves
> older databases broken. `database.py::_migrate_schema()` runs `ALTER TABLE … ADD COLUMN`
> (idempotent, guarded by `PRAGMA table_info`) after `create_all`. When you add a `SyncJob`
> column, add it to `_migrate_schema`'s `new_columns` map too. The notify columns above
> were the first users of this.

### Sync flow (`sync.py::run_sync_job`)

1. Decrypt both API keys (`decrypt_secret`). Legacy plaintext (not a Fernet token) is
   returned unchanged; a real Fernet token that won't decrypt (SECRET_KEY changed) returns
   `""`, and the run **fails** immediately with "A stored API key could not be decrypted
   … Re-enter the source and destination API keys" instead of sending ciphertext to Immich.
2. Locate source album by name (case-insensitive), list assets, pair Live Photo `.MOV`
   companions via `livePhotoVideoId`. Listing is version-adaptive: album-detail
   `assets` array on Immich ≤ v2; Immich v3 removed it, so `get_album_assets` falls
   back to paginated `POST /api/search/metadata` with `albumIds` (needs `asset.read`,
   which source keys already have). `check_permissions` uses the same fallback to
   find its sample asset. Each queued item gets a `subdir` = the **still's** asset id, so a
   Live Photo's still and motion part share one cache folder.
3. **Checksum pre-check** against the destination (`/api/assets/bulk-upload-check` by
   SHA-1): assets already on B are added straight to the album (no download/upload) —
   **except** Live Photo motion parts, which stay linked to their still and are not made
   album members (mirrors A). The destination album is looked up with `exact=True` (same
   name match as immich-go's `--into-album`). Only new assets are queued. Best-effort — on
   failure it downloads everything and lets immich-go de-dupe.
4. **Start-of-run cache prune**: `_clear_dir_contents(files_dir, keep=<subdirs of queued
   items>)` deletes everything in `${CACHE_PATH}/job_{id}/files` except folders of assets
   still pending upload. This is load-bearing: immich-go uploads the whole folder
   recursively, so any stale file (already on B, or since removed from A) would be
   re-uploaded / re-added to B's album. Pending files from an interrupted run are reused.
5. Download new originals into `files/<asset-id>/<filename>` in **rolling batches**
   (`BATCH_SIZE_MB` / `BATCH_FILE_COUNT`). `download_original` is **atomic** — writes
   `<file>.part`, `os.replace`s on completion, deletes the fragment on any error/cancel.
   A download failure counts as `failed` and makes the run `partial`, but does **not**
   `continue` past the batch-flush check, so a failure on the last item still uploads
   the final batch.
6. Upload each batch via `immich-go upload from-folder --server URL --into-album NAME
   --recursive DIR` (flags **must** come after `from-folder`; syntax unchanged in v0.32).
   The key goes in the child env as `IMMICH_GO_UPLOAD_API_KEY` (verified for v0.32.0 —
   re-check on any immich-go bump), **never argv**. stdout/stderr are read line by line
   with `readline()`; the `ValueError` from immich-go's `\r` progress ticker overrunning
   the 64 KiB reader limit is swallowed, only the last `\r` segment of each line is kept,
   and every line is scrubbed of the key before logging. On error/cancellation the
   `finally` kills immich-go if it's still running.
7. Cache clearing: intermediate batches always cleared; final batch cleared only if
   cleanup (`job.cleanup_cache` **or** env `CLEANUP_CACHE=true`) and the run succeeded —
   otherwise it's removed by the next run's prune. Mid-batch upload error → stop early,
   keep cache for the next run (status `partial`).
8. **Mirror deletes** (`job.delete_sync`, opt-in, UI-labelled experimental) —
   `_mirror_album_removals`: removes from B's album (exact name) every asset whose
   checksum isn't among the source album's assets, via `DELETE /api/albums/{id}/assets`
   (`ImmichClient.remove_assets_from_album`, chunks of 500; needs `albumAsset.delete`).
   **Album membership only — nothing trashed.** Also removes photos added to B's album
   directly. A B asset counts as "in A" by checksum **or** by same `originalFileName` +
   `fileCreatedAt` within 5 s — immich-go's own "server already has it" rule; without it,
   when B holds a different copy, immich-go re-adds B's copy and the mirror removes it
   again every run. Guards: skipped (warning) if the source listing is empty or any asset
   lacks a checksum; skipped if it would remove **every** destination asset. Each removal
   is logged by filename + id (reversible). Runs even after a partial upload (depends only
   on the two listings). An exception marks the run `partial`. A one-time startup
   migration (`Settings.mirror_deletes_v2`) switched `delete_sync` off on jobs that ticked
   it while it was a no-op, so they opt in again under the real behavior.

Source asset ids (and `livePhotoVideoId`) must match the UUID pattern `_ASSET_ID_RE`
before use — they become cache folder names, and the source server is untrusted.

Two triggers reach `run_sync_job`, both via `scheduler.py::execute_run(job_id, run_id)`
(the single run lifecycle: start notification, `run_sync_job`, record results, final
notification, `release_job`): the APScheduler cron (`_scheduled_sync_runner` reserves the
job, creates the `SyncRun`, awaits `execute_run`) and the "Run now" button (`main.py::
job_run_now` reserves, creates the run, `asyncio.create_task(execute_run(...))`, holding
the task in `_background_runs` so it can't be GC'd). The caller reserves via
`try_acquire_job`; `execute_run` is the only place that releases (callers release only if
creating the run row fails).

`sync.log` is a `RotatingFileHandler` (10 MB × 3 backups: `sync.log.1..3`). Timestamps use
the container's local `TZ`; everything else (UI, schedules) is UTC.

### Notifications (`notify.py`)

- `scheduler.execute_run` (shared by cron and "Run now") fires a `start` event when
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
  timestamps, and (failed/partial only) the error string — passed through
  `redact_text` (URL hosts, IPv4, IPv6 masked) **before** truncation. Never the API keys or
  webhook URL.
- Per-job webhook URLs are validated with `is_valid_webhook_url` on save, like the global one.
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
  sessions. Runs then fail with a clear "re-enter the API keys" error (see sync flow
  step 1). This is documented in README + the Unraid template — keep those in sync if
  the behavior changes.
- The README/compose examples deliberately use the `change-me-…` placeholder so a
  copy-pasted example is rejected by the boot guard. Don't introduce example keys that
  don't start with `change-me` (the local-dev `docker run` examples are the exception).
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
6. **Bumping `IMMICH_GO_VERSION` or `GOSU_VERSION` in the Dockerfile** also requires
   updating the per-arch SHA-256 pins next to it (amd64 + arm64, from the release's
   checksums file) — the build runs `sha256sum -c` and fails otherwise. For immich-go, also
   re-verify that `IMMICH_GO_UPLOAD_API_KEY` is still the env binding for
   `upload --api-key` (otherwise immich-go gets no key and every upload fails).
7. The Dockerfile runs `python -m compileall -q app && python -c "import app.main"` as a
   build-time smoke test (the repo has no test suite). A broken import or dependency
   mismatch fails the image build — read the build log, don't remove the step.

### Runtime env vars (keep XML ⇄ code ⇄ README in sync)

`SECRET_KEY` (required), `TZ`, `PUID`/`PGID` (default 99:100), `CLEANUP_CACHE`,
`CACHE_PATH` (default `/app/appdata/cache`), `BATCH_SIZE_MB` (10240), `BATCH_FILE_COUNT`
(0), `DB_PATH` (`/app/appdata/config.db`), `LOG_PATH` (`/app/appdata/logs/sync.log`),
`RUN_HISTORY_RETENTION_DAYS` (90; `0` = forever; in README + XML), `APP_VERSION`
(build-time). Read in code but **not** documented in README/XML: `DOWNLOAD_TIMEOUT_SECONDS`
(300, `immich_client.py`) and `WEBHOOK_TIMEOUT_SECONDS` (10, `notify.py`).
`FORWARDED_ALLOW_IPS` is read by **uvicorn** itself (not app code); it is mentioned only in
the README Security section (reverse-proxy rate-limiting note), deliberately not in the XML.
`TZ` affects `sync.log` timestamps only — schedules are `CronTrigger(timezone="UTC")` and
UI timestamps are naive UTC, labelled "UTC" by the `datetimefmt` filter.

---

## Security posture & README claims (verify before you touch these)

The README's "Security" section makes concrete promises. Each is implemented as noted —
if you change the code, keep the claim honest:

- **Forced first-login change** — enforced app-wide by the `_auth_gate` dependency
  (`main.py`, wired via `FastAPI(dependencies=[...])`): every non-public route (all
  POST/`/api/*`/`/logs/*` included) requires a session AND blocks until the change is done.
  Default password `admin` is blocked from reuse (min 8 chars). *(Fixed 2026-07 — this used
  to be enforced only on GET pages, so POST/API routes could bypass it.)* The
  `/change-password` form (which skips the current-password prompt) only works while
  `must_change_password` is set; otherwise both GET and POST redirect to `/settings`, where
  `/settings/password` verifies the current password.
- **API-key encryption** — Fernet (AES-128-CBC + HMAC), key from SECRET_KEY. Keys are
  never rendered into HTML (forms post blank = "keep existing"; edit shows a "stored"
  indicator). `crypto` now **fails closed**: `cryptography` is a hard import (no
  plaintext fallback), `encrypt_secret` raises rather than storing plaintext, and
  `decrypt_secret` returns a value unchanged only if it isn't a Fernet token (legacy
  plaintext, `is_encrypted`); an undecryptable token logs an error and returns `""`.
  `startup()` re-encrypts any plaintext `source_key`/`dest_key` left by pre-encryption
  builds, so "never stored in plaintext" holds for old installs too.
- **Key handling at runtime** — keys are decrypted in memory for a sync, a
  test-connection with a blank key field, and support-bundle scrubbing. The destination
  key reaches immich-go via the child env (`IMMICH_GO_UPLOAD_API_KEY`), never argv — so not
  in `ps` / `/proc/<pid>/cmdline` (`/proc/<pid>/environ` is readable only by the same uid
  or root). immich-go output lines are scrubbed of the key before logging.
- **Sessions** — signed cookie, `max_age=86400`, re-issued on each response (so 24 h of
  *inactivity*). Cookie is **not** marked `Secure` (fine on plain-HTTP LAN; still open,
  see below). **Session epoch revocation:** the cookie carries `epoch`; `_logged_in`
  compares it with the `session_epoch` setting (one DB read per gated request) and clears
  a stale session. `_bump_session_epoch` runs on both password-change paths and on username
  change — every *other* session is signed out, the current one is re-stamped. Login
  clears the session before setting `user`/`epoch`.
- **Login** — bcrypt runs on every attempt (even for a wrong username), so response time
  doesn't reveal a valid username. Blank usernames are rejected on change (they'd lock the
  admin out).
- **Login rate limiting** — in-memory per-IP, 10 fails / 15 min → 5-min lockout. Resets
  on restart. `_record_login_failure` sweeps expired entries once per window, so memory is
  bounded. Keyed on `request.client.host`; uvicorn's proxy-headers handling only trusts
  `X-Forwarded-For` from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`), so behind a reverse
  proxy all clients share the proxy IP unless the operator sets it. **Documented in the
  README only — no app-level handling.**
- **CSRF** — `csrf_protect` middleware compares `Origin` (then `Referer`) host to the
  `Host` header on unsafe methods and **fails closed** (rejects when neither header is
  present or the host mismatches), backed by the `SameSite=Lax` cookie. No CSRF token.
  *(Fixed 2026-07 — used to fail open on missing headers.)* Behind a reverse proxy the
  original `Host` must be forwarded or every POST is 403 — README documents the nginx /
  Apache settings.
- **Anti-framing** — the same middleware sets `X-Frame-Options: DENY` and
  `Content-Security-Policy: frame-ancestors 'none'` on every response. Deliberately only
  `frame-ancestors`: a fuller CSP would block the Tailwind CDN and inline template scripts.
  Side effect (documented in README): the UI can't be embedded in Organizr/Homarr iframes.
- **Non-root** — `entrypoint.sh` starts as root, fixes `/app/appdata` ownership, chowns
  the `CACHE_PATH` directory itself plus its `job_*` folders (never the user's whole
  share recursively), then `gosu`-drops to PUID:PGID before uvicorn.
- **Safe support bundle** — `/logs/support-bundle` applies `_scrub_job_secrets` (exact-
  string replace of every current job's decrypted API keys, plus bare hostnames of each
  job's source/dest URL, with lookarounds so `immich` doesn't mangle `immich-go`) and then
  `notify.redact_text` (URL hosts, IPv4, IPv6 — candidates validated with `ipaddress` so
  `12:34:56` times survive) to `sync.log` and every `sync_runs.json` error message. Job
  configs get `[redacted]` keys and masked URL hosts. Only the **current** `sync.log` is
  included (not `.1`–`.3`). Limit: keys/hosts of deleted jobs, or a key since replaced,
  aren't known to the scrubber — acceptable now that the key never reaches the log via argv.
- **Webhook payloads** — error text goes through `notify.redact_text` (URL hosts/IPs; not
  bare hostnames).
- **Build supply chain** — Dockerfile verifies immich-go and gosu downloads against pinned
  per-arch SHA-256s (`sha256sum -c`); unknown arch fails the build instead of silently
  installing the amd64 binary.

### Security review status (verified reviews, 2026-07 and 2026-10)

**Fixed 2026-07 — shipped to `:latest` (commit `36da370`)**
- ✅ **`must_change_password` bypass** — closed via the app-wide `_auth_gate` dependency.
- ✅ **Default/weak `SECRET_KEY`** — startup now refuses to boot on any `change-me*`
  placeholder / unset / <16-char key (was warn-only).
- ✅ **Per-job concurrency** — `try_acquire_job`/`release_job` guard (`sync.py`); manual run
  returns 409, cron fire skips, so no two runs share `job_{id}/files`.
- ✅ **CSRF fail-open** — now fails closed on missing/mismatched `Origin`/`Referer`.
- ✅ **Interrupted runs stuck `running`** — reconciled to `failed` on startup.

**Fixed 2026-10 (dev branch)**
- ✅ **API key on immich-go argv** → passed as `IMMICH_GO_UPLOAD_API_KEY` in the child env;
  immich-go output scrubbed of the key.
- ✅ **Unverified immich-go/gosu downloads** → per-arch SHA-256 pins + `sha256sum -c`.
- ✅ **Support-bundle redaction gaps** → API key values, bare configured hostnames, IPv6
  masked; `sync_runs.json` error messages redacted too.
- ✅ **Password change didn't invalidate old sessions** → session epoch (also bumped on
  username change).
- ✅ **`/change-password` usable any time without the current password** → restricted to
  the forced first-login change.
- ✅ **Username timing oracle** at login → bcrypt always runs.
- ✅ **Blank username could lock the admin out** → rejected.
- ✅ **Unbounded login-limiter memory** → periodic sweep of expired entries.
- ✅ **Clickjacking** → `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`.
- ✅ **Webhook error text leaked server hosts/IPs** to third parties → `redact_text`.
- ✅ **crypto failed open** (plaintext on missing package / ciphertext used as key) → fails
  closed; legacy plaintext keys re-encrypted at startup; undecryptable keys fail the run
  with a clear message.
- ✅ **Invalid cron saved silently / `job_toggle` swallowed errors** → `cron_trigger`
  validation on save (form 400) and toggle (JSON 400, job stays paused). Per-job webhook
  URLs validated like the global one.
- ✅ **`sync.log` grew unbounded** → `RotatingFileHandler` 10 MB × 3; live view reads only
  the tail and follows rotation.
- ✅ **Same-`originalFileName` cache collisions** → per-asset `files/<asset-id>/` subdirs.
- ✅ **Stale cache re-fed to immich-go every run** (re-added photos removed from A back
  into B's album when cleanup was off) → start-of-run prune to pending files.
- ✅ **Download failures reported `success`; failure on the last item skipped the final
  batch upload** → `partial`, and the final batch always flushes.
- ✅ **Non-atomic downloads** (truncated file later treated as cached) → `.part` + rename.
- ✅ **immich-go `\r` progress overflowed the stream reader on long uploads; orphaned
  immich-go on error/cancel** → tolerant `readline` loop; kill in `finally`.
- ✅ **Deleting a job left its cached private originals** (SQLite can reuse the id) →
  cache dir deleted; delete refused (redirect with a message) while the job is syncing.
- ✅ **Pre-check added Live Photo motion parts to B's album** → only stills/regular assets.
- ✅ **"Mirror deletes" checkbox did nothing** → implemented (`_mirror_album_removals`).
- ✅ **Dependency CVEs** (python-multipart unauthenticated DoS) → fastapi 0.120.4,
  starlette 0.49.3 (explicit pin), python-multipart 0.0.32.
- ✅ **Dead code** removed: `ImmichClient.test_connection`, `progress_callback` (with its
  deprecated `asyncio.get_event_loop()`), `_require_login`, `next_run_at`/`last_run_at`
  writes, per-route auth re-checks. "Run now" and cron share `scheduler.execute_run`.

**Still open — Medium (needs a decision — interacts with plain-HTTP LAN use)**
- **Session cookie not `Secure`** (starlette default `https_only=False`) — add an opt-in
  `https_only`/`SESSION_COOKIE_SECURE`, gated so plain-HTTP LAN deploys still work.

**Owner decisions — by design, don't "fix" (2026-10)**
- **Default `admin`/`admin` on a fresh install stays.** Requirement instead: nothing but the
  forced password change is reachable until it's done (enforced by `_auth_gate`).
- **Assets trashed on the destination are never restored.** Server B is a disposable
  replica the user can wipe and re-sync; a trashed asset counts as "already on B" until
  B's trash is emptied, then the next sync uploads it fresh. A photo removed from A and
  later re-added is put back into B's album by the pre-check like any new photo.

**Still open — Low / polish**
- **Starlette 0.49.3 has known CVEs** (CVE-2026-48710 Host-header URL confusion,
  CVE-2026-54283 form `max_fields` ignored). Both are mitigated in app code (gate on
  `scope["path"]`; `_BodySizeLimit`), but the real fix is Starlette ≥1.3.1, which needs a
  newer FastAPI **and** rewriting all 12 `templates.TemplateResponse(name, {"request": …})`
  calls to the `TemplateResponse(request, name, ctx)` signature Starlette 1.x requires —
  do it in one change and smoke-test every page.
- Rate limiter keys on `request.client.host` → one shared bucket (and a lockout DoS lever)
  behind a reverse proxy unless `FORWARDED_ALLOW_IPS` is set. **Documented only** (README);
  no app-level handling.
- `download_original` has no per-asset size ceiling — a hostile source can fill the disk.
- `http://` server URLs accepted with no warning → API key sent cleartext. (TLS verify is
  correctly **on** for `https://` — don't add an insecure toggle.)
- CI actions pinned by mutable tag, not commit SHA.
- **Query-string flash reflection** — `/settings?success=…` / `?error=…` text is rendered
  from the URL (autoescaped, so no XSS, but a crafted link can show arbitrary text in the
  app's UI). Use fixed message codes instead.
- **Live Photo companion lookups are sequential N+1** (`get_asset_info` per Live Photo in
  `run_sync_job` step 2) — slow on big albums; could be batched/concurrent.
- A fresh `httpx.AsyncClient` per call (no pooling).
- `sync_runs` retention: `scheduler.prune_run_history()` deletes runs older than
  `RUN_HISTORY_RETENTION_DAYS` (default 90; `0` = keep forever), always keeping the last
  `_HISTORY_PER_JOB_FLOOR` (10) per job. Runs once on startup and daily at 03:30 UTC (an
  APScheduler job id `maintenance_prune_history`). Reads stay fast via
  `ix_sync_runs_started_at` / `ix_sync_runs_job_started`. (Info, not an open issue.)
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

## AI Architectural & Pragmatism Rules

1. **YAGNI (You Aren't Gonna Need It):** Build ONLY the requested features. Do not add
   unused abstraction layers, speculative hooks, unused helper parameters, or
   future-proofing logic — unless omitting it would break upgrades or persisted data
   (schema migrations, cache-busting and similar plumbing are load-bearing, not
   speculative).
2. **KISS (Keep It Simple):** Use native browser APIs before reaching for libraries.
   Prefer flat, readable code with early returns over deeply nested logic.
3. **DRY Pragmatism:** Abstract logic when exact repetition occurs, but avoid hasty
   abstractions for mildly similar UI components.
4. **Single Responsibility:** Keep rendering/presentation separate from data fetching,
   complex state, and heavy calculations — following the separation pattern the codebase
   already uses.
5. **No Unused Code:** Do not output dead functions, unused variables, empty handlers,
   or unreferenced imports.
