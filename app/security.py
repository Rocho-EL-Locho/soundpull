"""Symmetric encryption for secrets stored at rest (WebDAV passwords).

Uses Fernet with the key from settings.fernet_key. The key must be a urlsafe
base64-encoded 32-byte key (see .env.example for how to generate one).
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    if not settings.fernet_key:
        raise RuntimeError(
            "FERNET_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(settings.fernet_key.encode())


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext secret; returns a token safe to store in the DB."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a token produced by encrypt_secret. Raises on tampering/wrong key."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - defensive
        raise RuntimeError("Could not decrypt stored secret (wrong FERNET_KEY?)") from exc
