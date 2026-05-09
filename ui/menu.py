"""Voitta Desktop — unified macOS menu bar app.

Auth section on top, conversations below. Dog icon in the menu bar.
Two background servers: LLM proxy (aiohttp) and MCP proxy (FastMCP).
"""
# Main UI module: manages menu bar interactions and background server lifecycle

import asyncio
import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

import objc
import rumps
from AppKit import (
    NSApp, NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSAttributedString, NSBackingStoreBuffered, NSBezierPath,
    NSColor, NSEvent, NSEventMaskKeyDown, NSEventModifierFlagCommand,
    NSEventModifierFlagShift,
    NSFloatingWindowLevel, NSFont, NSFontAttributeName,
    NSBaselineOffsetAttributeName,
    NSForegroundColorAttributeName, NSImage, NSMutableAttributedString,
    NSSize, NSTextAttachment, NSTextAttachmentCell,
    NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSScreen,
)
from Foundation import NSMakeRect, NSObject, NSTimer, NSRunLoop
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore

from auth.providers import (
    build_msal_app, do_auth_microsoft, do_auth_google,
    fetch_profile_microsoft, fetch_profile_google,
    do_refresh_microsoft, do_refresh_google,
)
from auth.jira import fetch_jira_projects
from config import (
    load_config, save_config, migrate_from_legacy, apps_for_backend,
    CONFIG_PATH, CONFIG_DIR,
)
from middleware import ConversationTracker, RequestLogger, BlockType, Turn
from middleware.cache_sim import CacheSimulator
from optimizers import OptimizerPipeline
from optimizers.bash_compress import BashCompressor
from optimizers.image import ImageOptimizer
from optimizers.file_read import FileReadOptimizer
from optimizers.thinking import ThinkingOptimizer
from optimizers.tool_result import ToolResultOptimizer
from proxy import AnthropicProxy
from mcpproxy.server import run_mcp_proxy
from ui.chart import generate_chart_html

logger = logging.getLogger("voitta-desktop")

# ── Subprocess + OAuth settings (sourced from apps.json; env seeds defaults) ─

_startup_cfg = load_config()
_oauth_cfg = _startup_cfg.get("oauth", {})
_sub_cfg = _startup_cfg.get("mcp_subprocess", {})

OAUTH_REDIRECT_PORT = int(_oauth_cfg.get("redirect_port", 53214))
GOOGLE_MCP_PORT = int(_sub_cfg.get("google_mcp_port", 18766))
JIRA_MCP_PORT = int(_sub_cfg.get("jira_mcp_port", 18767))
GOOGLE_MCP_DIR = os.path.expanduser(_sub_cfg.get("google_mcp_dir", "~/DEVEL/google_workspace_mcp"))
GOOGLE_MCP_ENV_PATH = os.path.expanduser(_sub_cfg.get("google_mcp_env_path", "~/DEVEL/google_workspace_mcp/.env"))
JIRA_MCP_DIR = os.path.expanduser(_sub_cfg.get("jira_mcp_dir", "~/DEVEL/mcp-atlassian"))
JIRA_MCP_ENV_PATH = os.path.expanduser(_sub_cfg.get("jira_mcp_env_path", str(CONFIG_DIR / "jira.env")))
LEGACY_SETTINGS_PATH = Path.home() / ".voitta_auth_settings.json"

ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images", "icon_menubar_bright.png")

BLOCK_ICONS = {
    BlockType.USER_TEXT:         "\u25B6 ",
    BlockType.ASSISTANT_TEXT:    "\u25C0 ",
    BlockType.THINKING:          "\u25C6 ",
    BlockType.TOOL_CALL:         "\u2699 ",
    BlockType.TOOL_RESULT:       "  \u21B3 ",
    BlockType.MCP_TOOL_CALL:     "\u26A1 ",
    BlockType.SERVER_TOOL_CALL:  "\u2601 ",
}


def _notify(title, subtitle, message):
    try:
        rumps.notification(title, subtitle, message)
    except Exception:
        pass


# ── NSAlert helpers ──────────────────────────────────────────────────────────

class _FocusTrigger(NSObject):
    def setWindow_field_(self, win, field):
        self._win = win
        self._field = field

    def focus_(self, _):
        NSApp.activateIgnoringOtherApps_(True)
        self._win.makeKeyAndOrderFront_(None)
        if self._field is not None:
            self._win.makeFirstResponder_(self._field)


class _InfoTicker(NSObject):
    """Pushes a fresh Info-tab state to the open settings webview every tick.

    Holds a generation counter so it auto-invalidates when the user closes
    and reopens the settings window (otherwise an old timer would race
    against the new webview).
    """

    def initWithApp_gen_(self, app_ref, gen):
        self = objc.super(_InfoTicker, self).init()
        if self is not None:
            self._app = app_ref
            self._gen = gen
        return self

    def tick_(self, timer):
        app = self._app
        # Settings closed or reopened with a new generation? Stop ticking.
        if getattr(app, "_settings_gen", -1) != self._gen:
            timer.invalidate()
            return
        refs = getattr(app, "_settings_refs", None)
        if refs is None:
            timer.invalidate()
            return
        webview = refs[1]
        try:
            state = app._collect_info_state()
            js = f"_setInfoState({json.dumps(state)})"
            webview.evaluateJavaScript_completionHandler_(js, None)
        except Exception as e:
            logger.debug("info ticker failed: %s", e)
            timer.invalidate()


def _is_port_free(port: int) -> bool:
    """True if 127.0.0.1:<port> is bindable right now."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _grab_free_port() -> int:
    """Ask the OS for an unused port. There's a TOCTOU window between this
    returning and the actual proxy binding it — acceptable since we only call
    this seconds before binding."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def _show_modal(alert, first_field=None):
    alert_window = alert.window()
    alert_window.setLevel_(NSFloatingWindowLevel)
    alert.layout()
    if first_field:
        alert_window.setInitialFirstResponder_(first_field)
    NSApp.activateIgnoringOtherApps_(True)
    trigger = _FocusTrigger.alloc().init()
    trigger.setWindow_field_(alert_window, first_field)
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        0.1, trigger, "focus:", None, False
    )
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSModalPanelRunLoopMode")
    result = alert.runModal()
    return result


# ── Main App ─────────────────────────────────────────────────────────────────

class VoittaDesktopApp(rumps.App):
    def __init__(self):
        super().__init__("VoittaDesktop", title=None, quit_button=None)

        self._noop = lambda _: None
        self._auth_lock = threading.Lock()
        self._auth = {}

        # Load config
        self._config = self._load_or_migrate_config()
        mcp_proxy_cfg = self._config.get("mcp_proxy", {})
        llm_proxy_cfg = self._config.get("llm_proxy", {})

        self.voitta_rag_url = mcp_proxy_cfg.get("rag_url", "https://rag.voitta.ai")
        self.voitta_image_rag_url = mcp_proxy_cfg.get("image_rag_url", "https://rag-img.voitta.ai/mcp")
        self.voitta_image_rag_key = mcp_proxy_cfg.get("image_rag_key", "")
        self.paperclip_url = mcp_proxy_cfg.get("paperclip_url", "https://paperclip.gxl.ai/mcp")
        self.paperclip_key = mcp_proxy_cfg.get("paperclip_key", "")
        self.edit_proxy_url = mcp_proxy_cfg.get("edit_proxy_url", f"http://localhost:{GOOGLE_MCP_PORT}")
        self.mcp_proxy_port = self._resolve_port("MCP proxy", mcp_proxy_cfg.get("port", 18765))
        self.llm_proxy_port = self._resolve_port("LLM proxy", llm_proxy_cfg.get("port", 18900))
        self.llm_upstream_url = llm_proxy_cfg.get("upstream_url", "https://api.anthropic.com")

        # Init auth state per (app, backend)
        for app in self._config.get("apps", []):
            for backend in app.get("use_for", []):
                self._auth[(app["id"], backend)] = {
                    "token": None, "refresh_token": None,
                    "profile": None, "refresh_timer": None, "msal_app": None,
                }
            if app["type"] == "microsoft":
                self._rebuild_msal_for_app(app)

        self.disabled_tools = set(self._config.get("disabled_tools", []))
        self._mcp_tools = {}

        self._active_app = {}
        self._init_active_defaults()

        # Sync .env and start MCP subprocesses
        self._sync_edit_mcp_env()
        self._sync_jira_mcp_env()
        self._start_mcp_subprocesses()

        # LLM proxy components
        self._tracker = ConversationTracker()
        self._request_logger = RequestLogger()
        time_cfg = self._config.get("time", {})
        self._tool_result_optimizer = ToolResultOptimizer(
            keep_turns=int(time_cfg.get("tool_result_keep_turns", 5))
        )
        self._image_optimizer = ImageOptimizer(
            keep_turns=int(time_cfg.get("image_keep_turns", 5))
        )
        self._file_read_optimizer = FileReadOptimizer()
        self._thinking_optimizer = ThinkingOptimizer(
            keep_turns=int(time_cfg.get("thinking_keep_turns", 5))
        )
        bash_cfg = self._config.get("bash", {})
        self._bash_compressor = BashCompressor(
            strip_ansi=bool(bash_cfg.get("strip_ansi", True)),
            trim_whitespace=bool(bash_cfg.get("trim_whitespace", True)),
            strip_progress=bool(bash_cfg.get("strip_progress", False)),
            smart_commands=bool(bash_cfg.get("smart_commands", False)),
        )
        opt_cfg = self._config.get("optimizer", {})
        # BashCompressor runs first so other Bash-handling optimizers see
        # already-compressed output (no double accounting).
        self._optimizer_pipeline = OptimizerPipeline(
            [self._bash_compressor, self._tool_result_optimizer, self._image_optimizer, self._thinking_optimizer],
            enabled=bool(opt_cfg.get("enabled", True)),
            haiku_only=bool(opt_cfg.get("haiku_only", False)),
        )
        self._cache_sim = CacheSimulator()
        self._proxy = AnthropicProxy(
            middlewares=[self._request_logger, self._tracker, self._optimizer_pipeline, self._cache_sim],
            port=self.llm_proxy_port,
            upstream_url=self.llm_upstream_url,
        )
        self._proxy_running = False

        # Build menu
        self._menu_items = {}
        self._conv_menus: dict[str, rumps.MenuItem] = {}
        self._conv_block_counts: dict[str, int] = {}
        self._build_menu()
        self._update_auth_state()
        self._install_edit_shortcuts()

        # Start background servers
        threading.Thread(target=self._run_llm_proxy, daemon=True).start()
        threading.Thread(target=self._run_mcp_proxy, daemon=True).start()

    def _resolve_port(self, label: str, port: int) -> int:
        """If `port` is taken, ask the user whether to switch to an
        OS-assigned free port or quit. Quit means quit — no half-running app.

        We only fall back to a runtime port; we don't persist it. That keeps
        the issue visible (user gets prompted again next launch) and lets the
        underlying conflict — usually a stale Voitta Desktop instance — get
        cleaned up rather than papered over.
        """
        if _is_port_free(port):
            return port

        NSApp.activateIgnoringOtherApps_(True)
        result = rumps.alert(
            title=f"{label} port {port} in use",
            message=(
                f"Port {port} is already in use by another process — most "
                f"likely a previous copy of Voitta Desktop that didn't shut "
                f"down cleanly.\n\n"
                f"Use a different port for this session?\n\n"
                f"Note: existing Claude Code links still point at port "
                f"{port}, so you'll need to re-run \"Link Claude\" from the "
                f"menu for them to find the new port."
            ),
            ok="Use another port",
            cancel="Quit",
        )
        if result != 1:
            logger.info("%s port %d in use; user chose Quit", label, port)
            sys.exit(0)

        new_port = _grab_free_port()
        logger.warning("%s port %d in use; using OS-assigned port %d for this session",
                       label, port, new_port)
        return new_port

    def _promote_for_keyboard(self):
        """Promote app to a Regular activation policy so popup windows can
        actually take keyboard focus.

        LSUIElement / Accessory apps cannot reliably steal key-window status
        from another foreground app — clicks register on our windows but
        keystrokes stay with whatever app is "active". Promoting to Regular
        for the lifetime of an interactive popup fixes this. Refcounted so
        nested popups don't demote prematurely.
        """
        if not hasattr(self, "_kbd_promotions"):
            self._kbd_promotions = 0
        self._kbd_promotions += 1
        if self._kbd_promotions == 1:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    def _demote_after_keyboard(self):
        """Counterpart to _promote_for_keyboard — restore Accessory on last close."""
        if not hasattr(self, "_kbd_promotions") or self._kbd_promotions <= 0:
            return
        self._kbd_promotions -= 1
        if self._kbd_promotions == 0:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    def _install_edit_shortcuts(self):
        """Wire Cmd+C/V/X/A/Z to the focused control via a local event monitor.

        Why: this is an LSUIElement (menu-bar) app with no application menu,
        so the standard editing shortcuts have no menu items to route through
        and silently fail in WKWebView text fields. Installing a main menu
        works for shortcuts but disturbs window activation/key-window behavior
        in a rumps app. A local event monitor avoids touching the main menu —
        it dispatches the relevant selectors through the responder chain via
        NSApp.sendAction_to_from_(sel, None, None) (target=None means "first
        responder"), which WKWebView text fields handle natively.
        """
        SHORTCUTS = {
            "c": "copy:",
            "v": "paste:",
            "x": "cut:",
            "a": "selectAll:",
            "z": "undo:",
        }

        def handler(event):
            flags = event.modifierFlags()
            if not (flags & NSEventModifierFlagCommand):
                return event
            chars = event.charactersIgnoringModifiers() or ""
            chars = chars.lower()
            if chars == "z" and (flags & NSEventModifierFlagShift):
                sel = "redo:"
            else:
                sel = SHORTCUTS.get(chars)
            if sel is None:
                return event
            if NSApp.sendAction_to_from_(sel, None, None):
                return None  # consumed
            return event

        self._edit_event_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, handler
        )

    # ── Config ───────────────────────────────────────────────────────────────

    def _load_or_migrate_config(self):
        if CONFIG_PATH.exists():
            return load_config()
        # Try migrating from voitta-auth legacy
        legacy = {}
        if LEGACY_SETTINGS_PATH.exists():
            try:
                legacy = json.loads(LEGACY_SETTINGS_PATH.read_text())
            except Exception:
                pass
        # Also try voitta-auth apps.json
        old_auth_config = Path.home() / ".voitta_auth" / "apps.json"
        if old_auth_config.exists():
            try:
                data = json.loads(old_auth_config.read_text())
                # Migrate proxy -> mcp_proxy
                if "proxy" in data and "mcp_proxy" not in data:
                    data["mcp_proxy"] = data.pop("proxy")
                if "llm_proxy" not in data:
                    data["llm_proxy"] = {"port": 18900, "upstream_url": "https://api.anthropic.com"}
                else:
                    data["llm_proxy"].setdefault("upstream_url", "https://api.anthropic.com")
                save_config(data)
                return data
            except Exception:
                pass
        config = migrate_from_legacy(legacy)
        save_config(config)
        return config

    def _app_by_id(self, app_id):
        return next((a for a in self._config.get("apps", []) if a["id"] == app_id), None)

    def _init_active_defaults(self):
        for backend in ("rag", "google_workspace"):
            for app in apps_for_backend(self._config, backend):
                key = (backend, app["type"])
                if key not in self._active_app:
                    self._active_app[key] = app["id"]

    def _set_active(self, backend, app_id):
        app = self._app_by_id(app_id)
        if not app:
            return
        self._active_app[(backend, app["type"])] = app_id
        self._update_auth_state()

    def _is_active(self, backend, app_id):
        app = self._app_by_id(app_id)
        if not app:
            return False
        return self._active_app.get((backend, app["type"])) == app_id

    def _has_jira_credentials(self):
        jira = self._config.get("jira", {})
        return bool(jira.get("server_url") and jira.get("email")
                     and jira.get("api_token") and jira.get("project"))

    # ── Menu ─────────────────────────────────────────────────────────────────

    def _build_menu(self):
        menu_list = []

        # ── Auth section ─────────────────────────────────────────────────────
        auth_header = rumps.MenuItem("\u2500\u2500 Auth \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
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
        proxy_header = rumps.MenuItem("\u2500\u2500 LLM Proxy \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
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
        menu_list.append(rumps.MenuItem("Settings", callback=self.show_settings))
        menu_list.append(rumps.MenuItem("Help", callback=self.show_help))
        menu_list.append(rumps.MenuItem("Quit", callback=self._quit))

        self.menu = menu_list

    def _rebuild_menu(self):
        self._menu_items = {}
        self._conv_menus = {}
        self._conv_block_counts = {}
        self.menu.clear()
        self._build_menu()

    # ── Auth menu helpers ────────────────────────────────────────────────────

    def _app_menu_title(self, app, backend, is_submenu=False):
        state = self._auth.get((app["id"], backend), {})
        connected = state.get("token") is not None
        dot = "\u25CF" if connected else "\u25CB"
        profile = state.get("profile") or {}
        right = profile.get("email", "") if connected else "Not connected"
        name = app.get("name", app["type"].capitalize())
        prefix = ""
        if is_submenu and connected and self._is_active(backend, app["id"]):
            prefix = "\u2713 "
        return f"{prefix}{dot}  {name:<30} {right}"

    def _jira_menu_title(self):
        jira = self._config.get("jira", {})
        if jira.get("server_url") and jira.get("email") and jira.get("api_token"):
            project = jira.get("project", "")
            email = jira.get("email", "")
            dot = "\u25CF"
            if project:
                return f"{dot}  Jira Cloud                  {project} ({email})"
            return f"{dot}  Jira Cloud                  {email}"
        return "\u25CB  Jira Cloud                  Not configured"

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
        from auth.providers import do_refresh_google
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

    # ── MCP .env sync ────────────────────────────────────────────────────────

    def _sync_edit_mcp_env(self):
        gw_google = [a for a in self._config.get("apps", [])
                      if a["type"] == "google" and "google_workspace" in a.get("use_for", [])]
        if not gw_google:
            return
        app = gw_google[0]
        client_id = app.get("client_id", "")
        client_secret = app.get("client_secret", "")
        if not client_id or not client_secret:
            return
        lines = [
            "# Managed by voitta-desktop",
            f"GOOGLE_OAUTH_CLIENT_ID={client_id}",
            f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}",
            "MCP_ENABLE_OAUTH21=true",
            "EXTERNAL_OAUTH21_PROVIDER=true",
            "",
        ]
        try:
            Path(GOOGLE_MCP_ENV_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(GOOGLE_MCP_ENV_PATH, "w") as f:
                f.write("\n".join(lines))
        except Exception as e:
            logger.warning("Failed to write edit MCP .env: %s", e)

    def _sync_jira_mcp_env(self):
        jira = self._config.get("jira", {})
        server_url = jira.get("server_url", "")
        email = jira.get("email", "")
        token = jira.get("api_token", "")
        if not server_url or not email or not token:
            return
        project = jira.get("project", "")
        lines = [
            "# Managed by voitta-desktop",
            f"JIRA_URL={server_url}",
            f"JIRA_USERNAME={email}",
            f"JIRA_API_TOKEN={token}",
        ]
        if project:
            lines.append(f"JIRA_PROJECTS_FILTER={project}")
        lines.append("")
        try:
            Path(JIRA_MCP_ENV_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(JIRA_MCP_ENV_PATH, "w") as f:
                f.write("\n".join(lines))
        except Exception as e:
            logger.warning("Failed to write Jira MCP .env: %s", e)

    # ── MCP subprocesses ─────────────────────────────────────────────────────

    def _start_mcp_subprocesses(self):
        self._subprocesses = []

        if Path(GOOGLE_MCP_DIR).is_dir():
            try:
                google_port = str(urlparse(self.edit_proxy_url).port or GOOGLE_MCP_PORT)
                env = {**os.environ, "PORT": google_port}
                proc = subprocess.Popen(
                    ["uv", "run", "main.py", "--transport", "streamable-http"],
                    cwd=GOOGLE_MCP_DIR, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._subprocesses.append(proc)
                logger.info("Started google_workspace_mcp (pid %d)", proc.pid)
            except Exception as e:
                logger.warning("Failed to start google_workspace_mcp: %s", e)

        if Path(JIRA_MCP_DIR).is_dir() and Path(JIRA_MCP_ENV_PATH).exists():
            try:
                proc = subprocess.Popen(
                    [
                        "uvx", "mcp-atlassian",
                        "--transport", "streamable-http",
                        "--port", str(JIRA_MCP_PORT),
                        "--env-file", JIRA_MCP_ENV_PATH,
                    ],
                    cwd=JIRA_MCP_DIR,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._subprocesses.append(proc)
                logger.info("Started mcp-atlassian (pid %d) on port %d", proc.pid, JIRA_MCP_PORT)
            except Exception as e:
                logger.warning("Failed to start mcp-atlassian: %s", e)

        atexit.register(self._stop_mcp_subprocesses)

    def _stop_mcp_subprocesses(self):
        for proc in getattr(self, "_subprocesses", []):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    # ── Background servers ───────────────────────────────────────────────────

    def _run_llm_proxy(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._proxy.start())
            self._proxy_running = True
            logger.info("LLM proxy started on port %d", self.llm_proxy_port)
            loop.run_forever()
        except Exception as e:
            logger.error("LLM proxy failed to start: %s", e)
            self._proxy_running = False

    def _run_mcp_proxy(self):
        try:
            run_mcp_proxy(self, self.mcp_proxy_port, JIRA_MCP_PORT)
        except BaseException as e:
            logger.error("MCP proxy failed: %s", e, exc_info=True)

    # ── Menu bar title + icon ────────────────────────────────────────────────

    @rumps.timer(0.1)
    def _startup_title(self, timer):
        self._update_title()
        timer.stop()

    @rumps.timer(2)
    def _refresh_menu(self, _timer):
        self._update_title()
        self._update_conversations()

    def _update_title(self):
        try:
            button = self._nsapp.nsstatusitem.button()
        except AttributeError:
            return

        # No separate image — we embed the dog inline via NSTextAttachment
        button.setImage_(None)

        is_dark = "Dark" in str(button.effectiveAppearance().name())
        base = 1.0 if is_dark else 0.0
        font = NSFont.menuBarFontOfSize_(0)

        convs = self._tracker.get_conversations_sorted()
        num_convs = sum(1 for c in convs if c.turns)
        alpha = 1.0 if self._proxy_running else 0.4

        title = NSMutableAttributedString.alloc().init()

        # ── Left of dog: savings + conversation count ────────────────────────

        savings = self._optimizer_pipeline.total_savings_usd
        if savings >= 0.0001:
            savings_str = f"${savings:.2f} " if savings >= 0.01 else f"${savings:.4f} "
            green = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.19, 0.82, 0.35, alpha)
            title.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    savings_str, {NSForegroundColorAttributeName: green, NSFontAttributeName: font}
                ))

        if num_convs > 0:
            color = NSColor.colorWithCalibratedWhite_alpha_(base, alpha)
            title.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    f"{num_convs} ", {NSForegroundColorAttributeName: color, NSFontAttributeName: font}
                ))

        # ── Dog icon (inline) ────────────────────────────────────────────────

        icon = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
        if icon:
            icon_size = 16
            icon.setSize_(NSSize(icon_size, icon_size))
            icon.setTemplate_(False)
            attachment = NSTextAttachment.alloc().init()
            cell = NSTextAttachmentCell.alloc().initImageCell_(icon)
            attachment.setAttachmentCell_(cell)
            icon_str = NSMutableAttributedString.alloc().initWithAttributedString_(
                NSAttributedString.attributedStringWithAttachment_(attachment)
            )
            # Shift the icon down so it aligns with the text midline
            baseline_offset = (font.capHeight() - icon_size) / 2.0
            icon_str.addAttribute_value_range_(
                NSBaselineOffsetAttributeName, baseline_offset, (0, icon_str.length())
            )
            title.appendAttributedString_(icon_str)
            title.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "  ", {NSFontAttributeName: font}
                ))

        # ── Right of dog: auth provider letters ─────────────────────────────

        seen_types = []
        for app in self._config.get("apps", []):
            t = app["type"]
            if t not in seen_types:
                seen_types.append(t)

        for app_type in seen_types:
            letter = "M" if app_type == "microsoft" else "G"
            active = any(
                state.get("token") is not None
                for key, state in self._auth.items()
                if self._app_by_id(key[0]) and self._app_by_id(key[0])["type"] == app_type
            )
            letter_alpha = alpha if active else 0.3
            color = NSColor.colorWithCalibratedWhite_alpha_(base, letter_alpha)
            title.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    letter + " ", {NSForegroundColorAttributeName: color, NSFontAttributeName: font}
                ))

        jira_active = self._has_jira_credentials()
        jira_alpha = alpha if jira_active else 0.3
        color = NSColor.colorWithCalibratedWhite_alpha_(base, jira_alpha)
        title.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                "J", {NSForegroundColorAttributeName: color, NSFontAttributeName: font}
            ))

        button.setAttributedTitle_(title)

    # ── Conversation menu ────────────────────────────────────────────────────

    def _update_conversations(self):
        convs = self._tracker.get_conversations_sorted()

        stale_ids = set(self._conv_menus.keys()) - {c.id for c in convs}
        for cid in stale_ids:
            try:
                del self.menu[self._conv_menus[cid].title]
            except KeyError:
                pass
            del self._conv_menus[cid]
            self._conv_block_counts.pop(cid, None)

        if not convs or not any(c.turns for c in convs):
            return

        prev = self._conv_header.title
        for conv in [c for c in convs[:20] if c.turns]:
            tokens = self._fmt_tokens(conv.total_tokens)
            cache_info = ""
            if conv.cache_read_input_tokens > 0:
                total_in = conv.input_tokens + conv.cache_read_input_tokens
                cache_pct = (conv.cache_read_input_tokens * 100) // max(total_in, 1)
                cache_info = f" cache:{cache_pct}%"

            title = f"{conv.label}  [{tokens}{cache_info}] \u00D7{conv.request_count}"
            block_count = sum(len(t.blocks) for t in conv.turns)

            if conv.id in self._conv_menus:
                old_item = self._conv_menus[conv.id]
                old_block_count = self._conv_block_counts.get(conv.id, 0)
                if old_item.title != title or block_count != old_block_count:
                    try:
                        del self.menu[old_item.title]
                    except KeyError:
                        pass
                    new_item = self._build_conv_submenu(conv, title)
                    self._conv_menus[conv.id] = new_item
                    self._conv_block_counts[conv.id] = block_count
                    self.menu.insert_after(prev, new_item)
            else:
                new_item = self._build_conv_submenu(conv, title)
                self._conv_menus[conv.id] = new_item
                self._conv_block_counts[conv.id] = block_count
                self.menu.insert_after(prev, new_item)

            prev = self._conv_menus[conv.id].title

    def _build_conv_submenu(self, conv, title: str) -> rumps.MenuItem:
        submenu = rumps.MenuItem(title, callback=self._show_conv_popup)
        submenu._conv_label = conv.label
        submenu._conv_id = conv.id
        self._populate_turns(submenu, conv)
        return submenu

    def _show_conv_popup(self, sender):
        conv_label = getattr(sender, '_conv_label', 'Conversation')
        conv_id = getattr(sender, '_conv_id', None)

        breakdown_data = {"system": 0, "tools": 0, "other": 0}
        turns_data = []
        if conv_id:
            conv = self._tracker.get_conversation(conv_id)
            if conv:
                bd = conv.breakdown
                if bd:
                    breakdown_data = {
                        "system": bd.system_prompt_chars,
                        "tools": bd.tools_chars,
                        "other": bd.other_chars,
                        "tools_count": bd.tools_count,
                        "tool_groups": [
                            {"prefix": g.prefix, "count": g.count, "chars": g.total_chars}
                            for g in bd.tool_groups[:8]
                        ],
                        "system_blocks": [
                            {"preview": p[:60], "chars": c}
                            for p, c in bd.system_blocks[:5]
                        ],
                    }
                stripped_ids = self._optimizer_pipeline.stripped_tool_ids
                stripped_msgs = self._optimizer_pipeline.stripped_msg_indices
                for t in conv.turns:
                    images_data = []
                    for img in t.images:
                        token_chars = int(img.width * img.height / 750 * 3.5)
                        images_data.append({
                            "media_type": img.media_type,
                            "base64_chars": img.base64_chars,
                            "raw_bytes": img.raw_bytes,
                            "width": img.width, "height": img.height,
                            "source_type": img.source_type,
                            "thumbnail": img.thumbnail_b64 if img.thumbnail_b64 else "",
                            "token_chars": token_chars,
                        })
                    blocks_data = [
                        {"type": b.block_type.value, "summary": b.summary[:100]}
                        for b in t.blocks
                    ]
                    # Compute per-turn stripped chars from optimizer data
                    stripped_tool = sum(
                        stripped_ids.get(tid, 0) for tid in t.tool_use_ids
                    )
                    stripped_think = sum(
                        stripped_msgs.get(mi, 0)
                        for mi in range(t._msg_range[0], t._msg_range[1])
                    )
                    turns_data.append({
                        "index": t.index, "label": t.label[:30],
                        "user_text": t.user_text_chars,
                        "tool_result": t.tool_result_chars,
                        "assistant_text": t.assistant_text_chars,
                        "tool_call": t.tool_call_chars,
                        "image": sum(int(img.width * img.height / 750 * 3.5) for img in t.images),
                        "stale_read": t.stale_read_chars,
                        "bash": t.bash_chars,
                        "thinking": t.thinking_chars,
                        "stripped_tool": stripped_tool,
                        "stripped_thinking": stripped_think,
                        "images": images_data,
                        "blocks": blocks_data,
                        "input_tokens": t.input_tokens,
                        "output_tokens": t.output_tokens,
                        "cache_read_input_tokens": t.cache_read_input_tokens,
                        "cache_creation_input_tokens": t.cache_creation_input_tokens,
                        "cache_control_types": t.cache_control_types,
                        "msg_count": t._msg_range[1] - t._msg_range[0],
                        "file_ops": [
                            {
                                "tool": op.tool_name,
                                "file": op.file_path,
                                "start": op.start_line,
                                "end": op.end_line,
                                "old_len": op.old_str_len,
                                "new_len": op.new_str_len,
                                "content_len": op.content_len,
                            }
                            for op in t.file_ops
                        ],
                    })

        active = self._optimizer_pipeline.active_optimizers
        cache_history = self._cache_sim.get_history(conv_id) if conv_id else []
        # Align cache data from the end (cache resets on restart, turns don't)
        ch_offset = len(turns_data) - len(cache_history)
        for i, td in enumerate(turns_data):
            ci = i - ch_offset
            if 0 <= ci < len(cache_history):
                ch = cache_history[ci]
                td["cache_sim"] = ch
            else:
                td["cache_sim"] = None
        html = generate_chart_html(None, breakdown_data, turns_data, active)

        screen = NSScreen.mainScreen().frame()
        num_turns = len(turns_data)
        width = max(720, min(int(screen.size.width * 0.9), 40 * (num_turns + 1) + 140))
        has_file_ops = any(td.get("file_ops") for td in turns_data)
        height = 700 if has_file_ops else 520
        frame = NSMakeRect(0, 0, width, height)
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        window.setTitle_(conv_label)
        window.setLevel_(NSFloatingWindowLevel)
        window.center()

        config = WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        webview.loadHTMLString_baseURL_(html, None)
        window.setContentView_(webview)
        window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

        if not hasattr(self, '_popup_windows'):
            self._popup_windows = []
        self._popup_windows.append(window)

    def _populate_turns(self, menu_item: rumps.MenuItem, conv):
        open_item = rumps.MenuItem(f"open_{conv.id}", callback=self._show_conv_popup)
        open_item.title = "Open conversation details\u2026"
        open_item._conv_label = conv.label
        open_item._conv_id = conv.id
        menu_item[f"open_{conv.id}"] = open_item

        sep = rumps.MenuItem(f"sep_{conv.id}")
        sep.title = "\u2500" * 40
        sep.set_callback(self._noop)
        menu_item[f"sep_{conv.id}"] = sep

        bd = conv.breakdown
        if bd:
            bd_item = rumps.MenuItem(f"bd_{id(bd)}")
            prompt = self._fmt_chars(bd.system_prompt_chars)
            tools = self._fmt_chars(bd.tools_chars)
            msgs = self._fmt_chars(bd.messages_chars)
            bd_item.title = f"Request overhead: {self._fmt_chars(bd.total_chars)} total \u2014 prompt {prompt} / tools {tools} / messages {msgs}"

            sys_item = rumps.MenuItem(f"sys_{id(bd)}")
            sys_item.title = f"System prompt: {self._fmt_chars(bd.system_prompt_chars)}"
            for i, (preview, chars) in enumerate(bd.system_blocks):
                si = rumps.MenuItem(f"sb_{i}_{id(bd)}")
                si.title = f"[{self._fmt_chars(chars)}] {preview}"
                si.set_callback(self._noop)
                sys_item[f"sb_{i}_{id(bd)}"] = si
            bd_item[f"sys_{id(bd)}"] = sys_item

            tools_item = rumps.MenuItem(f"tools_{id(bd)}")
            tools_item.title = f"Tools: {bd.tools_count} tools, {self._fmt_chars(bd.tools_chars)}"
            for gi, group in enumerate(bd.tool_groups):
                group_item = rumps.MenuItem(f"tg_{gi}_{id(bd)}")
                group_item.title = f"{group.prefix}: {group.count} tools, {self._fmt_chars(group.total_chars)}"
                for ti, (name, chars) in enumerate(group.tools):
                    tool_item = rumps.MenuItem(f"tl_{gi}_{ti}_{id(bd)}")
                    tool_item.title = f"[{self._fmt_chars(chars)}] {name}"
                    tool_item.set_callback(self._noop)
                    group_item[f"tl_{gi}_{ti}_{id(bd)}"] = tool_item
                tools_item[f"tg_{gi}_{id(bd)}"] = group_item
            bd_item[f"tools_{id(bd)}"] = tools_item

            menu_item[f"bd_{id(bd)}"] = bd_item

        for turn in conv.turns:
            in_chars = self._fmt_chars(turn.chars_in)
            out_chars = self._fmt_chars(turn.chars_out)
            key = f"t{turn.index}_{id(turn)}"
            turn_item = rumps.MenuItem(key)
            turn_item.title = f"[{in_chars}/{out_chars}]  {turn.label}"
            self._populate_blocks(turn_item, turn)
            menu_item[key] = turn_item

    def _populate_blocks(self, menu_item: rumps.MenuItem, turn: Turn):
        for i, block in enumerate(turn.blocks):
            icon = BLOCK_ICONS.get(block.block_type, "  ")
            token_info = ""
            if block.input_tokens:
                token_info += f"  in:{self._fmt_tokens(block.input_tokens)}"
            if block.output_tokens:
                token_info += f"  out:{self._fmt_tokens(block.output_tokens)}"
            key = f"b{i}_{id(block)}"
            item = rumps.MenuItem(key)
            item.title = f"{icon}{block.summary}{token_info}"
            item.set_callback(self._noop)
            menu_item[key] = item

    # ── Settings ─────────────────────────────────────────────────────────────

    def _poll_mcp_tools(self):
        """Poll the MCP proxy for ALL tool names (blocking HTTP). Run from a thread."""
        import urllib.request
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.mcp_proxy_port}/mcp",
                data=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1,
                                 "params": {"protocolVersion": "2025-03-26",
                                            "capabilities": {},
                                            "clientInfo": {"name": "settings", "version": "1"}}}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            session_id = resp.headers.get("Mcp-Session-Id", "")

            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{self.mcp_proxy_port}/mcp",
                data=json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}).encode(),
                headers=headers,
                method="POST",
            )
            resp2 = urllib.request.urlopen(req2, timeout=10)
            body = resp2.read().decode()

            if body.strip().startswith("event:") or body.strip().startswith("data:"):
                for line in body.splitlines():
                    if line.startswith("data:"):
                        body = line[5:].strip()
                        break

            result = json.loads(body)
            # These are the *filtered* tools (disabled ones excluded by the proxy).
            # Merge with _mcp_tools which has ALL tools (stashed before filtering).
            return [t["name"] for t in result.get("result", {}).get("tools", [])]
        except Exception as e:
            logger.warning("Failed to poll MCP proxy for tools: %s", e)
            return []

    def _build_tool_tree(self):
        """Build tool tree from _mcp_tools (always has ALL tools, including disabled)."""
        all_tools = set()
        for names in self._mcp_tools.values():
            all_tools.update(names)

        from mcpproxy.backends import tool_tree_groups
        groups = tool_tree_groups()
        tree = []
        used = set()
        for prefix, label in groups:
            matching = sorted([t for t in all_tools if t.startswith(prefix + "_")])
            used.update(matching)
            if matching:
                tree.append({"prefix": prefix, "label": label, "tools": matching})

        remaining = sorted(all_tools - used)
        if remaining:
            tree.append({"prefix": "", "label": "Other", "tools": remaining})

        return tree

    def show_settings(self, _):
        # Prevent double-open: if window still exists, just bring it forward
        if hasattr(self, "_settings_refs") and self._settings_refs:
            win = self._settings_refs[0]
            try:
                if win.isVisible():
                    NSApp.activateIgnoringOtherApps_(True)
                    win.makeKeyAndOrderFront_(None)
                    return
            except Exception:
                pass
            # Window exists but isn't visible (closed via red X) — clean up old observer
            old_observer = self._settings_refs[2]
            old_observer._removeKVO()
            self._settings_refs = None

        from WebKit import WKWebView

        # Bump generation counter so stale background threads skip their callback
        if not hasattr(self, "_settings_gen"):
            self._settings_gen = 0
        self._settings_gen += 1
        gen = self._settings_gen

        mask = 1 | 2 | 8
        frame = NSMakeRect(200, 200, 540, 650)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, mask, NSBackingStoreBuffered, False
        )
        window.setTitle_("Voitta Desktop \u2014 Settings")
        window.setReleasedWhenClosed_(False)
        window.center()

        webview = WKWebView.alloc().initWithFrame_(window.contentView().bounds())
        webview.setAutoresizingMask_(18)
        window.contentView().addSubview_(webview)

        # Load HTML immediately with empty tool tree — tools injected after poll
        html_path = Path(__file__).parent / "settings.html"
        html_content = html_path.read_text(encoding="utf-8")
        config_json = json.dumps(self._config)
        # Inject the live Claude-link state so the bottom-left button shows
        # the correct label the moment the window opens.
        from claude_link import load_claude_settings, is_voitta_connected
        linked = is_voitta_connected(load_claude_settings(), self.llm_proxy_port)
        # Live Info-tab state at popup open. Subsequent updates pushed by
        # the _InfoTicker every ~3s.
        info_state = self._collect_info_state()
        html_content = html_content.replace(
            "/*INJECT_CONFIG*/",
            f"var _initialConfig = {config_json};\n"
            f"var _initialClaudeLinked = {json.dumps(linked)};\n"
            f"var _initialInfo = {json.dumps(info_state)};\n"
            f"var _toolTree = [];",
        )
        webview.loadHTMLString_baseURL_(html_content, None)

        observer = _SettingsTitleObserver.alloc().initWithApp_window_gen_(self, window, gen)
        webview.addObserver_forKeyPath_options_context_(observer, "title", 1, None)
        observer._webview = webview

        # Handle the red X close button — clean up KVO before deallocation
        from AppKit import NSNotificationCenter
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            observer, "windowWillClose:", "NSWindowWillCloseNotification", window
        )

        # Info-tab live ticker — pushes a fresh state to the webview every
        # 3 s while the settings window is open. Self-invalidates when the
        # window closes (refs go None).
        info_ticker = _InfoTicker.alloc().initWithApp_gen_(self, gen)
        info_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.0, info_ticker, "tick:", None, True
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(info_timer, "NSDefaultRunLoopMode")

        self._settings_refs = (window, webview, observer)

        self._promote_for_keyboard()
        NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

        trigger = _FocusTrigger.alloc().init()
        trigger.setWindow_field_(window, webview)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, trigger, "focus:", None, False
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")

        # Poll MCP proxy in background, then inject tools into webview
        def _poll_and_inject():
            self._poll_mcp_tools()
            tree = self._build_tool_tree()
            js = f"_toolGroups = {json.dumps(tree)}; renderToolTree();"
            from PyObjCTools import AppHelper
            def _inject():
                # Guard: skip if settings window was closed or reopened since we started
                if self._settings_gen != gen or not self._settings_refs:
                    return
                wv = self._settings_refs[1]
                try:
                    wv.evaluateJavaScript_completionHandler_(js, None)
                except Exception:
                    logger.debug("Settings webview gone before tool inject")
            AppHelper.callAfter(_inject)
        threading.Thread(target=_poll_and_inject, daemon=True).start()

    def _apply_settings(self, new_config):
        """Apply new settings. Safe to call from any thread — UI updates
        are dispatched to the main thread via AppHelper.callAfter."""
        from PyObjCTools import AppHelper

        old_keys = set(self._auth.keys())
        self._config = new_config
        save_config(new_config)

        new_keys = set()
        for app in new_config.get("apps", []):
            for backend in app.get("use_for", []):
                new_keys.add((app["id"], backend))

        for app in new_config.get("apps", []):
            for backend in app.get("use_for", []):
                if (app["id"], backend) not in self._auth:
                    self._auth[(app["id"], backend)] = {
                        "token": None, "refresh_token": None,
                        "profile": None, "refresh_timer": None, "msal_app": None,
                    }
            if app["type"] == "microsoft":
                self._rebuild_msal_for_app(app)  # may block (OIDC discovery)

        for key in old_keys - new_keys:
            app_id, backend = key
            self._deauth_app(app_id, backend)
            self._auth.pop(key, None)

        mcp_proxy_cfg = new_config.get("mcp_proxy", {})
        self.voitta_rag_url = mcp_proxy_cfg.get("rag_url", "https://rag.voitta.ai")
        self.voitta_image_rag_url = mcp_proxy_cfg.get("image_rag_url", "https://rag-img.voitta.ai/mcp")
        self.voitta_image_rag_key = mcp_proxy_cfg.get("image_rag_key", "")
        self.paperclip_url = mcp_proxy_cfg.get("paperclip_url", "https://paperclip.gxl.ai/mcp")
        self.paperclip_key = mcp_proxy_cfg.get("paperclip_key", "")
        self.edit_proxy_url = mcp_proxy_cfg.get("edit_proxy_url", f"http://localhost:{GOOGLE_MCP_PORT}")
        self.disabled_tools = set(new_config.get("disabled_tools", []))

        opt_cfg = new_config.get("optimizer", {})
        self._optimizer_pipeline.enabled = bool(opt_cfg.get("enabled", True))
        self._optimizer_pipeline.haiku_only = bool(opt_cfg.get("haiku_only", False))

        bash_cfg = new_config.get("bash", {})
        self._bash_compressor.strip_ansi = bool(bash_cfg.get("strip_ansi", True))
        self._bash_compressor.trim_whitespace = bool(bash_cfg.get("trim_whitespace", True))
        self._bash_compressor.strip_progress = bool(bash_cfg.get("strip_progress", False))
        self._bash_compressor.smart_commands = bool(bash_cfg.get("smart_commands", False))

        time_cfg = new_config.get("time", {})
        self._tool_result_optimizer.keep_turns = max(1, int(time_cfg.get("tool_result_keep_turns", 5)))
        self._image_optimizer.keep_turns = max(1, int(time_cfg.get("image_keep_turns", 5)))
        self._thinking_optimizer.keep_turns = max(1, int(time_cfg.get("thinking_keep_turns", 5)))

        self._init_active_defaults()
        self._sync_edit_mcp_env()
        self._sync_jira_mcp_env()

        # UI mutations must happen on the main thread
        def _update_ui():
            self._rebuild_menu()
            self._update_auth_state()
        AppHelper.callAfter(_update_ui)

    # ── Help ─────────────────────────────────────────────────────────────────

    def show_help(self, _):
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Desktop Help")
        alert.setInformativeText_(
            "Voitta Desktop sits in your menu bar.\n\n"
            "Auth section: click providers to connect/disconnect.\n"
            "Conversations section: see live Claude Code sessions.\n\n"
            f"MCP proxy: http://127.0.0.1:{self.mcp_proxy_port}/mcp\n"
            f"  RAG \u2192 {self.voitta_rag_url}\n"
            f"  Google \u2192 {self.edit_proxy_url}\n"
            f"  Jira \u2192 http://127.0.0.1:{JIRA_MCP_PORT}/mcp\n\n"
            f"LLM proxy: http://127.0.0.1:{self.llm_proxy_port}\n"
            "  Set ANTHROPIC_BASE_URL to the LLM proxy URL."
        )
        alert.addButtonWithTitle_("OK")
        _show_modal(alert)

    # ── Formatting helpers ───────────────────────────────────────────────────

    def _fmt_chars(self, chars: int) -> str:
        if chars >= 1_000_000:
            return f"{chars / 1_000_000:.1f}M"
        elif chars >= 1_000:
            return f"{chars / 1_000:.1f}k"
        return str(chars)

    def _fmt_tokens(self, tokens: int) -> str:
        if tokens >= 1_000_000:
            return f"{tokens / 1_000_000:.1f}M"
        elif tokens >= 1_000:
            return f"{tokens / 1_000:.1f}k"
        return str(tokens)

    def _toggle_optimizer(self, sender):
        self._optimizer_pipeline.enabled = not self._optimizer_pipeline.enabled
        sender.state = self._optimizer_pipeline.enabled
        self._config.setdefault("optimizer", {})["enabled"] = self._optimizer_pipeline.enabled
        save_config(self._config)

    def _collect_info_state(self) -> dict:
        """Snapshot of system state for the Info-tab diagram.

        Computed each render — cheap (no upstream calls). Each MCP backend
        is classified by the cached tool count + last refresh outcome:
          - "ok"    → cached tools available
          - "empty" → no cache yet, no failure recorded
          - "error" → cache empty AND last refresh failed
        """
        from claude_link import (
            load_claude_settings, is_voitta_connected, is_mcp_wired,
            is_codex_mcp_wired,
        )
        from urllib.parse import urlparse

        cfg = load_claude_settings()
        llm_wired = is_voitta_connected(cfg, self.llm_proxy_port)
        mcp_wired_claude = is_mcp_wired(cfg, self.mcp_proxy_port)
        mcp_wired_codex = is_codex_mcp_wired(self.mcp_proxy_port)
        mcp_wired = mcp_wired_claude or mcp_wired_codex

        # Most-recent active conversation, used for the model badge.
        current_model = None
        active_count = 0
        try:
            convs = self._tracker.get_conversations_sorted()
            active_count = sum(1 for c in convs if c.turns)
            for c in convs:
                model = getattr(c, "model", None) or getattr(c, "last_model", None)
                if model:
                    current_model = model_family(model)
                    break
        except Exception:
            pass

        upstream_host = "api.anthropic.com"
        try:
            parsed = urlparse(self.llm_upstream_url or "")
            if parsed.netloc:
                upstream_host = parsed.netloc
        except Exception:
            pass

        backends = []
        for label, _url, proxy in getattr(self, "_mcp_backends", []) or []:
            try:
                count = proxy.peek_cached()
            except Exception:
                count = 0
            err = getattr(proxy, "_last_refresh_error", None)
            if count > 0:
                state = "ok"
            elif err:
                state = "error"
            else:
                state = "empty"
            backends.append({"label": label, "tools_count": count, "state": state})

        return {
            "llm_wired": llm_wired,
            "mcp_wired": mcp_wired,
            "mcp_wired_claude": mcp_wired_claude,
            "mcp_wired_codex": mcp_wired_codex,
            "current_model": current_model,
            "active_conversations": active_count,
            "llm_proxy": {"port": self.llm_proxy_port},
            "mcp_proxy": {"port": self.mcp_proxy_port},
            "optimizer_enabled": bool(self._optimizer_pipeline.enabled),
            "upstream_host": upstream_host,
            "savings_usd": float(self._optimizer_pipeline.total_savings_usd),
            "backends": backends,
        }

    def _open_claude_link_popup(self):
        """Show the Connect/Disconnect diff popup for ~/.claude/settings.json.

        Triggered by the bottom-left button in Settings → Proxies. Computes
        the plan against the live state of settings.json + the saved LLM
        proxy port, opens a modal-ish popup with the diff, and applies the
        changes if the user clicks OK.

        Side effects on confirm:
          - rewrites ~/.claude/settings.json (env block only; everything
            else is preserved)
          - if the plan inherits an upstream URL from Claude's existing
            ANTHROPIC_BASE_URL, also writes our llm_proxy.upstream_url
            to apps.json and applies it to the live proxy (requires a
            restart for the new upstream to take effect — we surface this
            in the popup, then ship the value).
          - re-injects _setClaudeLinkState into the open settings webview
            so the button label flips immediately.
        """
        from claude_link import (
            load_claude_settings, settings_file_is_malformed,
            is_voitta_connected, plan_connect, plan_disconnect, apply_changes,
        )
        from ui.claude_link_popup import ClaudeLinkPopup

        if settings_file_is_malformed():
            from AppKit import NSAlert
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Cannot read ~/.claude/settings.json")
            alert.setInformativeText_(
                "The file exists but isn't valid JSON. Fix it by hand and try again."
            )
            alert.addButtonWithTitle_("OK")
            _show_modal(alert)
            return

        cfg = load_claude_settings()
        port = self.llm_proxy_port
        currently_linked = is_voitta_connected(cfg, port)

        if currently_linked:
            plan = plan_disconnect(cfg, port)
            title = "Disconnect Claude"
            ok_label = "Disconnect"
        else:
            plan = plan_connect(cfg, port, self.llm_upstream_url)
            title = "Connect Claude"
            ok_label = "Connect"

        popup = ClaudeLinkPopup()
        self._claude_link_popup = popup

        def _on_confirm():
            try:
                apply_changes(plan)
            except Exception as e:
                logger.error("Claude link apply failed: %s", e, exc_info=True)
                _notify("Voitta Desktop", "Claude link failed", str(e)[:200])
                return

            # Inherited upstream goes into apps.json + the live proxy. The
            # llm_proxy_port hasn't changed, so the running proxy keeps its
            # listener; only the upstream URL it forwards to is different.
            # The aiohttp proxy reads upstream_url at construction time, so
            # a full effect requires restart — surface that.
            if plan.voitta_upstream_change is not None:
                new_upstream = plan.voitta_upstream_change.new
                self._config.setdefault("llm_proxy", {})["upstream_url"] = new_upstream
                save_config(self._config)
                self.llm_upstream_url = new_upstream
                _notify(
                    "Voitta Desktop",
                    "Upstream URL updated",
                    "Restart Voitta Desktop for the new upstream to take effect.",
                )

            # Push the new state into the still-open settings webview so the
            # button flips label without requiring the user to reopen it.
            new_state = plan.target == "connect"
            refs = getattr(self, "_settings_refs", None)
            if refs is not None:
                wv = refs[1]
                from PyObjCTools import AppHelper
                def _flip():
                    try:
                        wv.evaluateJavaScript_completionHandler_(
                            f"_setClaudeLinkState({json.dumps(new_state)})", None
                        )
                    except Exception:
                        pass
                AppHelper.callAfter(_flip)

            verb = "Connected" if plan.target == "connect" else "Disconnected"
            _notify("Voitta Desktop", verb + " Claude Code", "~/.claude/settings.json")

        popup.set_handlers(_on_confirm, lambda: None)
        popup.open(plan, title=title, ok_label=ok_label)

    def _show_llm_tools_status(self, _):
        """Open the LLM Tools Status popup (read-only) and let the user
        drive refreshes from inside it.

        Opening does NOT touch upstream — rows are filled from each backend's
        on-disk cache via `peek_cached()`. A persistent worker loop runs for
        the popup's lifetime so the user can refresh individual backends or
        all of them on demand. Closing the popup tears down the loop.
        """
        # If popup is already up, just bring it to front — no duplicates.
        existing = getattr(self, "_status_popup", None)
        if existing is not None and not existing._closed:
            try:
                NSApp.activateIgnoringOtherApps_(True)
                existing.window.makeKeyAndOrderFront_(None)
            except Exception:
                pass
            return

        backends = getattr(self, "_mcp_backends", None)
        if not backends:
            _notify("Voitta Desktop", None, "No MCP backends configured")
            return

        # Build initial rows from the on-disk cache.
        rows: list[tuple[str, str, str, str]] = []
        for label, url, proxy in backends:
            count = proxy.peek_cached()
            row_label = f"{count} tools" if count else "Not loaded"
            rows.append((label, url, "idle", row_label))

        from ui.refresh_popup import StatusPopup
        popup = StatusPopup()
        self._status_popup = popup

        # Worker-loop state, populated once the loop thread is up.
        self._status_loop: asyncio.AbstractEventLoop | None = None
        self._status_tasks: dict[int, asyncio.Task] = {}
        self._status_cancelled: set[int] = set()

        loop_ready = threading.Event()

        def _worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._status_loop = loop
            loop_ready.set()
            try:
                loop.run_forever()
            finally:
                # Best-effort drain of any tasks left mid-flight before close.
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass
                self._status_loop = None
                self._status_tasks = {}
                self._status_cancelled = set()

        threading.Thread(target=_worker, daemon=True).start()
        # Wait briefly for the loop to be ready so the first user click
        # doesn't race against worker startup. 1s is generous; if it
        # somehow doesn't come up, handlers degrade to no-ops.
        loop_ready.wait(timeout=1.0)

        async def _refresh_one(idx: int, label: str, proxy):
            import time
            start = time.time()
            try:
                ok, count, err = await proxy.force_refresh()
            except asyncio.CancelledError:
                ok, count, err = False, 0, "cancelled"
            except Exception as e:
                ok, count, err = False, 0, str(e)
            elapsed = time.time() - start

            # If user cancelled this row, the popup already shows
            # "✗ cancelled" — don't overwrite it with a late result.
            if idx in self._status_cancelled:
                self._status_cancelled.discard(idx)
                return

            if ok:
                popup.update(idx, "ok", f"✓ {count} tools · {elapsed:.1f}s")
            elif err == "cancelled":
                popup.update(idx, "fail", "✗ cancelled")
            else:
                popup.update(idx, "fail", f"✗ {err or 'unknown error'}")

        def _start_one(idx: int):
            """Schedule a refresh task for backend `idx` on the worker loop."""
            loop = self._status_loop
            if loop is None:
                return
            existing = self._status_tasks.get(idx)
            if existing is not None and not existing.done():
                return  # already refreshing
            self._status_cancelled.discard(idx)
            popup.update(idx, "fetching", "Fetching…")
            label, _url, proxy = backends[idx]

            def _create():
                task = loop.create_task(_refresh_one(idx, label, proxy))
                self._status_tasks[idx] = task

            loop.call_soon_threadsafe(_create)

        def _refresh_all():
            for i in range(len(backends)):
                _start_one(i)

        def _cancel(idx: int):
            if idx in self._status_cancelled:
                return
            self._status_cancelled.add(idx)
            # Immediate visual feedback regardless of whether the task
            # honors cancellation promptly.
            popup.update(idx, "fail", "✗ cancelled")
            loop = self._status_loop
            task = self._status_tasks.get(idx)
            if loop is not None and task is not None and not task.done():
                loop.call_soon_threadsafe(task.cancel)

        def _on_close():
            loop = self._status_loop
            if loop is not None:
                # Stops run_forever; finally-block in _worker drains pending tasks.
                loop.call_soon_threadsafe(loop.stop)
            self._status_popup = None

        popup.set_refresh_handler(_start_one)
        popup.set_refresh_all_handler(_refresh_all)
        popup.set_cancel_handler(_cancel)
        popup.set_close_handler(_on_close)
        popup.open(rows)

    def _quit(self, _):
        rumps.quit_application()


# ── Settings WKWebView bridge ────────────────────────────────────────────────

class _SettingsTitleObserver(NSObject):
    def initWithApp_window_gen_(self, app_ref, window, gen):
        self = objc.super(_SettingsTitleObserver, self).init()
        if self is not None:
            self._app = app_ref
            self._window = window
            self._gen = gen
            self._handled = False
            self._kvo_removed = False
            self._webview = None  # set after addObserver
        return self

    def _removeKVO(self):
        """Safely remove KVO observer exactly once."""
        if self._kvo_removed or self._webview is None:
            return
        self._kvo_removed = True
        try:
            self._webview.removeObserver_forKeyPath_(self, "title")
        except Exception:
            pass

    def windowWillClose_(self, notification):
        """Handle the red X close button — clean up before window is gone."""
        self._handled = True
        self._removeKVO()
        self._cleanupRefs()

    def _cleanupRefs(self):
        """Clear _settings_refs only if they still belong to our generation."""
        from AppKit import NSNotificationCenter
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        if getattr(self._app, "_settings_gen", 0) == self._gen:
            self._app._settings_refs = None
        try:
            self._app._demote_after_keyboard()
        except Exception:
            pass

    def observeValueForKeyPath_ofObject_change_context_(
        self, keyPath, obj, change, context
    ):
        if self._handled:
            return
        title = obj.title()
        if not title:
            return

        if title.startswith("VOITTA_FETCH_JIRA_PROJECTS:"):
            import base64
            payload = title.split(":", 1)[1]
            try:
                decoded = base64.b64decode(payload).decode("utf-8")
                url, email, token = decoded.split("|", 2)
            except Exception:
                obj.evaluateJavaScript_completionHandler_("_setJiraProjectsError('Bad request')", None)
                return
            obj.evaluateJavaScript_completionHandler_(
                "document.title = 'Voitta Desktop \u2014 Settings'", None
            )
            app_ref = self._app
            gen = self._gen

            def _do_fetch():
                try:
                    projects = fetch_jira_projects(url, email, token)
                    projects_json = json.dumps(projects)
                    js = f"_setJiraProjects({projects_json})"
                except Exception as e:
                    js = f"_setJiraProjectsError({json.dumps(str(e))})"
                from PyObjCTools import AppHelper
                def _inject():
                    if getattr(app_ref, "_settings_gen", 0) != gen or not app_ref._settings_refs:
                        return
                    wv = app_ref._settings_refs[1]
                    try:
                        wv.evaluateJavaScript_completionHandler_(js, None)
                    except Exception:
                        pass
                AppHelper.callAfter(_inject)

            threading.Thread(target=_do_fetch, daemon=True).start()
            return

        if title.startswith("VOITTA_CLAUDE_LINK_TOGGLE:"):
            # Reset the title so back-to-back clicks still fire KVO. Do NOT
            # mark this observer as handled — the settings window stays
            # open while the link popup is shown.
            obj.evaluateJavaScript_completionHandler_(
                "document.title = 'Voitta Desktop — Settings'", None
            )
            self._app._open_claude_link_popup()
            return

        if title == "VOITTA_SAVE":
            self._handled = True
            self._removeKVO()
            obj.evaluateJavaScript_completionHandler_(
                "JSON.stringify(collectAll())", self.onSaveData_error_
            )
            return

        elif title == "VOITTA_CANCEL":
            self._handled = True
            self._removeKVO()
            self._deferClose()

    def onSaveData_error_(self, result, error):
        # Close the window immediately so the user isn't blocked
        self._deferClose()
        if error:
            logger.error("Settings JS error: %s", error)
        elif result:
            try:
                data = json.loads(result)
            except Exception as e:
                logger.error("Settings save error: %s", e)
                return
            # Run _apply_settings in a thread: _rebuild_msal_for_app does a
            # blocking HTTP fetch (OIDC discovery) that can stall for 30s.
            app_ref = self._app
            def _apply():
                try:
                    app_ref._apply_settings(data)
                except Exception as e:
                    logger.error("Settings apply error: %s", e)
            threading.Thread(target=_apply, daemon=True).start()

    def _deferClose(self):
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, self, "doClose:", None, False
        )

    def doClose_(self, timer):
        if self._window:
            self._window.orderOut_(None)
        self._cleanupRefs()
