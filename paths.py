"""Single source of truth for every on-disk location Voitta Desktop uses.

Before this module the app wrote to three unrelated roots with two
spellings of the same name — ``~/.voitta_desktop`` (config),
``~/.voitta-desktop`` (logs) and ``~/.voitta_desktop_cache`` (tool cache).
Everything now lives under one root; :func:`migrate_legacy_dirs` copies
the old locations forward once.

Set ``VOITTA_DESKTOP_HOME`` to relocate the whole tree (used by tests).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("voitta-desktop.paths")

# The one root. Hyphenated, matching the app name and the logger namespace.
ROOT = Path(
    os.environ.get("VOITTA_DESKTOP_HOME") or (Path.home() / ".voitta-desktop")
)

CONFIG_PATH = ROOT / "apps.json"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
CACHE_DIR = ROOT / "cache"
TOOL_CACHE_DIR = CACHE_DIR / "tools"

# Where the object store and the "how did the last run end" marker live.
OBJECT_STORE_PATH = STATE_DIR / "objects.db"
RUN_MARKER_PATH = STATE_DIR / "last_run.json"

# Pre-unification locations. Read once by migrate_legacy_dirs(), then unused.
LEGACY_CONFIG_DIR = Path.home() / ".voitta_desktop"
LEGACY_CONFIG_PATH = LEGACY_CONFIG_DIR / "apps.json"
LEGACY_TOOL_CACHE_DIR = Path.home() / ".voitta_desktop_cache"
LEGACY_SETTINGS_PATH = Path.home() / ".voitta_auth_settings.json"

_ALL_DIRS = (ROOT, LOG_DIR, STATE_DIR, CACHE_DIR, TOOL_CACHE_DIR)


def ensure_dirs() -> None:
    """Create the whole tree. Safe to call repeatedly."""
    for d in _ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def migrate_legacy_dirs() -> None:
    """Copy pre-unification state into ROOT. Idempotent and non-destructive.

    We *copy* rather than move, and never overwrite an existing target, so
    that downgrading to an older build still finds its config where it
    expects it. A failed migration is logged and swallowed — a missing
    tool cache costs one slow refresh, and a missing config falls back to
    defaults, neither of which is worth refusing to start over.
    """
    ensure_dirs()

    if LEGACY_CONFIG_PATH.exists() and not CONFIG_PATH.exists():
        try:
            shutil.copy2(LEGACY_CONFIG_PATH, CONFIG_PATH)
            logger.warning("migrated config %s -> %s", LEGACY_CONFIG_PATH, CONFIG_PATH)
        except OSError as e:
            logger.error("config migration failed (%s); starting from defaults", e)

    if LEGACY_TOOL_CACHE_DIR.is_dir():
        for src in LEGACY_TOOL_CACHE_DIR.glob("*.json"):
            dst = TOOL_CACHE_DIR / src.name
            if dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                logger.warning("tool-cache migration failed for %s: %s", src.name, e)
