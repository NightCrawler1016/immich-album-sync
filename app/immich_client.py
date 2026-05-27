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
