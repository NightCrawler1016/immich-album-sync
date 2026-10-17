import asyncio
import contextlib
import logging
import logging.handlers
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .crypto import decrypt_secret
from .immich_client import ImmichClient

_SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-a-unique-random-32-64-char-string")

logger = logging.getLogger(__name__)

CACHE_BASE = os.getenv("CACHE_PATH", "/app/appdata/cache")
LOG_PATH = os.getenv("LOG_PATH", "/app/appdata/logs/sync.log")

# --------------------------------------------------------------------------- #
# In-flight guard
#
# Prevents two runs of the SAME job from executing at once — a manual "Run now"
# colliding with a scheduled fire, or two rapid clicks. Both paths write into
# the shared cache dir (job_{id}/files) and clear it, so overlap causes
# truncated/double uploads and wrong counts. asyncio is single-threaded, so a
# check-then-add with no await in between is atomic; no lock object is needed.
# --------------------------------------------------------------------------- #
_active_job_ids: set = set()


def try_acquire_job(job_id: int) -> bool:
    """Reserve *job_id* for a run. Returns False if a run is already in flight."""
    if job_id in _active_job_ids:
        return False
    _active_job_ids.add(job_id)
    return True


def release_job(job_id: int) -> None:
    """Release the in-flight reservation for *job_id* (safe if not held)."""
    _active_job_ids.discard(job_id)

# Batch processing limits — prevent cache overflow during large first-time syncs.
# Files are downloaded into the cache until a limit is hit, then uploaded and cleared
# before the next batch begins. 0 = unlimited for that dimension.
_BATCH_SIZE_BYTES: int = int(os.getenv("BATCH_SIZE_MB", "10240")) * 1024 * 1024  # default 10 GB
_BATCH_FILE_COUNT: int = int(os.getenv("BATCH_FILE_COUNT", "0"))                 # default unlimited

# Immich asset ids are UUIDs. They become cache folder names, so anything else
# from the (untrusted) source server — e.g. "../.." or "/app" — is refused.
_ASSET_ID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def _valid_asset_id(value) -> bool:
    return isinstance(value, str) and bool(_ASSET_ID_RE.fullmatch(value))

# --------------------------------------------------------------------------- #
# Logging setup
# --------------------------------------------------------------------------- #

def _get_sync_file_logger() -> logging.Logger:
    """Return a logger that writes to the (size-rotated) sync log file."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    sync_logger = logging.getLogger("sync.file")
    if sync_logger.handlers:
        return sync_logger  # already configured

    # Rotate at 10 MB, keep 3 old files — hourly syncs on big albums otherwise
    # grow the log without bound (and the live-log view / support bundle read it).
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    sync_logger.addHandler(fh)
    sync_logger.setLevel(logging.INFO)
    sync_logger.propagate = False
    return sync_logger


def _safe_filename(name: Optional[str], fallback: str) -> str:
    """Reduce a server-supplied filename to a safe basename for the cache dir.

    The *source* Immich server controls ``originalFileName``, so it must never
    be trusted as a path. Stripping directory components (on both POSIX and
    Windows separators) and rejecting ``.``/``..`` keeps every download inside
    the job's cache directory — without this, a malicious or compromised source
    could return e.g. ``../../app/main.py`` and write outside the cache.
    Control characters are replaced so a filename can't inject fake log lines.
    """
    if not name:
        return fallback
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    base = re.sub(r"[\x00-\x1f\x7f]", "_", base)
    if not base or base in (".", ".."):
        return fallback
    return base


def _clear_dir_contents(path: Path, keep: frozenset = frozenset()) -> int:
    """Delete everything inside *path* except names in *keep*; returns the count.

    Removing and recreating the directory races on network shares (SMB/CIFS):
    ``mkdir`` can raise ``FileExistsError`` immediately after ``rmtree`` because
    the directory-entry removal hasn't fully propagated. Clearing the contents
    in place never touches the directory inode, so it sidesteps the race.
    """
    removed = 0
    for child in path.iterdir():
        if child.name in keep:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
        removed += 1
    return removed


def _parse_time(value) -> Optional[datetime]:
    """Parse an Immich ISO timestamp ("…Z" or with offset) as an aware UTC datetime."""
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


async def _mirror_album_removals(
    dest: ImmichClient, album_name: str, source_assets: list, sync_log: logging.Logger
) -> None:
    """Remove from B's album every asset that isn't in A's album.

    An asset counts as "in A" when its SHA-1 matches, or — immich-go's own rule
    for "the server already has this photo" — it has the same filename and a
    capture time within 5 s. Without the second test, when B holds a different
    copy of a photo, immich-go adds B's copy and this step would remove it again,
    every run.

    Album membership only — nothing is trashed or deleted. Hidden Live Photo
    motion parts that are album members on B are removed too: A's album never
    holds them, Immich's album view hides them, and playback follows the still.
    Refuses to act on a listing that can't be trusted, so a bad source response
    can never empty the destination album.
    """
    keep = {a.get("checksum") for a in source_assets}
    if not source_assets or None in keep:
        sync_log.warning("   Mirror : skipped — source listing is empty or lacks checksums")
        return
    taken: dict = {}  # originalFileName -> capture times in A's album
    for a in source_assets:
        t = _parse_time(a.get("fileCreatedAt"))
        if t:
            taken.setdefault(a.get("originalFileName"), []).append(t)

    def in_source(a: dict) -> bool:
        if a["checksum"] in keep:
            return True
        t = _parse_time(a.get("fileCreatedAt"))
        return t is not None and any(
            abs((t - s).total_seconds()) <= 5 for s in taken.get(a.get("originalFileName"), ())
        )

    dest_album = await dest.get_album_by_name(album_name, exact=True)
    if not dest_album:
        return
    dest_assets = await dest.get_album_assets(dest_album["id"])
    stale = [a for a in dest_assets if a.get("checksum") and not in_source(a)]
    if not stale:
        return
    if len(stale) == len(dest_assets):
        sync_log.warning(
            f"   Mirror : skipped — all {len(stale)} destination assets would be removed "
            "(source/destination listing mismatch?)"
        )
        return
    await dest.remove_assets_from_album(dest_album["id"], [a["id"] for a in stale])
    # Name every removal so it can be undone by re-adding these ids to the album.
    for a in stale:
        sync_log.info(f"   Mirror : removed {a.get('originalFileName') or '?'} (id={a['id']})")
    sync_log.info(
        f"   Mirror : removed {len(stale)} asset(s) no longer in the source album "
        "from the destination album (assets not deleted)"
    )


# --------------------------------------------------------------------------- #
# Main sync entry point
# --------------------------------------------------------------------------- #

async def run_sync_job(job) -> dict:
    """
    Perform a full one-way sync for *job*.

    Flow:
      1. Find album on source (Immich A)
      2. Collect all assets + Live Photo companions
      3. Skip assets B already has (checksum pre-check)
      4. Download new originals to cache and upload them to B via immich-go
      5. Optionally mirror removals (delete_sync) and clean up the cache

    Returns a results dict that maps to SyncRun columns.
    """
    sync_log = _get_sync_file_logger()

    results = {
        "assets_found": 0,
        "assets_downloaded": 0,
        "assets_uploaded": 0,
        "assets_skipped": 0,
        "assets_failed": 0,
        "status": "success",
        "error_message": None,
    }

    # Decrypt API keys — handles both Fernet-encrypted and legacy plaintext values
    source_api_key = decrypt_secret(job.source_key, _SECRET_KEY)
    dest_api_key = decrypt_secret(job.dest_key, _SECRET_KEY)

    files_dir = Path(CACHE_BASE) / f"job_{job.id}" / "files"

    sync_log.info("=" * 70)
    sync_log.info(f"▶  SYNC START  [{job.name}]  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    sync_log.info(f"   Source : {job.source_url}  album='{job.source_album_name}'")
    sync_log.info(f"   Dest   : {job.dest_url}  album='{job.dest_album_name}'")

    try:
        if not source_api_key or not dest_api_key:
            raise ValueError(
                "A stored API key could not be decrypted (SECRET_KEY changed?). "
                "Re-enter the source and destination API keys in the job."
            )
        files_dir.mkdir(parents=True, exist_ok=True)
        source = ImmichClient(job.source_url, source_api_key)
        dest = ImmichClient(job.dest_url, dest_api_key)

        # ------------------------------------------------------------------ #
        # Step 1 — Locate the album
        # ------------------------------------------------------------------ #
        sync_log.info(f"Searching for album '{job.source_album_name}' on source server…")
        album = await source.get_album_by_name(job.source_album_name)

        if not album:
            raise ValueError(
                f"Album '{job.source_album_name}' not found on the source server. "
                "Check the album name and API key."
            )

        album_id = album["id"]
        sync_log.info(f"   Found  : '{album['albumName']}' (id={album_id})")

        # ------------------------------------------------------------------ #
        # Step 2 — Collect assets + Live Photo companions
        #
        # Each asset is cached in its own subdirectory named by the still's id,
        # so two assets sharing an originalFileName can't overwrite each other,
        # while a Live Photo's still and motion part stay side by side.
        # ------------------------------------------------------------------ #
        assets = await source.get_album_assets(album_id)
        sync_log.info(f"   Assets : {len(assets)} in album")
        results["assets_found"] = len(assets)

        to_download: list[dict] = []
        seen_ids: set[str] = set()

        for asset in assets:
            asset_id = asset.get("id")
            if not _valid_asset_id(asset_id):
                sync_log.warning(f"   Skip   : asset with malformed id {asset_id!r}")
                continue
            if asset_id in seen_ids:
                continue
            seen_ids.add(asset_id)

            filename = _safe_filename(asset.get("originalFileName"), f"{asset_id}.bin")
            to_download.append({
                "id": asset_id,
                "subdir": asset_id,
                "filename": filename,
                "checksum": asset.get("checksum"),
            })

            # Fetch Live Photo companion (.MOV paired with .HEIC)
            live_video_id = asset.get("livePhotoVideoId")
            if live_video_id and _valid_asset_id(live_video_id) and live_video_id not in seen_ids:
                seen_ids.add(live_video_id)
                try:
                    video_info = await source.get_asset_info(live_video_id)
                    video_filename = _safe_filename(
                        video_info.get("originalFileName"),
                        f"{Path(filename).stem}.MOV",
                    )
                    to_download.append({
                        "id": live_video_id,
                        "subdir": asset_id,
                        "filename": video_filename,
                        "checksum": video_info.get("checksum"),
                        "live_companion": True,
                    })
                    sync_log.info(f"   Live   : Paired {filename} ↔ {video_filename}")
                except Exception as exc:
                    sync_log.warning(f"   Live   : Could not fetch companion for {filename}: {exc}")

        sync_log.info(f"   Queue  : {len(to_download)} files to process (incl. companions)")

        # ------------------------------------------------------------------ #
        # Step 3 — Skip assets the destination already has (checksum pre-check)
        #
        # Immich identifies duplicates by the SHA-1 of the original file. Before
        # downloading anything, ask the destination which of these assets it
        # already holds. Ones already present are added straight to the album via
        # the API (no download, no upload); only genuinely-new assets are queued
        # for download. This keeps re-syncs of an unchanged album nearly free on
        # bandwidth and disk.
        #
        # Best-effort: if the check fails, fall back to downloading everything —
        # immich-go still de-dupes on upload, so correctness never depends on it.
        # ------------------------------------------------------------------ #
        new_items = to_download
        checkable = [it for it in to_download if it.get("checksum")]
        try:
            if checkable:
                check = await dest.bulk_upload_check(
                    [{"id": it["id"], "checksum": it["checksum"]} for it in checkable]
                )
                existing_dest_ids: list[str] = []
                filtered: list[dict] = []
                for it in to_download:
                    res = check.get(it["id"])
                    if not (res and res.get("action") == "reject" and res.get("assetId")):
                        filtered.append(it)
                        continue
                    # Already on B. A motion part stays linked to its still, so —
                    # as on A — only stills and regular assets are album members.
                    if not it.get("live_companion"):
                        existing_dest_ids.append(res["assetId"])
                new_items = filtered

                if existing_dest_ids:
                    # Ensure those already-present assets are in the target album.
                    dest_album = await dest.get_album_by_name(job.dest_album_name, exact=True)
                    if dest_album:
                        await dest.add_assets_to_album(dest_album["id"], existing_dest_ids)
                    else:
                        await dest.create_album(job.dest_album_name, existing_dest_ids)
                    results["assets_skipped"] += len(existing_dest_ids)
                    sync_log.info(
                        f"   Dedup  : {len(existing_dest_ids)} already on destination "
                        f"— ensured in album, skipped download"
                    )
                sync_log.info(f"   New    : {len(new_items)} file(s) need downloading")
        except Exception as exc:
            sync_log.warning(
                f"   Dedup  : destination pre-check failed ({exc}); "
                "downloading all and letting immich-go de-dupe"
            )
            new_items = to_download

        # Prune the cache to what this run still needs. Anything else — assets
        # already on B, or since removed from A, or orphaned by a deleted job —
        # would otherwise be handed to immich-go again on the next upload and
        # re-added to B's album. Cached files of still-pending assets are kept,
        # so a run interrupted mid-way resumes without re-downloading.
        try:
            pruned = await asyncio.to_thread(
                _clear_dir_contents, files_dir, frozenset(it["subdir"] for it in new_items)
            )
            if pruned:
                sync_log.info(f"   Cache  : pruned {pruned} stale cache entr{'y' if pruned == 1 else 'ies'}")
        except Exception as exc:
            sync_log.warning(f"   Cache  : could not prune stale cache entries: {exc}")

        # ------------------------------------------------------------------ #
        # Step 4 — Download and upload in rolling batches
        #
        # Files are staged in files_dir until a batch limit is reached, then
        # immediately uploaded and cleared before the next batch begins.
        # This prevents the local cache from growing unbounded during large
        # first-time syncs (e.g. multi-hundred-GB albums).
        #
        # Limits read from env at module load:
        #   BATCH_SIZE_MB  — max MB per batch  (0 = unlimited, default 10240)
        #   BATCH_FILE_COUNT — max files/batch (0 = unlimited, default 0)
        # ------------------------------------------------------------------ #
        cleanup = getattr(job, "cleanup_cache", False) or \
                  os.getenv("CLEANUP_CACHE", "false").lower() == "true"

        _size_limit = _BATCH_SIZE_BYTES   # bytes; 0 = unlimited
        _count_limit = _BATCH_FILE_COUNT  # files; 0 = unlimited
        is_batching = _size_limit > 0 or _count_limit > 0

        if is_batching:
            _parts: list[str] = []
            if _size_limit > 0:
                _parts.append(f"≤{_size_limit // 1_073_741_824} GB")
            if _count_limit > 0:
                _parts.append(f"≤{_count_limit} files")
            sync_log.info(f"   Batch  : enabled ({' + '.join(_parts)} per batch)")

        downloaded = failed = 0
        total_uploaded = 0
        batch_files: list[Path] = []
        batch_bytes = 0
        batch_num = 0
        total_items = len(new_items)

        for idx, item in enumerate(new_items):
            is_last = idx == total_items - 1
            dest_file = files_dir / item["subdir"] / item["filename"]

            if dest_file.exists() and dest_file.stat().st_size > 0:
                # Downloaded by an earlier run that stopped before uploading it
                batch_files.append(dest_file)
                batch_bytes += dest_file.stat().st_size
            else:
                try:
                    file_bytes = await source.download_original(item["id"], str(dest_file))
                    sync_log.info(f"   ↓  {item['filename']}  ({file_bytes / 1_048_576:.1f} MB)")
                    downloaded += 1
                    batch_files.append(dest_file)
                    batch_bytes += file_bytes
                except Exception as exc:
                    sync_log.error(f"   ✗  {item['filename']}: {exc}")
                    failed += 1
                    # No `continue`: a failure on the LAST item must still flush
                    # the batch, or every file downloaded before it is never uploaded.

            size_full = _size_limit > 0 and batch_bytes >= _size_limit
            count_full = _count_limit > 0 and len(batch_files) >= _count_limit

            # Only flush when a limit is hit or we've reached the final item
            if not (size_full or count_full or is_last):
                continue

            # ── Flush batch ────────────────────────────────────────────────
            batch_num += 1
            batch_gb = batch_bytes / 1_073_741_824
            remaining = total_items - idx - 1

            if is_batching:
                status_str = f"{remaining} item(s) remaining" if remaining else "final batch"
                sync_log.info(
                    f"   ─── Batch {batch_num}: {len(batch_files)} files "
                    f"({batch_gb:.2f} GB) — {status_str} ───"
                )

            if not batch_files:
                sync_log.info("   Upload : No files in this batch — skipping upload step")
            else:
                if is_batching:
                    sync_log.info(f"Uploading batch {batch_num} "
                                  f"({len(batch_files)} files, {batch_gb:.1f} GB)…")
                else:
                    sync_log.info(f"Uploading {len(batch_files)} files to '{job.dest_album_name}'…")

                upload_result = await _run_immich_go_upload(
                    server=job.dest_url,
                    api_key=dest_api_key,
                    album_name=job.dest_album_name,
                    source_dir=str(files_dir),
                    sync_log=sync_log,
                )
                batch_up = upload_result.get("uploaded", 0)
                total_uploaded += batch_up
                # Update running total so partial-failure runs still record progress
                results["assets_uploaded"] = total_uploaded

                if upload_result.get("error"):
                    results["status"] = "partial"
                    results["error_message"] = upload_result["error"]
                    sync_log.error(f"   Upload : {upload_result['error']}")
                    if not is_last:
                        sync_log.warning(
                            "   Upload error on mid-batch — stopping early; "
                            "cached files retained for next run"
                        )
                        break  # leave cache intact; next run will resume
                elif is_batching:
                    sync_log.info(
                        f"   Batch {batch_num}: {batch_up} uploaded  "
                        f"(running total: {total_uploaded})"
                    )
                else:
                    sync_log.info(f"   Upload : {total_uploaded} files pushed to destination")

            # Clear intermediate batches always; clear final batch only if cleanup_cache=True
            if (not is_last) or (cleanup and results["status"] == "success"):
                # A cleanup failure must never fail an already-successful upload.
                try:
                    await asyncio.to_thread(_clear_dir_contents, files_dir)
                    if is_last:
                        sync_log.info("   Cache  : Cleaned up after successful upload")
                    else:
                        sync_log.info(
                            f"   Batch {batch_num}: cache cleared, ready for next batch"
                        )
                except Exception as exc:
                    sync_log.warning(f"   Cache  : Could not clear staged files: {exc}")

            batch_files = []
            batch_bytes = 0

        results["assets_downloaded"] = downloaded
        results["assets_failed"] = failed
        results["assets_uploaded"] = total_uploaded
        if failed and results["status"] == "success":
            results["status"] = "partial"
            results["error_message"] = f"{failed} file(s) failed to download"

        # ------------------------------------------------------------------ #
        # Step 5 — Mirror deletes (opt-in): make B's album match A's album.
        # Correctness depends only on the two listings, not on this run's
        # uploads, so it runs even after a partial upload.
        # ------------------------------------------------------------------ #
        if job.delete_sync:
            try:
                await _mirror_album_removals(dest, job.dest_album_name, assets, sync_log)
            except Exception as exc:
                sync_log.error(f"   Mirror : could not remove stale assets: {exc}")
                if results["status"] == "success":
                    results["status"] = "partial"
                    results["error_message"] = f"Mirror deletes failed: {exc}"

        sync_log.info(
            f"   Done   : {downloaded} downloaded  |  {results['assets_skipped']} already on dest  |  "
            f"{failed} failed  |  {total_uploaded} uploaded"
        )

    except Exception as exc:
        results["status"] = "failed"
        results["error_message"] = str(exc)
        sync_log.error(f"   ERROR  : {exc}")

    finally:
        sync_log.info(
            f"■  SYNC END  [{job.name}]  status={results['status']}  "
            f"found={results['assets_found']}  up={results['assets_uploaded']}"
        )
        sync_log.info("=" * 70)

    return results


# --------------------------------------------------------------------------- #
# immich-go subprocess wrapper
# --------------------------------------------------------------------------- #

async def _run_immich_go_upload(
    server: str,
    api_key: str,
    album_name: str,
    source_dir: str,
    sync_log: logging.Logger,
) -> dict:
    """
    Call immich-go as a subprocess to upload every file under *source_dir*.
    Returns {"uploaded": int, "error": str|None}
    """
    # immich-go v0.31+ syntax (flags go AFTER the subcommand):
    #   immich-go upload from-folder --server URL --into-album NAME --recursive DIR
    # The API key is passed through immich-go's environment binding
    # (IMMICH_GO_UPLOAD_API_KEY — verified for the pinned v0.32.0; re-check on
    # any immich-go bump), never argv, so it isn't visible in `ps` or
    # /proc/<pid>/cmdline.
    cmd = [
        "immich-go",
        "upload",
        "from-folder",
        "--server", server,
        "--into-album", album_name,
        "--recursive",
        source_dir,
    ]

    sync_log.info(
        f"   CMD    : immich-go upload from-folder --server {server} "
        f"--into-album '{album_name}' {source_dir}"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env={**os.environ, "IMMICH_GO_UPLOAD_API_KEY": api_key},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"uploaded": 0, "error": "immich-go binary not found. Rebuild the Docker image."}
    except Exception as exc:
        return {"uploaded": 0, "error": str(exc)}

    async def read_lines(stream):
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                # immich-go's no-UI progress ticker rewrites one line with "\r"
                # and no newline; after ~64 KiB the reader overruns. That chunk
                # is discarded and the stream stays usable.
                continue
            if not line:
                return
            # Keep only the last "\r" progress state; scrub the key defensively.
            text = line.decode("utf-8", errors="replace").rsplit("\r", 1)[-1].strip()
            if text:
                yield text.replace(api_key, "[redacted-key]") if api_key else text

    uploaded_count = 0

    async def read_stdout():
        nonlocal uploaded_count
        async for text in read_lines(proc.stdout):
            sync_log.info(f"   [go]   {text}")
            # Parse the final summary report lines (most accurate).
            # "added to album : 3"  — preferred: assets actually in the album
            m = re.search(r"added to album\s*:\s*(\d+)", text, re.IGNORECASE)
            if m:
                uploaded_count = int(m.group(1))
                continue
            # "uploaded successfully : 1"  — fallback if no album line appears
            m = re.search(r"uploaded successfully\s*:\s*(\d+)", text, re.IGNORECASE)
            if m:
                uploaded_count = max(uploaded_count, int(m.group(1)))

    async def read_stderr():
        async for text in read_lines(proc.stderr):
            sync_log.warning(f"   [go!]  {text}")

    try:
        await asyncio.gather(read_stdout(), read_stderr())
        await proc.wait()
    except Exception as exc:
        return {"uploaded": uploaded_count, "error": str(exc)}
    finally:
        # Never leave immich-go running behind an error or a cancelled run —
        # an orphan would keep uploading into a cache the next run clears.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    if proc.returncode != 0:
        return {
            "uploaded": uploaded_count,
            "error": f"immich-go exited with code {proc.returncode}. "
                     "Check logs for details.",
        }
    return {"uploaded": uploaded_count, "error": None}
