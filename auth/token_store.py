"""Refresh-token persistence, backed by the macOS Keychain.

Access tokens are short-lived and deliberately not persisted. Refresh tokens
are what let a restart skip the browser round-trip — without them, every
launch demanded a fresh interactive sign-in for every connected app, which
is the single most visible piece of friction in the product.

The Keychain rather than a file in our own tree: these are long-lived
credentials for the user's Google and Microsoft accounts, they are read at
most a handful of times per launch, and ``security(1)`` is always present on
macOS with no dependency to bundle. Every operation degrades to a no-op if
the Keychain is unavailable, so a locked or absent keychain costs the user a
re-login and nothing worse.
"""

from __future__ import annotations

import logging
import platform
import subprocess

logger = logging.getLogger("voitta-desktop.tokens")

SERVICE = "ai.voitta.voitta-desktop"
_TIMEOUT_S = 5


def _available() -> bool:
    return platform.system() == "Darwin"


def _account(app_id: str, backend: str) -> str:
    return f"{app_id}:{backend}"


def save_refresh_token(app_id: str, backend: str, token: str | None) -> None:
    """Store (or clear) the refresh token for one app/backend pair."""
    if not _available():
        return
    account = _account(app_id, backend)

    if not token:
        delete_refresh_token(app_id, backend)
        return

    try:
        # -U updates in place if the item already exists. The token goes via
        # -w on argv, which is visible to other processes on this machine for
        # the lifetime of the call; `security` offers no stdin path for this,
        # and anything able to read our argv can already read our memory.
        subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", SERVICE, "-a", account, "-w", token],
            check=True, capture_output=True, timeout=_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("could not save refresh token for %s: %s", account, e)


def load_refresh_token(app_id: str, backend: str) -> str | None:
    """Return the stored refresh token, or None if there isn't a usable one."""
    if not _available():
        return None
    account = _account(app_id, backend)
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("could not read refresh token for %s: %s", account, e)
        return None

    if result.returncode != 0:
        return None  # no such item — first run for this app
    return result.stdout.strip() or None


def delete_refresh_token(app_id: str, backend: str) -> None:
    """Remove the stored refresh token. Silent if there wasn't one."""
    if not _available():
        return
    account = _account(app_id, backend)
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
            capture_output=True, timeout=_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("could not delete refresh token for %s: %s", account, e)
