"""OAuth + MSAL flows for VoittaDesktopApp.

Pulled out of ``ui/menu.py`` as a mixin so the main class file can stay
focused on orchestration. All methods here are tightly coupled to
``self._auth``, ``self._config``, ``self._app_by_id``, and the menu's
``_update_auth_state``/``_refresh_menu`` callbacks — mixing in is the
mechanically cheapest way to extract them while keeping every internal
``self._foo(...)`` call site unchanged.
"""
from __future__ import annotations

import threading
import traceback

from auth.providers import (
    build_msal_app, do_auth_microsoft, do_auth_google,
    fetch_profile_microsoft, fetch_profile_google,
    do_refresh_microsoft, do_refresh_google,
)
from ui._native import _notify


class AuthFlowsMixin:
    """Mixin: OAuth/MSAL handlers for ``VoittaDesktopApp``.

    Provides ``_rebuild_msal_for_app``, the ``_do_auth*`` dispatchers,
    the token-refresh scheduler/handlers, and ``_deauth_app``.
    """

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
        state["refresh_token"] = result.get("refresh_token")
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
            timer = threading.Timer(refresh_in, self._do_refresh_microsoft, args=(app_id, backend))
        elif app["type"] == "google":
            timer = threading.Timer(refresh_in, self._do_refresh_google, args=(app_id, backend))
        else:
            return

        timer.daemon = True
        timer.start()
        state["refresh_timer"] = timer

    def _do_refresh_microsoft(self, app_id, backend):
        state = self._auth.get((app_id, backend))
        if not state:
            return
        app = self._app_by_id(app_id)
        if not app:
            return
        result = do_refresh_microsoft(state["msal_app"], app, backend)
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
        result = do_refresh_google(app, state["refresh_token"])
        if result:
            state["token"] = result["access_token"]
            if "refresh_token" in result:
                state["refresh_token"] = result["refresh_token"]
            self._schedule_refresh(app_id, backend, result.get("expires_in", 3600))
        else:
            state["token"] = None
            state["refresh_token"] = None
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
            state["refresh_token"] = None
            state["profile"] = None
        _notify("Voitta Desktop", name, "Signed out.")
        self._update_auth_state()
