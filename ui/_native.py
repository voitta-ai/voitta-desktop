"""PyObjC plumbing for the menu-bar app.

Pulled out of ``ui/menu.py`` so that file can stay focused on the
``VoittaDesktopApp`` orchestration. Everything here is either:

  • a module-level helper that wraps an AppKit pattern (port checks,
    notifications, NSAlert focus tricks); or
  • an NSObject subclass driven by the main app via duck-typed
    ``app_ref`` references.

NSObject subclasses keep their own state through PyObjC init helpers
(``initWithApp_gen_`` etc.) — they don't call back into a specific
class, only into whatever attributes ``app_ref`` exposes
(``_collect_info_state``, ``_apply_settings``, ``_open_claude_link_popup``,
``_settings_gen``, ``_settings_refs``, ``_demote_after_keyboard``).
"""
from __future__ import annotations

import json
import logging
import socket

import objc
import rumps
from AppKit import (
    NSApp, NSFloatingWindowLevel,
)
from Foundation import NSObject, NSTimer, NSRunLoop

from auth.jira import fetch_jira_projects
from runtime import runtime
from ui.main_thread import on_main_thread

logger = logging.getLogger("voitta-desktop")


@on_main_thread
def _settings_inject_js(app_ref, gen, js):
    """Push JS into the still-open settings webview from a worker thread.

    The generation guard stops a slow worker writing into a window that was
    closed and reopened underneath it.
    """
    if getattr(app_ref, "_settings_gen", 0) != gen or not app_ref._settings_refs:
        return
    wv = app_ref._settings_refs[1]
    try:
        wv.evaluateJavaScript_completionHandler_(js, None)
    except Exception:
        logger.debug("settings JS injection failed", exc_info=True)


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
                "document.title = 'Voitta Desktop — Settings'", None
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
                _settings_inject_js(app_ref, gen, js)

            runtime.run_blocking(_do_fetch)
            return

        if title.startswith("VOITTA_MCP_CTL:"):
            # Start/stop/restart/status a controllable subprocess MCP server.
            # Payload: base64(json {"id": "<prefix>", "action": "start|stop|restart|status"}).
            import base64
            payload = title.split(":", 1)[1].split("#", 1)[0]
            obj.evaluateJavaScript_completionHandler_(
                "document.title = 'Voitta Desktop — Settings'", None
            )
            try:
                req = json.loads(base64.b64decode(payload).decode("utf-8"))
                sid = req["id"]; action = req["action"]
            except Exception:
                return
            app_ref = self._app
            gen = self._gen

            def _do_ctl():
                fn = {
                    "start": app_ref.start_mcp_server,
                    "stop": app_ref.stop_mcp_server,
                    "restart": app_ref.restart_mcp_server,
                    "status": app_ref.mcp_server_status,
                }.get(action, app_ref.mcp_server_status)
                try:
                    status = fn(sid)
                except Exception as e:
                    status = {"id": sid, "state": "unknown", "error": str(e)}
                _settings_inject_js(app_ref, gen, f"_setMcpServerStatus({json.dumps(status)})")

            runtime.run_blocking(_do_ctl)
            return

        if title.startswith("VOITTA_MCP_LOG:"):
            # Fetch a subprocess MCP server's captured stdout/stderr.
            # Payload: base64(server id).
            import base64
            payload = title.split(":", 1)[1].split("#", 1)[0]
            obj.evaluateJavaScript_completionHandler_(
                "document.title = 'Voitta Desktop — Settings'", None
            )
            try:
                sid = base64.b64decode(payload).decode("utf-8")
            except Exception:
                return
            app_ref = self._app
            gen = self._gen

            def _do_log():
                try:
                    text = app_ref.read_mcp_server_log(sid)
                except Exception as e:
                    text = f"(could not read log: {e})"
                _settings_inject_js(
                    app_ref, gen,
                    f"_setMcpServerLog({json.dumps({'id': sid, 'text': text})})",
                )

            runtime.run_blocking(_do_log)
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
            runtime.run_blocking(_apply)

    def _deferClose(self):
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, self, "doClose:", None, False
        )

    def doClose_(self, timer):
        if self._window:
            self._window.orderOut_(None)
        self._cleanupRefs()
