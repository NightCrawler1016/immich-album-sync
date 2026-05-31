import httpx
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "300"))


class ImmichClient:
    """Async client for the Immich REST API."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {
            "x-api-key": api_key,
            "Accept": "application/json",
        }

    async def test_connection(self) -> dict:
        """Ping the server and return version/info. Raises on failure."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try /api/server/about first (Immich v1.100+)
            try:
                resp = await client.get(
                    f"{self.base_url}/api/server/about", headers=self.headers
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError:
                pass

            # Fallback: /api/server-info
            resp = await client.get(
                f"{self.base_url}/api/server-info", headers=self.headers
            )
            resp.raise_for_status()
            return resp.json()

    async def get_albums(self) -> list:
        """Return all albums visible to the API key owner."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{self.base_url}/api/albums", headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    async def get_album_by_name(self, album_name: str) -> Optional[dict]:
        """Case-insensitive album lookup. Returns None if not found."""
        albums = await self.get_albums()
        for album in albums:
            if album.get("albumName", "").lower() == album_name.lower():
                return album
        return None

    async def get_album_assets(self, album_id: str) -> list:
        """Return all assets belonging to an album."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{self.base_url}/api/albums/{album_id}", headers=self.headers
            )
            resp.raise_for_status()
            return resp.json().get("assets", [])

    async def get_asset_info(self, asset_id: str) -> dict:
        """Return full metadata for a single asset."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.base_url}/api/assets/{asset_id}", headers=self.headers
            )
            resp.raise_for_status()
            return resp.json()

    async def check_permissions(self, role: str) -> tuple[list[dict], list[dict]]:
        """
        Test the minimum required permissions for this API key.

        role: "source"  → checks album.read + asset.read
              "dest"    → checks album.read + album.write + asset.write (info only)

        Returns (permissions_list, albums_list).
        Each permission entry: {name, desc, ok (True/False/None), detail}
        ok=None means the permission cannot be verified without a real upload.
        """
        results: list[dict] = []
        albums: list[dict] = []

        async with httpx.AsyncClient(timeout=10.0) as client:

            # ── API key validity ─────────────────────────────────────────────
            # GET /api/users/me requires the user.read scope.
            # 401 = key is invalid or expired (hard stop).
            # 403 = key is valid but lacks user.read. For the SOURCE role that
            #       scope is not needed, so we continue. For the DEST role it IS
            #       needed — immich-go's connection validation calls this exact
            #       endpoint and refuses to upload without user.read — so we flag
            #       it as a dedicated requirement further below.
            me_status: int | None = None
            try:
                resp = await client.get(
                    f"{self.base_url}/api/users/me", headers=self.headers
                )
                me_status = resp.status_code
                if resp.status_code == 401:
                    results.append({
                        "name": "API Key", "desc": "Valid, non-expired API key",
                        "ok": False,
                        "detail": "HTTP 401 — key invalid or expired",
                    })
                    return results, albums
                elif resp.status_code == 403:
                    # Valid key — user.read scope not granted (handled per-role below)
                    note = "user.read scope not required" if role == "source" \
                        else "user.read scope checked below"
                    results.append({
                        "name": "API Key", "desc": "Valid, non-expired API key",
                        "ok": True,
                        "detail": f"authenticated ({note})",
                    })
                else:
                    resp.raise_for_status()
                    user = resp.json()
                    results.append({
                        "name": "API Key", "desc": "Valid, non-expired API key",
                        "ok": True,
                        "detail": user.get("email") or user.get("name") or "authenticated",
                    })
            except Exception as exc:
                results.append({
                    "name": "API Key", "desc": "Valid, non-expired API key",
                    "ok": False, "detail": str(exc),
                })
                return results, albums

            # ── album.read ───────────────────────────────────────────────────
            try:
                resp = await client.get(
                    f"{self.base_url}/api/albums", headers=self.headers
                )
                if resp.status_code == 403:
                    results.append({
                        "name": "album.read",
                        "desc": "List albums and read their contents",
                        "ok": False,
                        "detail": "Permission denied — enable album.read scope on this API key",
                    })
                else:
                    resp.raise_for_status()
                    albums = resp.json()
                    results.append({
                        "name": "album.read",
                        "desc": "List albums and read their contents",
                        "ok": True,
                        "detail": f"{len(albums)} album(s) visible",
                    })
            except Exception as exc:
                results.append({
                    "name": "album.read",
                    "desc": "List albums and read their contents",
                    "ok": False, "detail": str(exc),
                })

            # ── Grab a sample asset id to test asset-level scopes ─────────────
            # Walk albums until we find one containing at least one asset.
            # The album-detail endpoint is covered by album.read, so this does
            # not prove asset.read / asset.download on its own.
            sample_asset_id: str | None = None
            if albums:
                for alb in albums:
                    try:
                        resp = await client.get(
                            f"{self.base_url}/api/albums/{alb['id']}",
                            headers=self.headers,
                        )
                        if resp.status_code == 403:
                            break  # album.read denied — already reflected above
                        resp.raise_for_status()
                        alb_assets = resp.json().get("assets", [])
                        if alb_assets:
                            sample_asset_id = alb_assets[0]["id"]
                            break
                    except Exception:
                        continue

            # ── asset.read (metadata) ────────────────────────────────────────
            # GET /api/assets/{id} returns filenames + Live Photo pairing info.
            if sample_asset_id:
                try:
                    resp = await client.get(
                        f"{self.base_url}/api/assets/{sample_asset_id}",
                        headers=self.headers,
                    )
                    if resp.status_code == 403:
                        results.append({
                            "name": "asset.read",
                            "desc": "Read asset metadata (filenames, Live Photo pairing)",
                            "ok": False,
                            "detail": "Permission denied — enable asset.read scope on this API key",
                        })
                    else:
                        resp.raise_for_status()
                        results.append({
                            "name": "asset.read",
                            "desc": "Read asset metadata (filenames, Live Photo pairing)",
                            "ok": True, "detail": "Verified against a sample asset",
                        })
                except Exception as exc:
                    results.append({
                        "name": "asset.read",
                        "desc": "Read asset metadata (filenames, Live Photo pairing)",
                        "ok": False, "detail": str(exc),
                    })
            else:
                results.append({
                    "name": "asset.read",
                    "desc": "Read asset metadata (filenames, Live Photo pairing)",
                    "ok": True,
                    "detail": "Assumed OK — no assets available to test against",
                })

            # ── asset.download (source only) ─────────────────────────────────
            # GET /api/assets/{id}/original requires the asset.download scope —
            # this is SEPARATE from asset.read. A key with only album.read +
            # asset.read passes every other check but still gets 403 here, so we
            # must test it explicitly. Request a single byte via Range to keep
            # the probe cheap.
            if role == "source":
                if sample_asset_id:
                    try:
                        resp = await client.get(
                            f"{self.base_url}/api/assets/{sample_asset_id}/original",
                            headers={**self.headers, "Range": "bytes=0-0"},
                            follow_redirects=True,
                        )
                        if resp.status_code == 403:
                            results.append({
                                "name": "asset.download",
                                "desc": "Download original photo and video files",
                                "ok": False,
                                "detail": "Permission denied — enable asset.download scope on this API key",
                            })
                        else:
                            resp.raise_for_status()
                            results.append({
                                "name": "asset.download",
                                "desc": "Download original photo and video files",
                                "ok": True,
                                "detail": "Verified — original file is downloadable",
                            })
                    except Exception as exc:
                        results.append({
                            "name": "asset.download",
                            "desc": "Download original photo and video files",
                            "ok": False, "detail": str(exc),
                        })
                else:
                    results.append({
                        "name": "asset.download",
                        "desc": "Download original photo and video files",
                        "ok": None,
                        "detail": "Cannot verify — no assets in any album to test against",
                    })

            if role == "dest":
                # ── user.read (required by immich-go) ────────────────────────
                # immich-go validates the destination connection via
                # GET /api/users/me before uploading and aborts with
                # "Missing required permission: user.read" on a 403.
                if me_status == 403:
                    results.append({
                        "name": "user.read",
                        "desc": "Required by the immich-go upload engine to connect",
                        "ok": False,
                        "detail": "Permission denied — enable user.read scope (immich-go "
                                  "needs a full-access key on the destination)",
                    })
                else:
                    results.append({
                        "name": "user.read",
                        "desc": "Required by the immich-go upload engine to connect",
                        "ok": True,
                        "detail": "Verified — destination connection will validate",
                    })

                # ── album.write ──────────────────────────────────────────────
                created_id: str | None = None
                try:
                    resp = await client.post(
                        f"{self.base_url}/api/albums",
                        headers={**self.headers, "Content-Type": "application/json"},
                        json={"albumName": "_immich_sync_permission_test_"},
                    )
                    if resp.status_code == 403:
                        results.append({
                            "name": "album.write",
                            "desc": "Create destination album and add assets to it",
                            "ok": False,
                            "detail": "Permission denied — enable album.write scope on this API key",
                        })
                    else:
                        resp.raise_for_status()
                        created_id = resp.json().get("id")
                        results.append({
                            "name": "album.write",
                            "desc": "Create destination album and add assets to it",
                            "ok": True, "detail": "Test album created and removed successfully",
                        })
                except Exception as exc:
                    results.append({
                        "name": "album.write",
                        "desc": "Create destination album and add assets to it",
                        "ok": False, "detail": str(exc),
                    })
                finally:
                    if created_id:
                        try:
                            await client.delete(
                                f"{self.base_url}/api/albums/{created_id}",
                                headers=self.headers,
                            )
                        except Exception:
                            pass

                # ── asset.write (informational — can't test without uploading) ──
                results.append({
                    "name": "asset.write",
                    "desc": "Upload new photos and videos to this server",
                    "ok": None,
                    "detail": "Cannot be verified without uploading a file — confirmed on first sync",
                })

        return results, albums

    async def download_original(self, asset_id: str, dest_path: str) -> int:
        """
        Stream-download the original file for an asset.
        Returns the number of bytes written.
        Skips download if file already exists with size > 0.
        """
        import aiofiles

        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return os.path.getsize(dest_path)

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        async with httpx.AsyncClient(
            timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
        ) as client:
            async with client.stream(
                "GET",
                f"{self.base_url}/api/assets/{asset_id}/original",
                headers=self.headers,
            ) as resp:
                resp.raise_for_status()
                size = 0
                async with aiofiles.open(dest_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        await f.write(chunk)
                        size += len(chunk)
                return size

    async def bulk_upload_check(self, items: list) -> dict:
        """Ask this server which assets it already holds, keyed by SHA-1 checksum.

        *items* is a list of ``{"id": <caller id>, "checksum": <base64 sha1>}``.
        The base64 checksum is exactly what the Immich asset API returns for an
        asset, and ``/api/assets/bulk-upload-check`` accepts it as-is (it detects
        base64 vs hex by length), so no re-encoding is needed.

        Returns ``{id: {"action": "accept"|"reject", "assetId": <existing id|None>}}``.
        ``reject`` means the server already has that file; ``assetId`` is the id of
        the existing copy (handy for adding it straight to an album).
        """
        if not items:
            return {}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/assets/bulk-upload-check",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"assets": [
                    {"id": it["id"], "checksum": it["checksum"]} for it in items
                ]},
            )
            resp.raise_for_status()
            data = resp.json()
        out: dict = {}
        for r in data.get("results", []):
            out[r.get("id")] = {
                "action": r.get("action"),
                "assetId": r.get("assetId"),
            }
        return out

    async def create_album(self, album_name: str, asset_ids: Optional[list] = None) -> dict:
        """Create an album, optionally seeding it with destination asset IDs."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/albums",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"albumName": album_name, "assetIds": asset_ids or []},
            )
            resp.raise_for_status()
            return resp.json()

    async def add_assets_to_album(self, album_id: str, asset_ids: list) -> int:
        """Add destination asset IDs to an album (idempotent).

        Returns the number now present in the album (newly added + already there).
        Chunks large lists to stay within server request limits.
        """
        if not asset_ids:
            return 0
        present = 0
        async with httpx.AsyncClient(timeout=60.0) as client:
            for i in range(0, len(asset_ids), 500):
                chunk = asset_ids[i:i + 500]
                resp = await client.put(
                    f"{self.base_url}/api/albums/{album_id}/assets",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={"ids": chunk},
                )
                resp.raise_for_status()
                for item in resp.json():
                    # success=True when newly added; error="duplicate" if already in album
                    if item.get("success") or item.get("error") == "duplicate":
                        present += 1
        return present
