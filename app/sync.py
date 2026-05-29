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

            filename = asset.get("originalFileName") or f"{asset_id}.bin"
            to_download.append({"id": asset_id, "filename": filename})

            # Fetch Live Photo companion (.MOV paired with .HEIC)
            live_video_id = asset.get("livePhotoVideoId")
            if live_video_id and live_video_id not in seen_ids:
                seen_ids.add(live_video_id)
                try:
                    video_info = await source.get_asset_info(live_video_id)
                    video_filename = video_info.get("originalFileName") or \
                        f"{Path(filename).stem}.MOV"
                    to_download.append({
                        "id": live_video_id,
                        "filename": video_filename,
                        "live_companion": True,
                    })
                    sync_log.info(f"   Live   : Paired {filename} ↔ {video_filename}")
                except Exception as exc:
                    sync_log.warning(f"   Live   : Could not fetch companion for {filename}: {exc}")

        sync_log.info(f"   Queue  : {len(to_download)} files to process (incl. companions)")

        # ------------------------------------------------------------------ #
        # Step 3 — Download originals
        # ------------------------------------------------------------------ #
        downloaded = skipped = failed = 0

        for item in to_download:
            dest_file = files_dir / item["filename"]

            # Already cached?
            if dest_file.exists() and dest_file.stat().st_size > 0:
                skipped += 1
                continue

            try:
                size_bytes = await source.download_original(item["id"], str(dest_file))
                size_mb = size_bytes / (1024 * 1024)
                sync_log.info(f"   ↓  {item['filename']}  ({size_mb:.1f} MB)")
                downloaded += 1
            except Exception as exc:
                sync_log.error(f"   ✗  {item['filename']}: {exc}")
                failed += 1

        results["assets_downloaded"] = downloaded
        results["assets_skipped"] = skipped
        results["assets_failed"] = failed

        sync_log.info(
            f"   DL done: {downloaded} new  |  {skipped} cached  |  {failed} failed"
        )
        log(f"Downloads complete — {downloaded} new, {skipped} already cached, {failed} failed")

        # ------------------------------------------------------------------ #
        # Step 4 — Upload via immich-go
        # ------------------------------------------------------------------ #
        file_count = sum(1 for _ in files_dir.glob("*") if _.is_file())
        if file_count == 0:
            sync_log.info("   Upload : No files in cache — skipping upload step")
        else:
            log(f"Uploading {file_count} files to '{job.dest_album_name}'…")
            upload_result = await _run_immich_go_upload(
                server=job.dest_url,
                api_key=dest_api_key,
                album_name=job.dest_album_name,
                source_dir=str(files_dir),
                sync_log=sync_log,
            )
            results["assets_uploaded"] = upload_result.get("uploaded", 0)

            if upload_result.get("error"):
                results["status"] = "partial"
                results["error_message"] = upload_result["error"]
                sync_log.error(f"   Upload : {upload_result['error']}")
            else:
                sync_log.info(f"   Upload : {results['assets_uploaded']} files pushed to destination")

            log(f"Upload complete — {results['assets_uploaded']} uploaded")

        # ------------------------------------------------------------------ #
        # Step 5 — Optional cache cleanup
        # ------------------------------------------------------------------ #
        cleanup = getattr(job, "cleanup_cache", False) or \
                  os.getenv("CLEANUP_CACHE", "false").lower() == "true"
        if cleanup and results["status"] == "success":
            shutil.rmtree(str(files_dir), ignore_errors=True)
            files_dir.mkdir(parents=True, exist_ok=True)
            sync_log.info("   Cache  : Cleaned up after successful upload")

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
