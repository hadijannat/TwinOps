"""HMAC signing utilities for service-to-service auth."""

from __future__ import annotations

import hashlib
import hmac


def build_message(timestamp: str, method: str, path: str, body: bytes) -> bytes:
    """Build an HMAC message payload."""
    return b".".join(
        [
            timestamp.encode("utf-8"),
            method.upper().encode("utf-8"),
            path.encode("utf-8"),
            body,
        ]
    )


def sign(secret: str, message: bytes) -> str:
    """Create a hex-encoded HMAC signature."""
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(secret: str, message: bytes, signature: str) -> bool:
    """Verify HMAC signature in constant time."""
    expected = sign(secret, message)
    return hmac.compare_digest(expected, signature)
