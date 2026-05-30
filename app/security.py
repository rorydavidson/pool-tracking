"""Token generation, hashing, and symmetric encryption for credentials.

The encryption key is derived deterministically from ``APP_SECRET`` so that
stored device credentials remain readable across restarts without managing a
separate key file. Rotating ``APP_SECRET`` invalidates stored credentials
(users simply re-enter them) and all active sessions/magic links.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_derive_fernet_key(get_settings().app_secret))


def encrypt_json(data: dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(payload).decode("utf-8")


def decrypt_json(blob: str) -> dict[str, Any]:
    try:
        payload = _fernet().decrypt(blob.encode("utf-8"))
    except InvalidToken as exc:  # pragma: no cover - defensive
        raise ValueError("Could not decrypt stored credentials") from exc
    return json.loads(payload.decode("utf-8"))


def generate_token() -> str:
    """A high-entropy URL-safe token for magic links."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hash a token for at-rest storage (so a DB leak can't be replayed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
