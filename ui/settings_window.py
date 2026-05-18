"""Settings window + Info-tab state + Claude-link + LLM-tools-status popup.

Extracted from menu.py as a mixin. Holds the four window-orchestration
methods that drive Settings, the Claude Connect/Disconnect popup, and
the LLM Tools Status popup, plus the helpers that feed them
(``_build_tool_tree``, ``_collect_info_state``).

The host (VoittaDesktopApp) needs to expose:

  self._auth, self._auth_lock, self._config, self._tracker,
  self._optimizer_pipeline, self._mcp_backends, self._bash_compressor,
  self._tool_result_optimizer, self._image_optimizer, self._thinking_optimizer,
  self.disabled_tools, self.suppress_codex_popup, self.claude_link_armed,
  self.mcp_servers,
  self.llm_proxy_port, self.mcp_proxy_port, self.llm_upstream_url,
  self._init_active_defaults, self._sync_edit_mcp_env, self._sync_jira_mcp_env,
  self._rebuild_msal_for_app, self._deauth_app, self._rebuild_menu,
  self._update_auth_state, self._promote_for_keyboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

import rumps
from AppKit import (
    NSApp, NSAlert, NSBackingStoreBuffered, NSWindow,
    NSNotificationCenter,
)
from Foundation import NSMakeRect, NSTimer, NSRunLoop
from WebKit import WKWebView

from config import save_config
from optimizers import model_family  # used by _collect_info_state
from ui.chart import _safe_json
from ui._native import (
    _notify, _show_modal, _FocusTrigger, _InfoTicker, _SettingsTitleObserver,
)

logger = logging.getLogger("voitta-desktop")


def _mcp_servers_diff(old: list[dict], new: list[dict]) -> bool:
    """True if the two mcp_servers lists differ in any field that affects
    the running proxy (URL, prefix, auth, kind, subprocess parameters).

    We don't bother with deep equality — just compare the JSON serialisations
    of the *user-meaningful* fields. Cheap, correct, no false negatives on
    nested edits.
    """
    def normalise(servers):
        return [
            {
                "name": s.get("name", ""),
                "prefix": s.get("prefix", ""),
                "description": s.get("description", ""),
                "kind": s.get("kind", "http"),
                "url": s.get("url", ""),
                "subprocess": s.get("subprocess") or {},
                "auth": s.get("auth") or {},
            }
            for s in (servers or [])
        ]
    return normalise(old) != normalise(new)


def _restart_voitta_desktop():
    """Re-exec the current binary so the new mcp_servers list takes effect.

    Uses os.execv: replaces the current process image with a fresh copy
    of itself, no new PID created, no double-startup. Works for both
    `python app.py` (terminal dev) and the bundled .app (briefcase's
    /usr/bin/python3.X wrapper). Best-effort — if the exec fails for any
    reason we fall back to a notification asking the user to restart by
    hand, so the app keeps running rather than dying mid-session.
    """
    try:
        executable = sys.executable
        argv = [executable] + sys.argv
        logger.info("Re-exec for MCP-server change: %s", argv)
        os.execv(executable, argv)
    except Exception as e:
        logger.error("Auto-restart failed: %s", e)
        _notify("Voitta Desktop",
                "Auto-restart failed",
                "Please quit and re-open Voitta Desktop to apply changes.")


class SettingsWindowMixin:
    """Mixin: Settings + Info-tab + Claude-link + Tools-status orchestration."""

    # ── Tool tree (read from on-disk cache) ──────────────────────────────────

    def _build_tool_tree(self):
        """Build tool tree by reading each backend's on-disk tool cache.

        Same data source as the LLM Tools Status popup (peek_cached). No
        HTTP, no live MCP round-trip, no dependency on _mcp_tools being
        populated — which is what stuck Settings → Tools at "Loading…".

        Per-backend caches live in ~/.voitta_desktop_cache/<name>_tools.json
        and are filled on the first listing after startup (then kept fresh
        via stale-while-revalidate).

        Order matches the user's mcp_servers list so the tools tab mirrors
        the MCPs tab.
        """
        from mcpproxy.server import tool_tree_groups

        backends = getattr(self, "_mcp_backends", None) or []
        # Map prefix → proxy. Resilient proxies expose _prefix; other types
        # (e.g. plain FastMCPProxy if someone slips one in) are ignored.
        prefix_to_proxy = {
            p._prefix: p for _label, _url, p in backends if getattr(p, "_prefix", None)
        }

        tree = []
        mcp_servers = getattr(self, "mcp_servers", None) or []
        for prefix, label in tool_tree_groups(mcp_servers):
            proxy = prefix_to_proxy.get(prefix)
            if proxy is None or not hasattr(proxy, "peek_cached_names"):
                continue
            names = proxy.peek_cached_names(prefix)
            if names:
                tree.append({"prefix": prefix, "label": label, "tools": names})
        return tree

    # ── Settings window ──────────────────────────────────────────────────────

    def show_settings(self, _):
        """Menu callback. rumps swallows exceptions raised in callbacks, so
        any path/attribute/import failure inside ``_show_settings`` would
        produce a silent click with no window. Wrap once and surface to the
        log + an alert so the bundle doesn't appear to do nothing."""
        try:
            self._show_settings()
        except Exception as e:
            logger.exception("show_settings failed: %s", e)
            try:
                rumps.alert(
                    title="Settings failed to open",
                    message=(
                        f"{type(e).__name__}: {e}\n\n"
                        f"Details written to:\n"
                        f"~/.voitta-desktop/logs/desktop.log"
                    ),
                    ok="OK",
                )
            except Exception:
                # If even the alert fails (rare; usually means NSApp isn't up
                # yet), at least the log line above already landed.
                pass

    def _show_settings(self):
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
        window.setTitle_("Voitta Desktop — Settings")
        window.setReleasedWhenClosed_(False)
        window.center()

        webview = WKWebView.alloc().initWithFrame_(window.contentView().bounds())
        webview.setAutoresizingMask_(18)
        window.contentView().addSubview_(webview)

        # Load HTML immediately with the live state injected. JS body lives
        # in a sibling settings.js — we read it here and stitch it into the
        # single <script>/*INJECT_CONFIG*/</script> placeholder.
        ui_dir = Path(__file__).parent
        html_content = (ui_dir / "settings.html").read_text(encoding="utf-8")
        js_body = (ui_dir / "settings.js").read_text(encoding="utf-8")
        # Inject the live Claude-link state so the bottom-left button shows
        # the correct label the moment the window opens.
        from claude_link import load_claude_settings, is_voitta_connected
        linked = is_voitta_connected(load_claude_settings(), self.llm_proxy_port)
        # Live Info-tab state at popup open. Subsequent updates pushed by
        # the _InfoTicker every ~3s.
        info_state = self._collect_info_state()
        # Build the tool tree synchronously from the on-disk cache so the
        # webview renders complete on first paint. No HTTP, no polling.
        tool_tree = self._build_tool_tree()
        # _safe_json escapes "</" so a user-pasted token containing
        # "</script>" can't terminate the surrounding <script> tag.
        html_content = html_content.replace(
            "/*INJECT_CONFIG*/",
            f"var _initialConfig = {_safe_json(self._config)};\n"
            f"var _initialClaudeLinked = {_safe_json(linked)};\n"
            f"var _initialInfo = {_safe_json(info_state)};\n"
            f"var _toolTree = {_safe_json(tool_tree)};\n"
            + js_body,
        )
        webview.loadHTMLString_baseURL_(html_content, None)

        observer = _SettingsTitleObserver.alloc().initWithApp_window_gen_(self, window, gen)
        webview.addObserver_forKeyPath_options_context_(observer, "title", 1, None)
        observer._webview = webview

        # Handle the red X close button — clean up KVO before deallocation
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

    def _apply_settings(self, new_config):
        """Apply new settings. Safe to call from any thread — UI updates
        are dispatched to the main thread via AppHelper.callAfter."""
        from PyObjCTools import AppHelper

        # Detect changes that need a process restart to take effect. The
        # FastMCP proxy mounts each MCP server at startup with a closed-over
        # client factory; we don't currently support live add/remove. Diff
        # the user's mcp_servers against what's running and prompt to
        # restart if anything changed.
        mcp_servers_changed = _mcp_servers_diff(
            self.mcp_servers, new_config.get("mcp_servers", [])
        )

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

        self.mcp_servers = list(new_config.get("mcp_servers", []))
        self.disabled_tools = set(new_config.get("disabled_tools", []))
        tools_cfg = new_config.get("tools", {})
        self.suppress_codex_popup = bool(tools_cfg.get("suppress_codex_popup", True))
        link_cfg = new_config.get("claude_link", {})
        self.claude_link_armed = bool(link_cfg.get("armed", False))

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
            if mcp_servers_changed:
                self._prompt_restart_for_mcp_changes()
        AppHelper.callAfter(_update_ui)

    def _prompt_restart_for_mcp_changes(self):
        """Show a restart prompt after the user changed mcp_servers.

        Two buttons: Restart Now (re-exec the current binary) and Later
        (just acknowledge — the changes are persisted but not live until
        the next launch).
        """
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Restart required")
        alert.setInformativeText_(
            "MCP server changes were saved, but the running proxy still has "
            "the old mounts. Restart Voitta Desktop to load the new set."
        )
        alert.addButtonWithTitle_("Restart Now")
        alert.addButtonWithTitle_("Later")
        response = _show_modal(alert)
        if response == 1000:  # NSAlertFirstButtonReturn → Restart Now
            _restart_voitta_desktop()

    # ── Info-tab state ───────────────────────────────────────────────────────

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

    # ── Claude link popup ────────────────────────────────────────────────────

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

            # Record the user's intent. Connect → armed=True so the next
            # start auto-arms after quit-time disarm; Disconnect → armed=False
            # so quit stops touching settings.json.
            self.claude_link_armed = (plan.target == "connect")
            self._config.setdefault("claude_link", {})["armed"] = self.claude_link_armed
            save_config(self._config)

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

    # ── LLM Tools Status popup ───────────────────────────────────────────────

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
