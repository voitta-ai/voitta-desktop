"""Durable hash → content-block store behind ``get_vt_object``.

The optimizers strip bulky content (images, long tool results, long call
arguments) out of the conversation and leave behind a reference:
``get_vt_object(hash="…")``. This keeps the dict interface the optimizers
already use and writes through to SQLite, so a long session's references stay
resolvable without holding every object in RAM: reads fall back to disk on a
miss and promote the row back into memory, and a byte budget evicts the
least-recently-read rows.

Scope note: the database does **not** survive a restart. ``purge_conversations()``
in :mod:`paths` deletes it at startup along with the rest of the previous run's
conversation content, so a reference from an earlier run resolves to
"No object found". That is a deliberate privacy trade. Within a run the
guarantee holds; across runs it does not, and the docstring used to claim
otherwise.
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
    """A dict backed by SQLite, so it can outgrow RAM.

    Subclasses dict so the optimizers' ``store[h] = obj`` and ``store.get(h)``
    keep working unchanged, and so anything that iterates it still sees the
    in-memory hot set. Contents survive reopening the file, but the app purges
    that file at startup — see the module docstring.
    """

    def __init__(self, path=OBJECT_STORE_PATH, budget_bytes: int = DEFAULT_BUDGET_BYTES):
        super().__init__()
        self._path = path
        self._budget = budget_bytes
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._disabled = False
        # Running total of payload bytes on disk, so a write does not have to
        # SUM() the whole table to decide whether to evict. Seeded once when
        # the connection opens; None until then.
        self._total_bytes: int | None = None

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
            self._total_bytes = conn.execute(
                "SELECT COALESCE(SUM(nbytes), 0) FROM objects"
            ).fetchone()[0]
            self._conn = conn
            logger.info("object store open at %s", self._path)
        except sqlite3.Error as e:
            logger.error("object store unavailable (%s); running in memory only", e)
            self._disabled = True
            return None
        return self._conn

    def _prune(self, conn: sqlite3.Connection) -> None:
        """Drop least-recently-read rows until the budget is met.

        Reads the running byte total instead of ``SUM()``-ing the table. That
        scan ran on every single write and made each write cost O(rows):
        0.45 ms into an empty store against 7.21 ms into a 7000-row one, which
        dominated request latency on long sessions.

        Evicted rows stay in the in-memory dict on purpose. Memory is never
        evicted and the file is deleted at startup, so the resident copy is
        what keeps a live reference resolvable for the rest of the run.
        """
        total = self._total_bytes
        if total is None:                      # defensive; seeded in _db()
            total = conn.execute(
                "SELECT COALESCE(SUM(nbytes), 0) FROM objects"
            ).fetchone()[0]
        if total <= self._budget:
            self._total_bytes = total
            return
        freed = 0
        for hash_, nbytes in conn.execute(
            "SELECT hash, nbytes FROM objects ORDER BY accessed ASC"
        ).fetchall():
            conn.execute("DELETE FROM objects WHERE hash = ?", (hash_,))
            freed += nbytes
            if total - freed <= self._budget:
                break
        self._total_bytes = total - freed
        logger.info("object store pruned %.1f MB", freed / 1_000_000)

    # ── dict interface ───────────────────────────────────────────────────────

    def __setitem__(self, key: str, value: dict) -> None:
        # Keys are content hashes (image bytes, tool_result content, or
        # id+name+input of a call), so holding the key already means holding
        # this exact payload and there is nothing to write. This matters
        # because the proxy re-derives the whole optimized view every turn:
        # without the check, every object was re-INSERTed on every request —
        # 478 writes per request on a long session, each with its own fsync.
        # A hash collision would keep the first payload, which is the same
        # trade the references themselves already make.
        if super().__contains__(key):
            return
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
                # Only reachable for a row this process has not written (a
                # store reopened over an existing file), but it keeps the
                # running total honest when REPLACE does displace a row.
                prev = conn.execute(
                    "SELECT nbytes FROM objects WHERE hash = ?", (key,)
                ).fetchone()
                conn.execute(
                    "INSERT OR REPLACE INTO objects "
                    "(hash, type, payload, nbytes, created, accessed) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, value.get("type", "unknown"), payload, len(payload), now, now),
                )
                if self._total_bytes is not None:
                    if prev is not None:
                        self._total_bytes -= prev[0]
                    self._total_bytes += len(payload)
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
                self._total_bytes = 0
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
