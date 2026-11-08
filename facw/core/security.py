"""Password hashing, secret sealing and stream-token signing.

* Passwords: PBKDF2-HMAC-SHA256 with per-user random salt and a high iteration
  count.  Stored as ``pbkdf2_sha256$<iters>$<salt-b64>$<hash-b64>``.  Never
  stored in clear text and never reversible (spec 6.5 / pitfall 21).
* Sealing: biometric vectors and camera passwords are sealed with
  AES-256-GCM using a key derived from a per-installation secret file (spec
  9.3 / 6.7: biometric data must not be readable in the clear).
* Stream tokens: short-lived HMAC tokens so an ``<img>`` tag can authenticate
  the MJPEG stream without putting the session cookie in a URL.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PBKDF2_ITERATIONS = 210_000


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 4:
        raise ValueError("password too short")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Sealing (AES-GCM)
# --------------------------------------------------------------------------
class Sealer:
    """Encrypt/decrypt small payloads with a per-installation key."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("key must be at least 32 bytes")
        self._aead = AESGCM(key[:32])
        self._key = key[:32]

    @classmethod
    def from_path(cls, secret_file: Path) -> "Sealer":
        if secret_file.exists():
            raw = secret_file.read_bytes()
        else:
            raw = os.urandom(64)
            secret_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = secret_file.with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, secret_file)
            try:  # best effort hardening on posix
                os.chmod(secret_file, 0o600)
            except OSError:
                pass
        return cls(hashlib.sha256(raw + b"|facw-seal-v1").digest())

    def seal_bytes(self, data: bytes, aad: bytes = b"") -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, data, aad)
        return base64.b64encode(nonce + ct).decode()

    def open_bytes(self, token: str, aad: bytes = b"") -> bytes:
        raw = base64.b64decode(token)
        nonce, ct = raw[:12], raw[12:]
        return self._aead.decrypt(nonce, ct, aad)

    def sign_token(self, purpose: str, subject: str, ttl_seconds: int = 86_400) -> str:
        """Signed, expiring token: ``purpose.subject.expiry.sig``."""
        exp = int(time.time()) + ttl_seconds
        body = f"{purpose}.{subject}.{exp}"
        sig = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{body}.{sig}"

    def verify_token(self, token: str, purpose: str) -> Optional[str]:
        """Return the subject if the token is valid and unexpired."""
        try:
            p, subject, exp, sig = token.rsplit(".", 3)
            if p != purpose:
                return None
            body = f"{p}.{subject}.{exp}"
            want = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()[:32]
            if not hmac.compare_digest(want, sig):
                return None
            if int(exp) < time.time():
                return None
            return subject
        except (ValueError, TypeError):
            return None


def random_id(n: int = 8) -> str:
    return secrets.token_hex(n // 2)
