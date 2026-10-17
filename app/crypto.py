"""
Symmetric encryption for secrets stored in the database.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from SECRET_KEY.

WARNING: If SECRET_KEY changes, all stored encrypted values become
         unreadable. You will need to re-enter all API keys in Jobs settings.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _fernet_key(secret_key: str) -> bytes:
    """Derive a 32-byte Fernet-compatible key from an arbitrary string."""
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plain: str, secret_key: str) -> str:
    """Encrypt *plain* for database storage. Raises rather than storing plaintext."""
    if not plain:
        return plain
    return Fernet(_fernet_key(secret_key)).encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(stored: str, secret_key: str) -> str:
    """
    Decrypt a stored secret back to plaintext.

    - Legacy plaintext (saved before encryption existed) is returned unchanged.
    - A real Fernet token that won't decrypt means SECRET_KEY changed: that is
      logged and "" is returned, so callers fail with a clear "key missing"
      error instead of sending ciphertext to Immich as if it were the key.
    """
    if not stored:
        return stored
    try:
        return Fernet(_fernet_key(secret_key)).decrypt(stored.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        if not is_encrypted(stored):
            return stored  # legacy plaintext
        logger.error(
            "A stored secret could not be decrypted — SECRET_KEY has probably "
            "changed. Re-enter the affected API keys / webhook URLs."
        )
        return ""


def is_encrypted(value: str) -> bool:
    """Heuristic: Fernet tokens always start with 'gAAAAA'."""
    return bool(value) and value.startswith("gAAAAA")
