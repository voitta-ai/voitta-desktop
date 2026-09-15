"""purge_conversations() deletes every previous-run conversation artefact
and nothing else."""

import paths


def _touch(path, size=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_purge_removes_conversation_files_and_keeps_the_rest(tmp_home):
    logs, state, cache = tmp_home / "logs", tmp_home / "state", tmp_home / "cache" / "tools"
    gone = [
        _touch(logs / "2026-09-14.jsonl"),
        _touch(logs / "conv_abc-123.json"),
        _touch(logs / "conv_abc-123_42.json"),
        _touch(logs / "fail_529_20260914_120000.json"),
        _touch(state / "objects.db"),
        _touch(state / "objects.db-wal"),
        _touch(state / "objects.db-shm"),
    ]
    kept = [
        _touch(logs / "desktop.log"),
        _touch(logs / "desktop.log.1"),
        _touch(logs / "mcp-vim.log"),
        _touch(state / "last_run.json"),
        _touch(cache / "tools.json"),
        _touch(tmp_home / "apps.json"),
    ]

    paths.purge_conversations()

    assert not any(p.exists() for p in gone), [p.name for p in gone if p.exists()]
    assert all(p.exists() for p in kept), [p.name for p in kept if not p.exists()]


def test_purge_on_fresh_tree_is_a_no_op(tmp_home):
    paths.purge_conversations()      # creates the tree, deletes nothing
    assert (tmp_home / "logs").is_dir() and (tmp_home / "state").is_dir()
    paths.purge_conversations()      # idempotent


def test_object_store_starts_empty_after_purge(tmp_home):
    from optimizers.object_store import PersistentObjectStore

    store = PersistentObjectStore(path=paths.OBJECT_STORE_PATH)
    store["h1"] = {"type": "tool_result", "data": "old"}
    assert "h1" in store
    del store

    paths.purge_conversations()

    fresh = PersistentObjectStore(path=paths.OBJECT_STORE_PATH)
    assert fresh.get("h1") is None
