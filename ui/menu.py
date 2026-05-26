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
import sys
import threading
import time
from pathlib import Path

import rumps
from AppKit import (
    NSApp, NSApplication,
    NSApplicationActivationPolicyAccessory,
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
from WebKit import WKWebView

from config import (
    load_config, save_config, migrate_from_legacy, apps_for_backend,
    CONFIG_PATH, CONFIG_DIR,
)
from middleware import ConversationTracker, RequestLogger
from middleware.cache_sim import CacheSimulator
from optimizers import OptimizerPipeline
from optimizers.bash_compress import BashCompressor
from optimizers.image import ImageOptimizer
from optimizers.thinking import ThinkingOptimizer
from optimizers.tool_result import ToolResultOptimizer
from proxy import AnthropicProxy
from mcpproxy.server import run_mcp_proxy
from ui.chart import generate_chart_html
from ui._native import (
    _notify, _FocusTrigger, _InfoTicker, _is_port_free, _grab_free_port,
    _show_modal, _SettingsTitleObserver,
)
from ui.auth_flows import AuthFlowsMixin
from ui.conv_menu import ConvMenuMixin
from ui.menu_builder import MenuBuilderMixin
from ui.mcp_lifecycle import MCPLifecycleMixin, OAUTH_REDIRECT_PORT
from ui.settings_window import SettingsWindowMixin

logger = logging.getLogger("voitta-desktop")

LEGACY_SETTINGS_PATH = Path.home() / ".voitta_auth_settings.json"

ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images", "icon_menubar_bright.png")



# ── Main App ─────────────────────────────────────────────────────────────────

class VoittaDesktopApp(
    AuthFlowsMixin, MCPLifecycleMixin, ConvMenuMixin, MenuBuilderMixin,
    SettingsWindowMixin, rumps.App,
):
    def __init__(self):
        super().__init__("VoittaDesktop", title=None, quit_button=None)

        self._noop = lambda _: None
        self._auth_lock = threading.Lock()
        self._auth = {}

        # Load config
        self._config = self._load_or_migrate_config()
        mcp_proxy_cfg = self._config.get("mcp_proxy", {})
        llm_proxy_cfg = self._config.get("llm_proxy", {})

        # Unified list of editable MCP backends — server.py + lifecycle drive
        # everything off this. The legacy mcp_proxy URL/key fields and the
        # mcp_subprocess block are still present in apps.json (kept for
        # rollback safety) but no code reads them anymore.
        self.mcp_servers = list(self._config.get("mcp_servers", []))
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
        tools_cfg = self._config.get("tools", {})
        self.suppress_codex_popup = bool(tools_cfg.get("suppress_codex_popup", True))
        link_cfg = self._config.get("claude_link", {})
        self.claude_link_armed = bool(link_cfg.get("armed", False))
        self._mcp_tools = {}
        self._mcp_upstream_instructions: dict[str, str] = {}

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
        self.terminal_mode = False
        self._tracker.app_ref = self  # enables notify_update callbacks

        # Build menu
        self._menu_items = {}
        self._conv_menus: dict[str, rumps.MenuItem] = {}
        self._build_menu()
        self._update_auth_state()
        self._install_edit_shortcuts()

        # Start background servers
        threading.Thread(target=self._run_llm_proxy, daemon=True).start()
        threading.Thread(target=self._run_mcp_proxy, daemon=True).start()

        # Claude link lifecycle: re-arm now if user intent says so; register
        # the disarm hook for graceful shutdown (atexit) AND for force-quit
        # via the dock/Cmd-Q path (NSApplicationWillTerminate is delivered
        # synchronously to atexit handlers by rumps.quit_application).
        self._rearm_claude_link_if_intended()
        atexit.register(self._disarm_claude_link_on_quit)

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

        # _resolve_port runs from __init__, before rumps.App.run() boots the
        # NSApplication. PyObjC's `NSApp` resolves lazily and returns None
        # until sharedApplication() has been called, which is why this branch
        # crashed the first time someone hit a busy port. Materialize it now
        # so the alert (and any subsequent NSApp.* call) work pre-run.
        NSApplication.sharedApplication()
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
            run_mcp_proxy(self, self.mcp_proxy_port)
        except BaseException as e:
            logger.error("MCP proxy failed: %s", e, exc_info=True)

    def notify_update(self) -> None:
        """No-op — Mac UI polls on a 2-second timer instead."""

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

    # ── About ────────────────────────────────────────────────────────────────

    def show_about(self, _):
        try:
            from voitta_desktop._version import __version__
        except ImportError:
            __version__ = "dev"
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Desktop")
        mcp_lines = []
        for s in self.mcp_servers:
            name = s.get("name") or s.get("prefix", "?")
            kind = s.get("kind", "http")
            if kind == "subprocess":
                sp = s.get("subprocess") or {}
                tpl = sp.get("template", "")
                if tpl == "npx":
                    mcp_lines.append(f"  {name}  stdio:npx {sp.get('package', '?')}")
                elif tpl == "command":
                    cmd = " ".join(str(c) for c in (sp.get("command") or [])[:2])
                    mcp_lines.append(f"  {name}  stdio:{cmd}")
                else:
                    mcp_lines.append(f"  {name}  http://127.0.0.1:{sp.get('port', '?')}/mcp")
            else:
                mcp_lines.append(f"  {name}  {s.get('url', '')}")
        alert.setInformativeText_(
            f"Version {__version__}\n\n"
            f"MCP proxy:  http://127.0.0.1:{self.mcp_proxy_port}/mcp\n"
            f"LLM proxy:  http://127.0.0.1:{self.llm_proxy_port}\n\n"
            "Connected MCP servers:\n"
            + ("\n".join(mcp_lines) if mcp_lines else "  (none)")
        )
        alert.addButtonWithTitle_("OK")
        _show_modal(alert)

    # ── Help ─────────────────────────────────────────────────────────────────

    def show_help(self, _):
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Voitta Desktop Help")
        # Enumerate the configured MCPs straight from self.mcp_servers so the
        # help text reflects what's actually mounted.
        mcp_lines = []
        for s in self.mcp_servers:
            name = s.get("name") or s.get("prefix", "?")
            if s.get("kind") == "subprocess":
                port = (s.get("subprocess") or {}).get("port", "?")
                mcp_lines.append(f"  {name} \u2192 http://127.0.0.1:{port}/mcp")
            else:
                mcp_lines.append(f"  {name} \u2192 {s.get('url', '')}")
        alert.setInformativeText_(
            "Voitta Desktop sits in your menu bar.\n\n"
            "Auth section: click providers to connect/disconnect.\n"
            "Conversations section: see live Claude Code sessions.\n\n"
            f"MCP proxy: http://127.0.0.1:{self.mcp_proxy_port}/mcp\n"
            + "\n".join(mcp_lines) + "\n\n"
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

    def _quit(self, _):
        rumps.quit_application()
