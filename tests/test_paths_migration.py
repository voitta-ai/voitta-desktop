"""Migration from the three pre-unification directories.

The app used to write to ``~/.voitta_desktop`` (config), ``~/.voitta-desktop``
(logs) and ``~/.voitta_desktop_cache`` (tool cache) — two spellings of the
same name across three roots. Consolidating them is only safe if the move
never destroys or overwrites a user's existing config.
"""

import json

import paths


def _legacy(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "legacy_config"
    cache_dir = tmp_path / "legacy_cache"
    cfg_dir.mkdir()
    cache_dir.mkdir()
    monkeypatch.setattr(paths, "LEGACY_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(paths, "LEGACY_CONFIG_PATH", cfg_dir / "apps.json")
    monkeypatch.setattr(paths, "LEGACY_TOOL_CACHE_DIR", cache_dir)
    return cfg_dir, cache_dir


def test_ensure_dirs_creates_the_whole_tree(tmp_home):
    paths.ensure_dirs()
    for d in (paths.ROOT, paths.LOG_DIR, paths.STATE_DIR,
              paths.CACHE_DIR, paths.TOOL_CACHE_DIR):
        assert d.is_dir()


def test_config_is_copied_forward(tmp_home, tmp_path, monkeypatch):
    cfg_dir, _ = _legacy(monkeypatch, tmp_path)
    (cfg_dir / "apps.json").write_text(json.dumps({"apps": ["mine"]}))

    paths.migrate_legacy_dirs()

    assert json.loads(paths.CONFIG_PATH.read_text()) == {"apps": ["mine"]}


def test_legacy_config_is_left_in_place(tmp_home, tmp_path, monkeypatch):
    """Copy, not move — downgrading to an older build must still work."""
    cfg_dir, _ = _legacy(monkeypatch, tmp_path)
    (cfg_dir / "apps.json").write_text("{}")

    paths.migrate_legacy_dirs()

    assert (cfg_dir / "apps.json").exists()


def test_existing_config_is_never_overwritten(tmp_home, tmp_path, monkeypatch):
    """The single most destructive thing this could do. It must not."""
    cfg_dir, _ = _legacy(monkeypatch, tmp_path)
    (cfg_dir / "apps.json").write_text(json.dumps({"which": "legacy"}))
    paths.ensure_dirs()
    paths.CONFIG_PATH.write_text(json.dumps({"which": "current"}))

    paths.migrate_legacy_dirs()

    assert json.loads(paths.CONFIG_PATH.read_text()) == {"which": "current"}


def test_tool_cache_is_copied_forward(tmp_home, tmp_path, monkeypatch):
    _, cache_dir = _legacy(monkeypatch, tmp_path)
    (cache_dir / "vim_tools.json").write_text("[]")

    paths.migrate_legacy_dirs()

    assert (paths.TOOL_CACHE_DIR / "vim_tools.json").exists()


def test_migration_is_idempotent(tmp_home, tmp_path, monkeypatch):
    """Runs on every launch, so a second run must change nothing."""
    cfg_dir, cache_dir = _legacy(monkeypatch, tmp_path)
    (cfg_dir / "apps.json").write_text(json.dumps({"apps": []}))
    (cache_dir / "vim_tools.json").write_text("[]")

    paths.migrate_legacy_dirs()
    paths.CONFIG_PATH.write_text(json.dumps({"edited": True}))
    paths.migrate_legacy_dirs()

    assert json.loads(paths.CONFIG_PATH.read_text()) == {"edited": True}


def test_no_legacy_state_is_not_an_error(tmp_home, tmp_path, monkeypatch):
    """Fresh install: nothing to migrate, nothing to complain about."""
    monkeypatch.setattr(paths, "LEGACY_CONFIG_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(paths, "LEGACY_TOOL_CACHE_DIR", tmp_path / "absent")

    paths.migrate_legacy_dirs()

    assert paths.ROOT.is_dir()
    assert not paths.CONFIG_PATH.exists()
