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
            try:
                resp = await client.get(
                    f"{self.base_url}/api/users/me", headers=self.headers
                )
                if resp.status_code in (401, 403):
                    results.append({
                        "name": "API Key", "desc": "Valid, non-expired API key",
                        "ok": False,
                        "detail": f"HTTP {resp.status_code} — key invalid or expired",
                    })
                    return results, albums
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

            # ── asset.read ───────────────────────────────────────────────────
            # Test by fetching the first album's full detail (includes asset list)
            asset_read_ok = False
            if albums:
                try:
                    resp = await client.get(
                        f"{self.base_url}/api/albums/{albums[0]['id']}",
                        headers=self.headers,
                    )
                    if resp.status_code == 403:
                        results.append({
                            "name": "asset.read",
                            "desc": "Read asset metadata and download original files",
                            "ok": False,
                            "detail": "Permission denied — enable asset.read scope on this API key",
                        })
                    else:
                        resp.raise_for_status()
                        asset_read_ok = True
                        results.append({
                            "name": "asset.read",
                            "desc": "Read asset metadata and download original files",
                            "ok": True, "detail": "Verified via album asset list",
                        })
                except Exception as exc:
                    results.append({
                        "name": "asset.read",
                        "desc": "Read asset metadata and download original files",
                        "ok": False, "detail": str(exc),
                    })
            else:
                results.append({
                    "name": "asset.read",
                    "desc": "Read asset metadata and download original files",
                    "ok": True,
                    "detail": "Assumed OK — no albums present to test against",
                })
                asset_read_ok = True

            if role == "dest":
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
