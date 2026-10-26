"""Redirect every on-disk path into a throwaway tree.

``paths.ROOT`` is resolved at import time, so this has to run before any
application module is imported — which conftest does. Without it the tests
would read and write the developer's real config, logs and object store;
``vt_object_store.clear()`` alone would wipe a live store.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = Path(tempfile.mkdtemp(prefix="voitta-desktop-tests-"))
os.environ["VOITTA_DESKTOP_HOME"] = str(_TMP_HOME)
# Claude Code's own config too (~/.claude.json, read by llmgw/window_header and
# by any real `claude` the code under test might start). On 2026-10-24 a test
# that faked `claude plugin` but not `claude mcp` re-pointed the developer's
# real "voitta" MCP entry at a temp file. With this, a real `claude` run here
# only ever sees a throwaway config.
_TMP_CLAUDE = _TMP_HOME / "claude-config"
_TMP_CLAUDE.mkdir()
os.environ["CLAUDE_CONFIG_DIR"] = str(_TMP_CLAUDE)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import paths  # noqa: E402


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Point the path module at a per-test tree.

    Rebinds the module attributes rather than re-importing, since callers
    captured them at their own import time.
    """
    root = tmp_path / "home"
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setattr(paths, "CONFIG_PATH", root / "apps.json")
    monkeypatch.setattr(paths, "LOG_DIR", root / "logs")
    monkeypatch.setattr(paths, "STATE_DIR", root / "state")
    monkeypatch.setattr(paths, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(paths, "TOOL_CACHE_DIR", root / "cache" / "tools")
    monkeypatch.setattr(paths, "OBJECT_STORE_PATH", root / "state" / "objects.db")
    monkeypatch.setattr(paths, "RUN_MARKER_PATH", root / "state" / "last_run.json")
    monkeypatch.setattr(
        paths, "_ALL_DIRS",
        (root, root / "logs", root / "state", root / "cache", root / "cache" / "tools"),
    )
    return root
