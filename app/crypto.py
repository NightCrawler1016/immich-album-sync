"""
Symmetric encryption for secrets stored in the database.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from SECRET_KEY.

WARNING: If SECRET_KEY changes, all stored encrypted values become
         unreadable. You will need to re-enter all API keys in Jobs settings.
"""
import base64
import hashlib
import logging

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False
    logger.warning(
        "cryptography package not installed — API keys stored unencrypted. "
        "Rebuild the Docker image to install it."
    )


def _fernet_key(secret_key: str) -> bytes:
    """Derive a 32-byte Fernet-compatible key from an arbitrary string."""
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plain: str, secret_key: str) -> str:
    """Encrypt *plain* for database storage. Returns unchanged if crypto unavailable."""
    if not plain or not _CRYPTO_AVAILABLE:
        return plain
    try:
        return Fernet(_fernet_key(secret_key)).encrypt(plain.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        logger.error(f"Secret encryption failed: {exc}")
        return plain


def decrypt_secret(stored: str, secret_key: str) -> str:
    """
    Decrypt a stored secret back to plaintext.

    Gracefully falls back to returning *stored* unchanged when:
    - The value was saved before encryption was introduced (legacy plaintext).
    - The SECRET_KEY changed since the value was encrypted.
    """
    if not stored or not _CRYPTO_AVAILABLE:
        return stored
    try:
        return Fernet(_fernet_key(secret_key)).decrypt(stored.encode("utf-8")).decode("utf-8")
    except Exception:
        # Not a Fernet token — treat as legacy plaintext
        return stored


def is_encrypted(value: str) -> bool:
    """Heuristic: Fernet tokens always start with 'gAAAAA'."""
    return bool(value) and value.startswith("gAAAAA")
