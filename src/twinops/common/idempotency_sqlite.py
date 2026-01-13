"""SQLite-backed idempotency store for cross-process safety."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class SqliteIdempotencyStore:
    """SQLite store with TTL for idempotency keys."""

    def __init__(self, path: str, ttl_seconds: float = 300.0) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_seconds
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS idempotency ("
            "key TEXT PRIMARY KEY,"
            "expires_at REAL NOT NULL,"
            "value TEXT NOT NULL"
            ")"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON idempotency (expires_at)")
        self._conn.commit()

    def _cleanup(self) -> None:
        now = time.time()
        self._conn.execute("DELETE FROM idempotency WHERE expires_at < ?", (now,))
        self._conn.commit()

    def get(self, key: str) -> Any | None:
        self._cleanup()
        row = self._conn.execute(
            "SELECT value, expires_at FROM idempotency WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        value_json, expires_at = row
        if time.time() > expires_at:
            self._conn.execute("DELETE FROM idempotency WHERE key = ?", (key,))
            self._conn.commit()
            return None
        return json.loads(value_json)

    def set(self, key: str, value: Any) -> None:
        expires_at = time.time() + self._ttl_seconds
        self._conn.execute(
            "REPLACE INTO idempotency (key, expires_at, value) VALUES (?, ?, ?)",
            (key, expires_at, json.dumps(value)),
        )
        self._conn.commit()
