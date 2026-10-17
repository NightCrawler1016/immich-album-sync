"""
Configuration backup and restore.

A backup is one UTF-8 JSON document, deliberately decoupled from the database
schema so it stays restorable across releases:

- It names itself ("format") and carries an integer "format_version". Additive
  changes (a new optional field) keep the version: readers ignore fields they
  don't know and default the ones that are missing. Only a change an older
  reader would misread bumps the version — this module must then keep reading
  every older version, and it refuses files newer than it understands instead
  of half-restoring them.
- Field names describe the setting, not the column ("mirror_deletes", not
  "delete_sync"), so renaming a column never changes the file.
- Secrets (API keys, webhook URLs) are never written in plaintext, and never
  with this install's SECRET_KEY — that would make a backup useless after a
  rebuild with a new key. They are encrypted with a key derived from a
  passphrase the user chooses, and every KDF parameter is stored in the file,
  so a release that changes the defaults can still decrypt old backups.
  Without a passphrase the secrets are left out entirely.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from .crypto import decrypt_secret

FORMAT = "immich-album-sync-backup"
FORMAT_VERSION = 1
MIN_PASSPHRASE = 8

_KDF = "pbkdf2-sha256"
_KDF_ITERATIONS = 600_000            # OWASP's PBKDF2-HMAC-SHA256 recommendation
_MAX_KDF_ITERATIONS = 10_000_000     # a crafted file can't make restore spin for hours
_CHECK = b"immich-album-sync"
_NOTIFY_MODES = ("inherit", "off", "custom")
_MAX_TEXT = 1000  # names, URLs, albums, schedules — the widest column is 500


class BackupError(ValueError):
    """The file can't be restored; the message is shown to the user as-is."""


def _fernet(passphrase: str, salt: bytes, iterations: int) -> Fernet:
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)
    return Fernet(base64.urlsafe_b64encode(key))


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def export_config(jobs, settings: dict, secret_key: str, passphrase: str,
                  app_version: str) -> bytes:
    """Serialize every job plus the global notification settings.

    *settings* is the Settings table as {key: value}; stored secrets are
    decrypted with *secret_key* and re-encrypted under *passphrase*.
    """
    fernet = encryption = None
    if passphrase:
        salt = os.urandom(16)
        fernet = _fernet(passphrase, salt, _KDF_ITERATIONS)
        encryption = {
            "cipher": "fernet",
            "kdf": _KDF,
            "iterations": _KDF_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "check": fernet.encrypt(_CHECK).decode("ascii"),
        }

    def secret(stored):
        plain = decrypt_secret(stored, secret_key) if stored else ""
        if not (fernet and plain):
            return None
        return fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def events(csv):
        return [e for e in (csv or "").split(",") if e]

    doc = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": app_version,
        "encryption": encryption,
        "notifications": {
            "enabled": (settings.get("webhook_enabled") or "").lower() == "true",
            "webhook_url": secret(settings.get("webhook_url")),
            "events": events(settings.get("webhook_events")),
        },
        "jobs": [
            {
                "name": job.name,
                "enabled": bool(job.enabled),
                "schedule": job.schedule,
                "source": {
                    "url": job.source_url,
                    "album": job.source_album_name,
                    "api_key": secret(job.source_key),
                },
                "destination": {
                    "url": job.dest_url,
                    "album": job.dest_album_name,
                    "api_key": secret(job.dest_key),
                },
                "mirror_deletes": bool(job.delete_sync),
                "cleanup_cache": bool(job.cleanup_cache),
                "notifications": {
                    "mode": job.notify_override or "inherit",
                    "webhook_url": secret(job.webhook_url),
                    "events": events(job.webhook_events),
                },
            }
            for job in jobs
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def _obj(value, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BackupError(f"Backup file is damaged ({where} is not an object).")
    return value


def _text(obj: dict, key: str, where: str, default=None) -> str:
    value = obj.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise BackupError(f"Backup file is damaged ({where}.{key} is missing).")
    try:
        value.encode("utf-8")  # JSON "\ud800" escapes yield strings SQLite can't store
    except UnicodeEncodeError:
        raise BackupError(f"Backup file is damaged ({where}.{key} is not valid text).")
    if len(value) > _MAX_TEXT:
        raise BackupError(f"Backup file is damaged ({where}.{key} is too long).")
    return value.strip()


def _flag(obj: dict, key: str, default: bool) -> bool:
    value = obj.get(key, default)
    return value if isinstance(value, bool) else default


def _events(obj: dict) -> list:
    value = obj.get("events")
    return [e for e in value if isinstance(e, str)] if isinstance(value, list) else []


def _unlock(encryption, passphrase: str):
    """Return a Fernet for the backup's secrets, or None if it has none."""
    if encryption is None:
        return None
    encryption = _obj(encryption, "encryption")
    if encryption.get("cipher") != "fernet" or encryption.get("kdf") != _KDF:
        raise BackupError("This backup uses an encryption method this version doesn't know. "
                          "Update the app, then restore.")
    iterations = encryption.get("iterations")
    if (not isinstance(iterations, int) or isinstance(iterations, bool)
            or not 1 <= iterations <= _MAX_KDF_ITERATIONS):
        raise BackupError("Backup file is damaged (encryption settings).")
    if not passphrase:
        raise BackupError("This backup is protected — enter the passphrase it was made with.")
    try:
        salt = base64.b64decode(encryption.get("salt") or "", validate=True)
    except (ValueError, TypeError):  # binascii.Error is a ValueError
        raise BackupError("Backup file is damaged (encryption settings).")
    fernet = _fernet(passphrase, salt, iterations)
    try:
        if fernet.decrypt(str(encryption.get("check", "")).encode("ascii")) != _CHECK:
            raise InvalidToken
    except (InvalidToken, UnicodeEncodeError):
        raise BackupError("Wrong passphrase for this backup.")
    return fernet


def import_config(raw: bytes, passphrase: str) -> dict:
    """Parse and validate a backup. Raises BackupError with a user-facing reason.

    Returns plaintext secrets and SyncJob column names:
      {"has_secrets": bool,
       "notifications": {"enabled", "webhook_url", "events"},
       "jobs": [{<SyncJob column>: value, "webhook_events": [..]}, ...]}
    An empty string means "no secret in the backup".
    """
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):  # deep nesting → RecursionError
        raise BackupError("That file isn't an Immich Album Sync backup (not valid JSON).")
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise BackupError("That file isn't an Immich Album Sync backup.")
    version = doc.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BackupError("Backup file is damaged (format_version).")
    if version > FORMAT_VERSION:
        raise BackupError(
            f"This backup was made by a newer version of Immich Album Sync (backup format "
            f"{version}; this version reads up to {FORMAT_VERSION}). Update the app, then restore."
        )

    fernet = _unlock(doc.get("encryption"), passphrase)

    def secret(value) -> str:
        if not fernet or not isinstance(value, str) or not value:
            return ""
        try:
            return fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise BackupError("Backup file is damaged (an encrypted value can't be read).")

    notifications = _obj(doc.get("notifications"), "notifications")
    jobs_in = doc.get("jobs", [])
    if not isinstance(jobs_in, list):
        raise BackupError("Backup file is damaged (jobs is not a list).")

    jobs = []
    for i, job in enumerate(jobs_in, start=1):
        where = f"job {i}"
        job = _obj(job, where)
        source = _obj(job.get("source"), f"{where}.source")
        dest = _obj(job.get("destination"), f"{where}.destination")
        job_notify = _obj(job.get("notifications"), f"{where}.notifications")
        mode = job_notify.get("mode")
        jobs.append({
            "name": _text(job, "name", where),
            "enabled": _flag(job, "enabled", True),
            "schedule": _text(job, "schedule", where, default="0 */6 * * *"),
            "source_url": _text(source, "url", f"{where}.source").rstrip("/"),
            "source_album_name": _text(source, "album", f"{where}.source"),
            "source_key": secret(source.get("api_key")),
            "dest_url": _text(dest, "url", f"{where}.destination").rstrip("/"),
            "dest_album_name": _text(dest, "album", f"{where}.destination"),
            "dest_key": secret(dest.get("api_key")),
            "delete_sync": _flag(job, "mirror_deletes", False),
            "cleanup_cache": _flag(job, "cleanup_cache", False),
            "notify_override": mode if mode in _NOTIFY_MODES else "inherit",
            "webhook_url": secret(job_notify.get("webhook_url")),
            "webhook_events": _events(job_notify),
        })

    return {
        "has_secrets": fernet is not None,
        "notifications": {
            "enabled": _flag(notifications, "enabled", False),
            "webhook_url": secret(notifications.get("webhook_url")),
            "events": _events(notifications),
        },
        "jobs": jobs,
    }
