import asyncio
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .crypto import decrypt_secret
from .immich_client import ImmichClient

_SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-something-random-and-long")

logger = logging.getLogger(__name__)

CACHE_BASE = os.getenv("CACHE_PATH", "/app/appdata/cache")
LOG_PATH = os.getenv("LOG_PATH", "/app/appdata/logs/sync.log")

# Batch processing limits — prevent cache overflow during large first-time syncs.
# Files are downloaded into the cache until a limit is hit, then uploaded and cleared
# before the next batch begins. 0 = unlimited for that dimension.
_BATCH_SIZE_BYTES: int = int(os.getenv("BATCH_SIZE_MB", "10240")) * 1024 * 1024  # default 10 GB
_BATCH_FILE_COUNT: int = int(os.getenv("BATCH_FILE_COUNT", "0"))                 # default unlimited

# --------------------------------------------------------------------------- #
# Logging setup
# --------------------------------------------------------------------------- #

def _get_sync_file_logger() -> logging.Logger:
    """Return a logger that writes to the sync log file."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    sync_logger = logging.getLogger("sync.file")
    if sync_logger.handlers:
        return sync_logger  # already configured

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
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
    """
    if not name:
        return fallback
    base = os.path.basename(str(name).replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        return fallback
    return base


def _clear_dir_contents(path: Path) -> None:
    """Delete everything inside *path* while keeping the directory itself.

    Removing and recreating the directory races on network shares (SMB/CIFS):
    ``mkdir`` can raise ``FileExistsError`` immediately after ``rmtree`` because
    the directory-entry removal hasn't fully propagated. Clearing the contents
    in place never touches the directory inode, so it sidesteps the race.
    """
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- #
# Main sync entry point
# --------------------------------------------------------------------------- #

async def run_sync_job(
    job,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Perform a full one-way sync for *job*.

    Flow:
      1. Find album on source (Immich A)
      2. Collect all assets + Live Photo companions
      3. Download originals to cache
      4. Upload to destination (Immich B) via immich-go
      5. Optionally clean up cache

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

    def log(msg: str, level: str = "info"):
        getattr(sync_log, level)(msg)
        if progress_callback:
            try:
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: progress_callback(msg)
                )
            except Exception:
                pass

    # Decrypt API keys — handles both Fernet-encrypted and legacy plaintext values
    source_api_key = decrypt_secret(job.source_key, _SECRET_KEY)
    dest_api_key = decrypt_secret(job.dest_key, _SECRET_KEY)

    job_cache_dir = Path(CACHE_BASE) / f"job_{job.id}"
    files_dir = job_cache_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    sync_log.info("=" * 70)
    sync_log.info(f"▶  SYNC START  [{job.name}]  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    sync_log.info(f"   Source : {job.source_url}  album='{job.source_album_name}'")
    sync_log.info(f"   Dest   : {job.dest_url}  album='{job.dest_album_name}'")

    try:
        source = ImmichClient(job.source_url, source_api_key)

        # ------------------------------------------------------------------ #
        # Step 1 — Locate the album
        # ------------------------------------------------------------------ #
        log(f"Searching for album '{job.source_album_name}' on source server…")
        album = await source.get_album_by_name(job.source_album_name)

        if not album:
            raise ValueError(
                f"Album '{job.source_album_name}' not found on {job.source_url}. "
                "Check the album name and API key."
            )

        album_id = album["id"]
        sync_log.info(f"   Found  : '{album['albumName']}' (id={album_id})")

        # ------------------------------------------------------------------ #
        # Step 2 — Collect assets + Live Photo companions
        # ------------------------------------------------------------------ #
        assets = await source.get_album_assets(album_id)
        sync_log.info(f"   Assets : {len(assets)} in album")
        results["assets_found"] = len(assets)

        log(f"Found {len(assets)} assets in album '{job.source_album_name}'")

        to_download: list[dict] = []
        seen_ids: set[str] = set()

        for asset in assets:
            asset_id = asset["id"]
            if asset_id in seen_ids:
                continue
            seen_ids.add(asset_id)

            filename = _safe_filename(asset.get("originalFileName"), f"{asset_id}.bin")
            to_download.append({
                "id": asset_id,
                "filename": filename,
                "checksum": asset.get("checksum"),
            })

            # Fetch Live Photo companion (.MOV paired with .HEIC)
            live_video_id = asset.get("livePhotoVideoId")
            if live_video_id and live_video_id not in seen_ids:
                seen_ids.add(live_video_id)
                try:
                    video_info = await source.get_asset_info(live_video_id)
                    video_filename = _safe_filename(
                        video_info.get("originalFileName"),
                        f"{Path(filename).stem}.MOV",
                    )
                    to_download.append({
                        "id": live_video_id,
                        "filename": video_filename,
                        "checksum": video_info.get("checksum"),
                        "live_companion": True,
                    })
                    sync_log.info(f"   Live   : Paired {filename} ↔ {video_filename}")
                except Exception as exc:
                    sync_log.warning(f"   Live   : Could not fetch companion for {filename}: {exc}")

        sync_log.info(f"   Queue  : {len(to_download)} files to process (incl. companions)")

        # ------------------------------------------------------------------ #
        # Step 2.5 — Skip assets the destination already has (checksum pre-check)
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
        dest = ImmichClient(job.dest_url, dest_api_key)
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
                    if res and res.get("action") == "reject" and res.get("assetId"):
                        existing_dest_ids.append(res["assetId"])
                    else:
                        filtered.append(it)
                new_items = filtered

                if existing_dest_ids:
                    # Ensure those already-present assets are in the target album.
                    dest_album = await dest.get_album_by_name(job.dest_album_name)
                    if dest_album:
                        await dest.add_assets_to_album(dest_album["id"], existing_dest_ids)
                    else:
                        await dest.create_album(job.dest_album_name, existing_dest_ids)
                    results["assets_skipped"] += len(existing_dest_ids)
                    sync_log.info(
                        f"   Dedup  : {len(existing_dest_ids)} already on destination "
                        f"— ensured in album, skipped download"
                    )
                sync_log.info(f"   New    : {len(new_items)} asset(s) need downloading")
        except Exception as exc:
            sync_log.warning(
                f"   Dedup  : destination pre-check failed ({exc}); "
                "downloading all and letting immich-go de-dupe"
            )
            new_items = to_download

        # ------------------------------------------------------------------ #
        # Steps 3+4+5 — Download and upload in rolling batches
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

        downloaded = skipped = failed = 0
        total_uploaded = 0
        batch_files: list[Path] = []
        batch_bytes = 0
        batch_num = 0
        total_items = len(new_items)

        for idx, item in enumerate(new_items):
            dest_file = files_dir / item["filename"]
            file_bytes = 0

            # Download or reuse cached file
            if dest_file.exists() and dest_file.stat().st_size > 0:
                file_bytes = dest_file.stat().st_size
                skipped += 1
            else:
                try:
                    file_bytes = await source.download_original(item["id"], str(dest_file))
                    sync_log.info(f"   ↓  {item['filename']}  ({file_bytes / 1_048_576:.1f} MB)")
                    downloaded += 1
                except Exception as exc:
                    sync_log.error(f"   ✗  {item['filename']}: {exc}")
                    failed += 1
                    continue  # failed file — skip batch tracking

            batch_files.append(dest_file)
            batch_bytes += file_bytes

            is_last = (idx == total_items - 1)
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

            dir_file_count = sum(1 for f in files_dir.glob("*") if f.is_file())

            if dir_file_count == 0:
                sync_log.info("   Upload : No files in cache — skipping upload step")
            else:
                if is_batching:
                    log(f"Uploading batch {batch_num} "
                        f"({len(batch_files)} files, {batch_gb:.1f} GB)…")
                else:
                    log(f"Uploading {dir_file_count} files to '{job.dest_album_name}'…")

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
                else:
                    if is_batching:
                        sync_log.info(
                            f"   Batch {batch_num}: {batch_up} uploaded  "
                            f"(running total: {total_uploaded})"
                        )
                    else:
                        sync_log.info(
                            f"   Upload : {total_uploaded} files pushed to destination"
                        )
                    log(f"Upload complete — {total_uploaded} uploaded so far")

            # Clear intermediate batches always; clear final batch only if cleanup_cache=True
            clear_now = (not is_last) or \
                        (is_last and cleanup and results["status"] == "success")
            if clear_now:
                # Empty the staged files but keep files_dir itself. Removing and
                # recreating the directory races on network shares (SMB/CIFS):
                # mkdir() can raise FileExistsError right after rmtree() because
                # the directory-entry removal hasn't propagated yet. A cleanup
                # failure must never fail an already-successful upload.
                try:
                    _clear_dir_contents(files_dir)
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
        # assets_skipped already holds the count skipped by the dedup pre-check;
        # add the per-file cache hits (files already present locally) to it.
        results["assets_skipped"] += skipped
        results["assets_failed"] = failed
        results["assets_uploaded"] = total_uploaded

        sync_log.info(
            f"   Done   : {downloaded} new  |  {results['assets_skipped']} skipped  |  "
            f"{failed} failed  |  {total_uploaded} uploaded"
        )
        if is_batching and batch_num > 1:
            log(
                f"All {batch_num} batches complete — "
                f"{total_uploaded} files uploaded to destination"
            )
        else:
            log(
                f"Sync complete — {downloaded} downloaded, "
                f"{results['assets_skipped']} skipped (already on destination), "
                f"{total_uploaded} uploaded"
            )

    except Exception as exc:
        results["status"] = "failed"
        results["error_message"] = str(exc)
        sync_log.error(f"   ERROR  : {exc}")
        log(f"SYNC FAILED: {exc}")

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
    Call immich-go as a subprocess to upload files.
    Supports immich-go v0.22+ command structure.
    Returns {"uploaded": int, "error": str|None}
    """
    # immich-go v0.31 syntax (flags go AFTER the subcommand, matching README examples):
    #   immich-go upload from-folder --server URL --api-key KEY --into-album NAME --recursive DIR
    # NOTE: placing --server/--api-key before "upload" as global flags breaks v0.31's
    #       argument parser — they must follow the "from-folder" subcommand.
    cmd = [
        "immich-go",
        "upload",
        "from-folder",
        "--server", server,
        "--api-key", api_key,
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Stream stdout in real time
        uploaded_count = 0
        stdout_lines = []

        async def read_stdout():
            nonlocal uploaded_count
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    sync_log.info(f"   [go]   {text}")
                    stdout_lines.append(text)

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
            async for line in proc.stderr:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    sync_log.warning(f"   [go!]  {text}")

        await asyncio.gather(read_stdout(), read_stderr())
        await proc.wait()

        if proc.returncode != 0:
            return {
                "uploaded": uploaded_count,
                "error": f"immich-go exited with code {proc.returncode}. "
                         "Check logs for details.",
            }

        return {"uploaded": uploaded_count, "error": None}

    except FileNotFoundError:
        return {
            "uploaded": 0,
            "error": "immich-go binary not found. Rebuild the Docker image.",
        }
    except Exception as exc:
        return {"uploaded": 0, "error": str(exc)}
