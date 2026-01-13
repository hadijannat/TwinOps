"""In-memory idempotency store with TTL eviction."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class IdempotencyStore:
    """Stores results by idempotency key with TTL."""

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 1000):
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        """Get cached value if not expired."""
        now = time.time()
        entry = self._entries.get(key)
        if not entry:
            return None

        expires_at, value = entry
        if now > expires_at:
            self._entries.pop(key, None)
            return None

        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        """Store a value with TTL."""
        expires_at = time.time() + self._ttl_seconds
        self._entries[key] = (expires_at, value)
        self._entries.move_to_end(key)

        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
