"""
Outbound webhook notifications for sync runs.

One global webhook (configured on the Settings page) can be overridden per job.
The same generic JSON payload is sent to any URL; when the URL is a Discord or
Slack incoming webhook it is auto-formatted into that service's message shape.

Design rules:
- A notification failure must NEVER affect a sync. Everything is wrapped so the
  worst case is a logged warning.
- Payloads never contain secrets: no API keys, no server URLs, no webhook URL.
  Only the job name, album names, run counts, status, timestamps, and (for
  failures) the error message are included.
"""
import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlparse

import httpx

from .crypto import decrypt_secret

logger = logging.getLogger(__name__)

# Read the same key the rest of the app uses (startup already validated it).
_SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-a-unique-random-32-64-char-string")

NOTIFY_TIMEOUT = float(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "10"))

# Canonical, ordered event names the UI offers as checkboxes.
EVENTS = ("start", "success", "partial", "failed")

_STATUS_TEXT = {
    "start": "Sync started",
    "success": "Sync completed",
    "partial": "Sync completed with errors",
    "failed": "Sync failed",
}
_EMOJI = {"start": "🔄", "success": "✅", "partial": "⚠️", "failed": "❌"}
_DISCORD_COLOR = {
    "start": 0x3498DB,    # blue
    "success": 0x2ECC71,  # green
    "partial": 0xE67E22,  # orange
    "failed": 0xE74C3C,   # red
}
_SLACK_COLOR = {
    "start": "#3498db",
    "success": "#2ecc71",
    "partial": "#e67e22",
    "failed": "#e74c3c",
}

# Map a run's final status to the notification event name.
STATUS_TO_EVENT = {"success": "success", "partial": "partial", "failed": "failed"}


def parse_events(csv: Optional[str]) -> set:
    """Parse a stored 'start,failed' CSV into a set of valid event names."""
    if not csv:
        return set()
    return {e.strip().lower() for e in csv.split(",") if e.strip().lower() in EVENTS}


def is_valid_webhook_url(url: str) -> bool:
    """Accept only absolute http(s) URLs — the only schemes we POST to."""
    if not url:
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def events_to_csv(events) -> str:
    """Serialize an iterable of event names to a canonical, de-duped CSV."""
    chosen = {e for e in events if e in EVENTS}
    return ",".join(e for e in EVENTS if e in chosen)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_discord(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("discord.com") or host.endswith("discordapp.com")


def _is_slack(url: str) -> bool:
    return urlparse(url).netloc.lower().endswith("hooks.slack.com")


def _counts(results: Optional[dict]) -> dict:
    r = results or {}
    return {
        "found": r.get("assets_found", 0),
        "downloaded": r.get("assets_downloaded", 0),
        "uploaded": r.get("assets_uploaded", 0),
        "skipped": r.get("assets_skipped", 0),
        "failed": r.get("assets_failed", 0),
    }


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #

def _generic_payload(event, job, results, run) -> dict:
    payload = {
        "service": "immich-album-sync",
        "event": event,
        "status": (results or {}).get("status", event),
        "job": job.name,
        "source_album": job.source_album_name,
        "dest_album": job.dest_album_name,
        "message": f"{job.name}: {_STATUS_TEXT.get(event, event)}",
        "timestamp": _now_iso(),
    }
    if results is not None:
        payload["counts"] = _counts(results)
        if results.get("error_message") and event in ("failed", "partial"):
            payload["error"] = str(results["error_message"])
    if run is not None:
        if getattr(run, "started_at", None):
            payload["started_at"] = run.started_at.isoformat()
        if getattr(run, "finished_at", None):
            payload["finished_at"] = run.finished_at.isoformat()
        dur = getattr(run, "duration_seconds", None)
        if dur is not None:
            payload["duration_seconds"] = dur
    return payload


def _discord_payload(event, job, results, run) -> dict:
    title = f"{_EMOJI.get(event, '')} {job.name} — {_STATUS_TEXT.get(event, event)}".strip()
    fields = []
    if results is not None:
        c = _counts(results)
        fields.append({"name": "Uploaded", "value": str(c["uploaded"]), "inline": True})
        fields.append({"name": "Skipped", "value": str(c["skipped"]), "inline": True})
        fields.append({"name": "Failed", "value": str(c["failed"]), "inline": True})
    fields.append({"name": "Source album", "value": (job.source_album_name or "—"), "inline": True})
    fields.append({"name": "Dest album", "value": (job.dest_album_name or "—"), "inline": True})

    embed = {
        "title": title[:256],
        "color": _DISCORD_COLOR.get(event, 0x95A5A6),
        "fields": fields,
        "footer": {"text": "immich-album-sync"},
        "timestamp": _now_iso(),
    }
    if results is not None and results.get("error_message") and event in ("failed", "partial"):
        embed["description"] = "```\n" + str(results["error_message"])[:1000] + "\n```"
    return {"embeds": [embed]}


def _slack_payload(event, job, results, run) -> dict:
    lines = [f"*{job.name}* — {_STATUS_TEXT.get(event, event)}"]
    if results is not None:
        c = _counts(results)
        lines.append(f"Uploaded {c['uploaded']} · Skipped {c['skipped']} · Failed {c['failed']}")
    lines.append(f"{job.source_album_name} → {job.dest_album_name}")
    if results is not None and results.get("error_message") and event in ("failed", "partial"):
        lines.append("Error: " + str(results["error_message"])[:500])
    return {
        "attachments": [
            {
                "color": _SLACK_COLOR.get(event, "#95a5a6"),
                "fallback": f"{job.name}: {_STATUS_TEXT.get(event, event)}",
                "text": "\n".join(lines),
            }
        ]
    }


def build_payload(url, event, job, results=None, run=None) -> dict:
    if _is_discord(url):
        return _discord_payload(event, job, results, run)
    if _is_slack(url):
        return _slack_payload(event, job, results, run)
    return _generic_payload(event, job, results, run)


# --------------------------------------------------------------------------- #
# Config resolution
# --------------------------------------------------------------------------- #

def _resolve_target(global_settings: dict, job) -> Optional[tuple]:
    """Return (url, events_set) for *job*, or None if it should not notify.

    global_settings is a {key: value} dict of the webhook_* Settings rows.
    """
    mode = (getattr(job, "notify_override", None) or "inherit").lower()
    if mode == "off":
        return None

    if mode == "custom":
        url_enc = getattr(job, "webhook_url", None)
        url = decrypt_secret(url_enc, _SECRET_KEY) if url_enc else ""
        events = parse_events(getattr(job, "webhook_events", None))
    else:  # inherit
        if (global_settings.get("webhook_enabled") or "").lower() != "true":
            return None
        url = decrypt_secret(global_settings.get("webhook_url") or "", _SECRET_KEY)
        events = parse_events(global_settings.get("webhook_events"))

    if not url or not events:
        return None
    return url, events


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _safe_error(exc: Exception) -> str:
    """A log/UI-safe description of a delivery error.

    httpx exceptions stringify with the full request URL, which for Discord/Slack
    webhooks embeds the auth token — so we return only the exception type and any
    HTTP status code, never the raw message/URL.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return type(exc).__name__ + (f" (HTTP {status})" if status else "")


async def _post(url: str, payload: dict) -> None:
    async with httpx.AsyncClient(timeout=NOTIFY_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


async def notify(db, job, event: str, results: Optional[dict] = None, run=None) -> None:
    """Fire the webhook for *event* if configured. Never raises."""
    try:
        from .models import Settings

        rows = db.query(Settings).filter(Settings.key.like("webhook_%")).all()
        global_settings = {s.key: s.value for s in rows}

        target = _resolve_target(global_settings, job)
        if not target:
            return
        url, events = target
        if event not in events:
            return

        payload = build_payload(url, event, job, results=results, run=run)
        await _post(url, payload)
        logger.info(f"Notification sent ({event}) for job {getattr(job, 'id', '?')}")
    except Exception as exc:
        # Never log the raw exception — httpx errors embed the full request URL,
        # and a Discord/Slack webhook URL carries its auth token in that URL.
        logger.warning(
            f"Notification failed ({event}) for job {getattr(job, 'id', '?')}: "
            f"{_safe_error(exc)}"
        )


async def send_test(url: str) -> tuple:
    """Send a sample notification to *url*. Returns (ok: bool, error: str|None)."""
    if not url:
        return False, "No webhook URL provided."
    fake_job = SimpleNamespace(
        id="test",
        name="Immich Album Sync (test)",
        source_album_name="Example Source Album",
        dest_album_name="Example Destination Album",
    )
    fake_results = {
        "status": "success",
        "assets_found": 3,
        "assets_downloaded": 2,
        "assets_uploaded": 2,
        "assets_skipped": 1,
        "assets_failed": 0,
        "error_message": None,
    }
    try:
        payload = build_payload(url, "success", fake_job, results=fake_results, run=None)
        await _post(url, payload)
        return True, None
    except Exception as exc:
        # Sanitized — the raw error would embed the (secret) webhook URL.
        return False, _safe_error(exc)
