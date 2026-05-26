"""Menu construction + auth-state refresh for VoittaDesktopApp.

Mixin pulled out of menu.py. Owns the static menu shape (auth, LLM proxy,
conversations, bottom buttons) and the live refresh of auth-related
titles. Host attributes consumed:

  self.mcp_proxy_port, self.llm_proxy_port, self._config, self._auth,
  self._optimizer_pipeline, self._noop, self._active_app,
  self._menu_items, self._conv_menus,
  self._is_active, self._deauth_app, self._do_auth, self._set_active,
  self._toggle_optimizer, self._show_llm_tools_status, self.show_settings,
  self.show_about, self.show_help, self._quit.

Sets the host attributes ``self._llm_status``, ``self._optimize_toggle``,
``self._status_item``, ``self._conv_header`` while building.
"""
from __future__ import annotations

import threading

import rumps

from config import apps_for_backend


class MenuBuilderMixin:
    """Mixin: top-level menu shape + per-app titles."""

    # ── Menu ─────────────────────────────────────────────────────────────────

    def _build_menu(self):
        menu_list = []

        # ── Auth section ─────────────────────────────────────────────────────
        auth_header = rumps.MenuItem("── Auth ─────────────────────────────────")
        auth_header.set_callback(self._noop)
        menu_list.append(auth_header)

        mcp_item = rumps.MenuItem(f"MCP  http://127.0.0.1:{self.mcp_proxy_port}/mcp")
        mcp_item.set_callback(self._noop)
        self._menu_items["mcp_proxy"] = mcp_item
        menu_list.append(mcp_item)

        for backend, label in (("rag", "RAG (voitta.ai)"), ("google_workspace", "Google Workspace")):
            backend_apps = apps_for_backend(self._config, backend)
            if backend == "google_workspace":
                backend_apps = [a for a in backend_apps if a["type"] != "microsoft"]
            if not backend_apps:
                continue
            header = rumps.MenuItem(label)
            header.set_callback(self._noop)
            menu_list.append(header)

            by_type = {}
            for app in backend_apps:
                by_type.setdefault(app["type"], []).append(app)

            for app_type, apps_of_type in by_type.items():
                if len(apps_of_type) == 1:
                    app = apps_of_type[0]
                    item = rumps.MenuItem("", callback=self._make_app_toggle(app["id"], backend))
                    self._menu_items[f"{backend}:{app['id']}"] = item
                    menu_list.append(item)
                else:
                    type_label = "Microsoft" if app_type == "microsoft" else "Google"
                    parent = rumps.MenuItem(type_label)
                    for app in apps_of_type:
                        sub_item = rumps.MenuItem(
                            "", callback=self._make_app_activate(backend, app["id"])
                        )
                        self._menu_items[f"{backend}:{app['id']}"] = sub_item
                        parent.add(sub_item)
                    menu_list.append(parent)
                    self._menu_items[f"{backend}:{app_type}:parent"] = parent

        # Jira
        jira_header = rumps.MenuItem("Jira")
        jira_header.set_callback(self._noop)
        menu_list.append(jira_header)
        jira_item = rumps.MenuItem("")
        jira_item.set_callback(self._noop)
        self._menu_items["jira"] = jira_item
        menu_list.append(jira_item)

        menu_list.append(None)

        # ── LLM Proxy section ────────────────────────────────────────────────
        proxy_header = rumps.MenuItem("── LLM Proxy ─────────────────────────")
        proxy_header.set_callback(self._noop)
        menu_list.append(proxy_header)

        self._llm_status = rumps.MenuItem(f"  http://127.0.0.1:{self.llm_proxy_port}")
        self._llm_status.set_callback(self._noop)
        menu_list.append(self._llm_status)

        self._optimize_toggle = rumps.MenuItem("  Optimize context", callback=self._toggle_optimizer)
        self._optimize_toggle.state = self._optimizer_pipeline.enabled
        menu_list.append(self._optimize_toggle)

        self._status_item = rumps.MenuItem("  LLM Tools Status", callback=self._show_llm_tools_status)
        menu_list.append(self._status_item)

        menu_list.append(None)

        # ── Conversations section ────────────────────────────────────────────
        # Header is always visible; the section sits empty until live
        # conversations stream in via _update_conversations.
        self._conv_header = rumps.MenuItem("── Conversations ─────────────────────────")
        self._conv_header.set_callback(self._noop)
        menu_list.append(self._conv_header)

        menu_list.append(None)

        # ── Bottom ───────────────────────────────────────────────────────────
        menu_list.append(rumps.MenuItem("About Voitta Desktop", callback=self.show_about))
        menu_list.append(rumps.MenuItem("Settings", callback=self.show_settings))
        menu_list.append(rumps.MenuItem("Help", callback=self.show_help))
        menu_list.append(rumps.MenuItem("Quit", callback=self._quit))

        self.menu = menu_list

    def _rebuild_menu(self):
        self._menu_items = {}
        self._conv_menus = {}
        self.menu.clear()
        self._build_menu()

    # ── Auth menu helpers ────────────────────────────────────────────────────

    def _app_menu_title(self, app, backend, is_submenu=False):
        state = self._auth.get((app["id"], backend), {})
        connected = state.get("token") is not None
        dot = "●" if connected else "○"
        profile = state.get("profile") or {}
        right = profile.get("email", "") if connected else "Not connected"
        name = app.get("name", app["type"].capitalize())
        prefix = ""
        if is_submenu and connected and self._is_active(backend, app["id"]):
            prefix = "✓ "
        return f"{prefix}{dot}  {name:<30} {right}"

    def _jira_menu_title(self):
        jira = self._config.get("jira", {})
        if jira.get("server_url") and jira.get("email") and jira.get("api_token"):
            project = jira.get("project", "")
            email = jira.get("email", "")
            dot = "●"
            if project:
                return f"{dot}  Jira Cloud                  {project} ({email})"
            return f"{dot}  Jira Cloud                  {email}"
        return "○  Jira Cloud                  Not configured"

    def _update_auth_state(self):
        """Refresh auth-related menu item titles."""
        for backend in ("rag", "google_workspace"):
            backend_apps = apps_for_backend(self._config, backend)
            by_type = {}
            for app in backend_apps:
                by_type.setdefault(app["type"], []).append(app)
            for app_type, apps_of_type in by_type.items():
                is_submenu = len(apps_of_type) > 1
                for app in apps_of_type:
                    key = f"{backend}:{app['id']}"
                    if key in self._menu_items:
                        self._menu_items[key].title = self._app_menu_title(
                            app, backend, is_submenu=is_submenu
                        )
                if is_submenu:
                    parent_key = f"{backend}:{app_type}:parent"
                    if parent_key in self._menu_items:
                        active_id = self._active_app.get((backend, app_type))
                        type_label = "Microsoft" if app_type == "microsoft" else "Google"
                        if active_id:
                            state = self._auth.get((active_id, backend), {})
                            profile = state.get("profile") or {}
                            email = profile.get("email", "")
                            if email:
                                type_label = f"{type_label} ({email})"
                        self._menu_items[parent_key].title = type_label

        if "jira" in self._menu_items:
            self._menu_items["jira"].title = self._jira_menu_title()

    def _make_app_toggle(self, app_id, backend):
        def callback(_):
            state = self._auth.get((app_id, backend), {})
            if state.get("token"):
                self._deauth_app(app_id, backend)
            else:
                threading.Thread(
                    target=self._do_auth, args=(app_id, backend), daemon=True
                ).start()
        return callback

    def _make_app_activate(self, backend, app_id):
        def callback(_):
            self._set_active(backend, app_id)
            state = self._auth.get((app_id, backend), {})
            if not state.get("token"):
                threading.Thread(
                    target=self._do_auth, args=(app_id, backend), daemon=True
                ).start()
        return callback
