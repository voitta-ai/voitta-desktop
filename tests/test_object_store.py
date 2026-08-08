"""The object store must outlive the process.

Optimizers strip content out of a conversation and leave a
``get_vt_object(hash=...)`` reference behind. Those references sit in a
transcript that survives a restart, so a store that does not survive one
turns every earlier reference into a dead end. These tests pin that.
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


def test_rewrite_replaces_rather_than_duplicates(tmp_path):
    store = _store(tmp_path)
    store["abc123"] = OBJ
    store["abc123"] = {"type": "image", "data": "replaced"}

    assert store.stats()["count"] == 1
    assert _store(tmp_path).get("abc123")["data"] == "replaced"


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
