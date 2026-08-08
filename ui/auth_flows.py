"""OAuth + MSAL flows for VoittaDesktopApp.

Pulled out of ``ui/menu.py`` as a mixin so the main class file can stay
focused on orchestration. All methods here are tightly coupled to
``self._auth``, ``self._config``, ``self._app_by_id``, and the menu's
``_update_auth_state``/``_refresh_menu`` callbacks — mixing in is the
mechanically cheapest way to extract them while keeping every internal
``self._foo(...)`` call site unchanged.
"""
from __future__ import annotations

import logging
import traceback

from auth.providers import (
    build_msal_app, do_auth_microsoft, do_auth_google,
    fetch_profile_microsoft, fetch_profile_google,
    do_refresh_microsoft, do_refresh_google,
)
from auth.token_store import load_refresh_token, save_refresh_token
from ui._native import _notify

logger = logging.getLogger("voitta-desktop")

# When a refresh attempt fails on a network error the token is still valid —
# retry soon instead of signing out. Fed through _schedule_refresh, which
# fires `expires_in - 300` seconds out, so 360 ≈ a 60-second retry.
_RETRY_EXPIRES_IN = 360


class AuthFlowsMixin:
    """Mixin: OAuth/MSAL handlers for ``VoittaDesktopApp``.

    Provides ``_rebuild_msal_for_app``, the ``_do_auth*`` dispatchers,
    the token-refresh scheduler/handlers, and ``_deauth_app``.
    """

    # ── Refresh-token persistence ────────────────────────────────────────────

    def _set_refresh_token(self, app_id, backend, token) -> None:
        """Update the in-memory refresh token and mirror it to the Keychain.

        Single writer, so in-memory state and stored state cannot drift.
        """
        state = self._auth.get((app_id, backend))
        if state is not None:
            state["refresh_token"] = token
        save_refresh_token(app_id, backend, token)

    def restore_refresh_tokens(self) -> None:
        """Reload stored refresh tokens and kick off a refresh for each.

        Called once at startup. Without this the app forgot every sign-in on
        every restart and made the user re-authenticate each connected app
        through the browser — the most visible piece of friction there was.
        Access tokens are not stored, so the first refresh is what actually
        brings each app back online.
        """
        restored = 0
        for (app_id, backend), state in self._auth.items():
            token = load_refresh_token(app_id, backend)
            if not token:
                continue
            state["refresh_token"] = token
            restored += 1
            # Refresh immediately rather than on a timer: we have no access
            # token yet, so the app is signed out until this completes.
            self._schedule_refresh(app_id, backend, expires_in=360)
        if restored:
            logger.info("restored %d refresh token(s) from the Keychain", restored)

    # ── MSAL ─────────────────────────────────────────────────────────────────

    def _rebuild_msal_for_app(self, app):
        for backend in app.get("use_for", []):
            state = self._auth.get((app["id"], backend))
            if not state:
                continue
            state["msal_app"] = build_msal_app(app)

    # ── Auth dispatcher ──────────────────────────────────────────────────────

    def _do_auth(self, app_id, backend):
        app = self._app_by_id(app_id)
        if not app:
            return
        if not self._auth_lock.acquire(blocking=False):
            _notify("Voitta Desktop", "Busy", "Another authentication is in progress.")
            return
        try:
            if app["type"] == "microsoft":
                self._do_auth_microsoft(app, backend)
            elif app["type"] == "google":
                self._do_auth_google(app, backend)
        except Exception as e:
            traceback.print_exc()
            _notify("Voitta Desktop", "Error", str(e))
        finally:
            self._auth_lock.release()

    def _do_auth_microsoft(self, app, backend):
        state = self._auth[(app["id"], backend)]
        result = do_auth_microsoft(state["msal_app"], app, backend)
        if not result:
            return
        if "error" in result:
            _notify("Voitta Desktop", app["name"], result["error"])
            return
        if "access_token" in result:
            state["token"] = result["access_token"]
            state["profile"] = fetch_profile_microsoft(state["token"])
            self._schedule_refresh(app["id"], backend, result.get("expires_in", 3600))
            name = (state["profile"] or {}).get("name", "Unknown")
            self._update_auth_state()
            _notify("Voitta Desktop", app["name"], f"Welcome, {name}!")

    def _do_auth_google(self, app, backend):
        result = do_auth_google(app, backend)
        if not result:
            return
        if "error" in result:
            _notify("Voitta Desktop", app["name"], result["error"])
            return
        state = self._auth[(app["id"], backend)]
        state["token"] = result["access_token"]
        self._set_refresh_token(app["id"], backend, result.get("refresh_token"))
        state["profile"] = fetch_profile_google(state["token"])
        self._schedule_refresh(app["id"], backend, result.get("expires_in", 3600))
        name = (state["profile"] or {}).get("name", "Unknown")
        self._update_auth_state()
        _notify("Voitta Desktop", app["name"], f"Welcome, {name}!")

    # ── Token refresh ────────────────────────────────────────────────────────

    def _schedule_refresh(self, app_id, backend, expires_in):
        state = self._auth.get((app_id, backend))
        if not state:
            return
        if state["refresh_timer"]:
            state["refresh_timer"].cancel()
        refresh_in = max(expires_in - 300, 60)
        app = self._app_by_id(app_id)
        if not app:
            return

        if app["type"] == "microsoft":
            refresh = self._do_refresh_microsoft
        elif app["type"] == "google":
            refresh = self._do_refresh_google
        else:
            return

        # A scheduled task on the shared runtime rather than a thread per
        # token. The returned future keeps Timer's .cancel() interface, so
        # the reschedule path above is unchanged.
        from runtime import runtime
        state["refresh_timer"] = runtime.call_later(refresh_in, refresh, app_id, backend)

    def _do_refresh_microsoft(self, app_id, backend):
        state = self._auth.get((app_id, backend))
        if not state:
            return
        app = self._app_by_id(app_id)
        if not app:
            return
        try:
            result = do_refresh_microsoft(state["msal_app"], app, backend)
        except Exception as e:
            # MSAL propagates connection errors from requests. These timer
            # threads have no other exception handler — an uncaught raise
            # kills the thread and refresh is never rescheduled.
            logger.warning("microsoft refresh (%s/%s) network error: %s — retrying in 60s",
                           app_id, backend, e)
            self._schedule_refresh(app_id, backend, _RETRY_EXPIRES_IN)
            return
        if result and "access_token" in result:
            state["token"] = result["access_token"]
            self._schedule_refresh(app_id, backend, result.get("expires_in", 3600))
        else:
            state["token"] = None
            state["profile"] = None
            self._update_auth_state()

    def _do_refresh_google(self, app_id, backend):
        state = self._auth.get((app_id, backend))
        if not state or not state["refresh_token"]:
            return
        app = self._app_by_id(app_id)
        if not app:
            return
        try:
            result = do_refresh_google(app, state["refresh_token"])
        except Exception as e:
            logger.warning("google refresh (%s/%s) network error: %s — retrying in 60s",
                           app_id, backend, e)
            self._schedule_refresh(app_id, backend, _RETRY_EXPIRES_IN)
            return
        if result:
            state["token"] = result["access_token"]
            if "refresh_token" in result:
                self._set_refresh_token(app_id, backend, result["refresh_token"])
            self._schedule_refresh(app_id, backend, result.get("expires_in", 3600))
        else:
            state["token"] = None
            self._set_refresh_token(app_id, backend, None)
            state["profile"] = None
            self._update_auth_state()

    # ── Deauth ───────────────────────────────────────────────────────────────

    def _deauth_app(self, app_id, backend=None):
        app = self._app_by_id(app_id)
        name = app["name"] if app else app_id
        backends = [backend] if backend else [b for b in (app or {}).get("use_for", [])]
        for b in backends:
            state = self._auth.get((app_id, b))
            if not state:
                continue
            if state["refresh_timer"]:
                state["refresh_timer"].cancel()
                state["refresh_timer"] = None
            if state["msal_app"]:
                for account in state["msal_app"].get_accounts():
                    state["msal_app"].remove_account(account)
            state["token"] = None
            self._set_refresh_token(app_id, b, None)
            state["profile"] = None
        _notify("Voitta Desktop", name, "Signed out.")
        self._update_auth_state()
