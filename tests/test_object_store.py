"""The object store must hold every reference written during a run.

Optimizers strip content out of a conversation and leave a
``get_vt_object(hash=...)`` reference behind. A long session writes far more
than fits in RAM, so the store writes through to SQLite, falls back to disk on
a miss, and evicts by least-recently-read. These tests pin the durability of a
reference against eviction and process-level reopening.

Scope: the app deletes the database at startup (``purge_conversations()``), so
references do not resolve across restarts by design. These tests exercise
reopening directly because that is the mechanism the in-run disk fallback
relies on, not because the shipped app reuses a previous run's file.
"""

import json
import sqlite3

import pytest

from optimizers.object_store import PersistentObjectStore

OBJ = {"type": "image", "data": {"source": {"data": "x" * 100}}}


def _store(tmp_path, **kw):
    return PersistentObjectStore(path=tmp_path / "objects.db", **kw)


def test_survives_a_restart(tmp_path):
    """The whole point: a new process finds what the old one stored."""
    first = _store(tmp_path)
    first["abc123"] = OBJ

    second = _store(tmp_path)          # simulates the restart
    assert second.get("abc123") == OBJ


def test_read_promotes_into_memory(tmp_path):
    _store(tmp_path)["abc123"] = OBJ

    fresh = _store(tmp_path)
    assert dict.get(fresh, "abc123") is None   # not resident yet
    assert fresh.get("abc123") == OBJ
    assert dict.get(fresh, "abc123") == OBJ    # promoted by the read


def test_missing_key_behaves_like_a_dict(tmp_path):
    store = _store(tmp_path)
    assert store.get("nope") is None
    assert store.get("nope", "fallback") == "fallback"
    assert "nope" not in store
    with pytest.raises(KeyError):
        store["nope"]


def test_contains_finds_persisted_keys(tmp_path):
    _store(tmp_path)["abc123"] = OBJ
    assert "abc123" in _store(tmp_path)


def test_clear_wipes_both_layers(tmp_path):
    store = _store(tmp_path)
    store["abc123"] = OBJ
    store.clear()

    assert store.get("abc123") is None
    assert _store(tmp_path).get("abc123") is None


def test_eviction_respects_the_budget(tmp_path):
    """Least-recently-read rows go first, and the budget is actually enforced."""
    store = _store(tmp_path, budget_bytes=2_000)
    payload = {"type": "tool_result", "data": "y" * 400}

    for i in range(20):
        store[f"h{i}"] = payload

    stats = store.stats()
    assert stats["persistent"] is True
    assert stats["bytes"] <= 2_000
    assert stats["count"] < 20            # something was evicted
    assert _store(tmp_path).get("h19") == payload   # newest survived


def test_unserialisable_value_does_not_raise(tmp_path):
    """A bad value costs persistence for that key, not the caller's request."""
    store = _store(tmp_path)
    store["bad"] = {"type": "image", "data": object()}

    assert dict.get(store, "bad") is not None   # in memory, as a plain dict would
    assert _store(tmp_path).get("bad") is None  # but nothing was written


def test_unusable_database_degrades_to_memory(tmp_path):
    """A broken store must not take the optimizers down with it."""
    broken = tmp_path / "objects.db"
    broken.write_text("this is not a database")

    store = PersistentObjectStore(path=broken)
    store["abc123"] = OBJ

    assert store.get("abc123") == OBJ           # still works in memory
    assert store.stats()["persistent"] is False


def test_stats_counts_what_is_on_disk(tmp_path):
    store = _store(tmp_path)
    store["a"] = OBJ
    store["b"] = OBJ

    stats = store.stats()
    assert stats["count"] == 2
    assert stats["bytes"] > 0
    assert stats["persistent"] is True


def test_rewriting_a_held_key_is_a_no_op(tmp_path):
    """Keys are content hashes, so a key already held means the same payload:
    the write is skipped entirely rather than re-INSERTed.

    This is the hot path. The proxy re-derives the optimized view on every
    turn, so without the skip every object was rewritten on every request
    (478 writes/request on a long session, each with a table scan and an
    fsync). A same-key write with *different* content therefore keeps the
    first payload — the same trade the 12-hex-char references already make.
    """
    store = _store(tmp_path)
    store["abc123"] = OBJ

    before = store._db().total_changes
    store["abc123"] = {"type": "image", "data": "replaced"}

    assert store._db().total_changes == before, "the database was written to"
    assert store.stats()["count"] == 1
    assert store["abc123"] == OBJ                       # first payload kept
    assert _store(tmp_path).get("abc123") == OBJ


def test_reopened_store_still_replaces_a_row(tmp_path):
    """The skip is per-process (it consults the resident dict), so a store
    reopened over an existing file still writes — and the running byte total
    must not double-count the row it displaces."""
    _store(tmp_path)["abc123"] = OBJ

    second = _store(tmp_path)
    second["abc123"] = {"type": "image", "data": "y" * 100}

    assert second.stats()["count"] == 1
    assert second.stats()["bytes"] == second._total_bytes


def test_byte_total_tracks_inserts_and_eviction(tmp_path):
    """The running total replaces a SUM() scan per write, so it has to stay
    equal to what the table actually holds — including across an eviction."""
    store = _store(tmp_path, budget_bytes=2_000)
    payload = {"type": "tool_result", "data": "y" * 400}

    for i in range(4):                                  # under budget
        store[f"h{i}"] = payload
    assert store._total_bytes == store.stats()["bytes"]

    for i in range(4, 20):                              # forces eviction
        store[f"h{i}"] = payload
    assert store._total_bytes == store.stats()["bytes"]
    assert store._total_bytes <= 2_000


def test_evicted_object_stays_resolvable_from_memory(tmp_path):
    """Eviction drops the disk row but must leave the resident copy alone.

    Within a run memory is the store of record: it is never evicted and the
    file is deleted at startup. If eviction also dropped the key, the skip
    above would refuse to rewrite it and a live reference in the context would
    stop resolving.
    """
    store = _store(tmp_path, budget_bytes=2_000)
    payload = {"type": "tool_result", "data": "y" * 400}
    for i in range(20):
        store[f"h{i}"] = payload

    assert store.stats()["count"] < 20                  # something was evicted
    evicted = [f"h{i}" for i in range(20) if dict.get(store, f"h{i}") is None]
    assert not evicted, f"evicted from memory as well: {evicted[:3]}"
    assert store.get("h0") == payload                   # oldest still resolves


def test_schema_is_readable_by_plain_sqlite(tmp_path):
    """Nothing exotic on disk — a human debugging a live store can read it."""
    store = _store(tmp_path)
    store["abc123"] = OBJ

    conn = sqlite3.connect(tmp_path / "objects.db")
    row = conn.execute(
        "SELECT type, payload FROM objects WHERE hash = ?", ("abc123",)
    ).fetchone()
    conn.close()

    assert row[0] == "image"
    assert json.loads(row[1]) == OBJ
