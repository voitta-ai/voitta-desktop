"""Durable hash → content-block store behind ``get_vt_object``.

The optimizers strip bulky content (images, long tool results, tool-use
pairs) out of the conversation and leave behind a reference:
``get_vt_object(hash="…")``. Those references live in the transcript, which
outlives the process — but the store used to be a bare module-level dict, so
every restart silently orphaned every reference already in flight. The model
would ask for a hash and be told it does not exist.

This keeps the dict interface the optimizers already use, and writes through
to SQLite so the references stay resolvable across restarts. Reads fall back
to disk on a miss, so memory holds only what this session has touched rather
than the entire history.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

from paths import OBJECT_STORE_PATH, ensure_dirs

logger = logging.getLogger("voitta-desktop.object_store")

# Bytes of stored payload to keep before evicting the least recently read.
# Images dominate; a few hundred MB is many sessions' worth and still small
# next to the disk this ships on.
DEFAULT_BUDGET_BYTES = 512 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    hash     TEXT PRIMARY KEY,
    type     TEXT NOT NULL,
    payload  TEXT NOT NULL,
    nbytes   INTEGER NOT NULL,
    created  REAL NOT NULL,
    accessed REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS objects_accessed ON objects (accessed);
"""


class PersistentObjectStore(dict):
    """A dict whose contents survive a restart.

    Subclasses dict so the optimizers' ``store[h] = obj`` and ``store.get(h)``
    keep working unchanged, and so anything that iterates it still sees the
    in-memory hot set.
    """

    def __init__(self, path=OBJECT_STORE_PATH, budget_bytes: int = DEFAULT_BUDGET_BYTES):
        super().__init__()
        self._path = path
        self._budget = budget_bytes
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._disabled = False

    # ── Backing store ────────────────────────────────────────────────────────

    def _db(self) -> sqlite3.Connection | None:
        """Open the database on first use. Returns None if it cannot be used.

        A broken store must not take the app with it: the optimizers still
        function, the references just stop surviving restarts.
        """
        if self._disabled:
            return None
        if self._conn is not None:
            return self._conn
        try:
            ensure_dirs()
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            logger.info("object store open at %s", self._path)
        except sqlite3.Error as e:
            logger.error("object store unavailable (%s); running in memory only", e)
            self._disabled = True
            return None
        return self._conn

    def _prune(self, conn: sqlite3.Connection) -> None:
        """Drop least-recently-read rows until the budget is met."""
        total = conn.execute("SELECT COALESCE(SUM(nbytes), 0) FROM objects").fetchone()[0]
        if total <= self._budget:
            return
        freed = 0
        for hash_, nbytes in conn.execute(
            "SELECT hash, nbytes FROM objects ORDER BY accessed ASC"
        ).fetchall():
            conn.execute("DELETE FROM objects WHERE hash = ?", (hash_,))
            freed += nbytes
            if total - freed <= self._budget:
                break
        logger.info("object store pruned %.1f MB", freed / 1_000_000)

    # ── dict interface ───────────────────────────────────────────────────────

    def __setitem__(self, key: str, value: dict) -> None:
        super().__setitem__(key, value)
        with self._lock:
            conn = self._db()
            if conn is None:
                return
            try:
                payload = json.dumps(value)
            except (TypeError, ValueError) as e:
                logger.warning("object %s is not JSON-serialisable: %s", key, e)
                return
            now = time.time()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO objects "
                    "(hash, type, payload, nbytes, created, accessed) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, value.get("type", "unknown"), payload, len(payload), now, now),
                )
                self._prune(conn)
                conn.commit()
            except sqlite3.Error as e:
                logger.warning("object store write failed for %s: %s", key, e)

    def _load(self, key: str) -> dict | None:
        """Fetch from disk and promote into memory."""
        with self._lock:
            conn = self._db()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT payload FROM objects WHERE hash = ?", (key,)
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE objects SET accessed = ? WHERE hash = ?", (time.time(), key)
                )
                conn.commit()
                value = json.loads(row[0])
            except (sqlite3.Error, ValueError) as e:
                logger.warning("object store read failed for %s: %s", key, e)
                return None
        super().__setitem__(key, value)
        logger.info("object %s restored from disk", key)
        return value

    def get(self, key: str, default=None):
        value = super().get(key)
        if value is not None:
            return value
        return self._load(key) or default

    def __getitem__(self, key: str):
        try:
            return super().__getitem__(key)
        except KeyError:
            value = self._load(key)
            if value is None:
                raise
            return value

    def __contains__(self, key: object) -> bool:
        if super().__contains__(key):
            return True
        return isinstance(key, str) and self._load(key) is not None

    def clear(self) -> None:
        super().clear()
        with self._lock:
            conn = self._db()
            if conn is None:
                return
            try:
                conn.execute("DELETE FROM objects")
                conn.commit()
            except sqlite3.Error as e:
                logger.warning("object store clear failed: %s", e)

    def stats(self) -> dict:
        """Row count and total payload size, for the Info tab."""
        with self._lock:
            conn = self._db()
            if conn is None:
                return {"count": len(self), "bytes": 0, "persistent": False}
            try:
                count, nbytes = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(nbytes), 0) FROM objects"
                ).fetchone()
            except sqlite3.Error:
                return {"count": len(self), "bytes": 0, "persistent": False}
        return {"count": count, "bytes": nbytes, "persistent": True}
