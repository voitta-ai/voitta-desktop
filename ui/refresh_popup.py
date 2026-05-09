"""LLM Tools Status popup — live view of MCP backend tool inventories.

Shows one row per backend with cached tool count and refresh/cancel controls.
Opening the popup does NOT initiate any work; refresh is user-driven via the
per-row ↻ buttons or the bottom "Refresh All" button.

Lifecycle:
    popup = StatusPopup()
    popup.set_refresh_handler(lambda idx: ...)
    popup.set_refresh_all_handler(lambda: ...)
    popup.set_cancel_handler(lambda idx: ...)
    popup.set_close_handler(lambda: ...)
    popup.open(rows)                    # main thread; rows = [(name, url, state, label)]
    popup.update(idx, state, label)     # any thread; states: idle/fetching/ok/fail
    popup.close()                       # main thread

State convention:
    idle     — no recent action; shows cached count or "Not loaded"
    fetching — refresh in progress; shows ✕ cancel button
    ok       — last refresh succeeded; ↻ refresh available
    fail     — last refresh failed/cancelled; ↻ refresh available
"""

import json
import logging

import objc
from AppKit import (
    NSApp, NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSScreen,
)
from Foundation import NSMakeRect, NSObject, NSNotificationCenter
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore

logger = logging.getLogger("voitta-desktop.tools_status")


def _build_html(rows: list[tuple[str, str, str, str]]) -> str:
    """rows: list of (name, url, state, label)."""
    rows_json = json.dumps([
        {"name": n, "url": u, "state": s, "label": l} for n, u, s, l in rows
    ])
    return r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  :root {
    --bg: #f5f5f7;
    --card-bg: #ffffff;
    --card-border: #d2d2d7;
    --text: #1d1d1f;
    --text-secondary: #86868b;
    --accent: #0071e3;
    --danger: #ff3b30;
    --ok: #2ea043;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1c1e;
      --card-bg: #2c2c2e;
      --card-border: #3a3a3c;
      --text: #f5f5f7;
      --text-secondary: #98989d;
      --accent: #0a84ff;
      --danger: #ff453a;
      --ok: #30d158;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 13px;
    color: var(--text);
    background: var(--bg);
    padding: 16px;
    -webkit-user-select: none;
    user-select: none;
  }
  h1 { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
  .subtitle { font-size: 12px; color: var(--text-secondary); margin-bottom: 14px; }
  .list {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 10px;
    overflow: hidden;
  }
  .row {
    display: flex;
    align-items: center;
    padding: 9px 12px;
    border-bottom: 1px solid var(--card-border);
    font-size: 12px;
    gap: 12px;
  }
  .row:last-child { border-bottom: none; }
  .name { flex: 0 0 150px; font-weight: 600; }
  .url {
    flex: 1;
    color: var(--text-secondary);
    font-family: ui-monospace, "SF Mono", monospace;
    font-size: 11px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .status {
    flex: 0 0 220px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    font-variant-numeric: tabular-nums;
  }
  .status-text { white-space: nowrap; }
  .status-text.idle     { color: var(--text-secondary); }
  .status-text.fetching { color: var(--accent); }
  .status-text.ok       { color: var(--ok); }
  .status-text.fail     { color: var(--danger); }
  .row-btn {
    border: none;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
    padding: 2px 7px;
    border-radius: 4px;
    font-size: 13px;
    line-height: 1;
    min-width: 24px;
  }
  .row-btn:hover { background: var(--card-border); }
  .row-btn.refresh:hover { color: var(--accent); }
  .row-btn.cancel:hover  { color: var(--danger); }
  .bottom {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 14px;
  }
  .btn {
    padding: 5px 14px;
    border-radius: 6px;
    border: 1px solid var(--card-border);
    background: var(--card-bg);
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
  }
  .btn:hover { background: var(--card-border); }
  .btn-primary {
    border-color: var(--accent);
    color: var(--accent);
  }
</style>
</head>
<body>
<h1>LLM Tools Status</h1>
<div class="subtitle">Shows the cached tool inventory for each MCP backend. Refresh individually with ↻ or all at once.</div>
<div class="list" id="list"></div>
<div class="bottom">
  <button class="btn btn-primary" onclick="refreshAll()">Refresh All</button>
  <button class="btn" onclick="closeWindow()">Close</button>
</div>
<script>
var _backends = __ROWS__;

function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function render() {
  var c = document.getElementById('list');
  c.innerHTML = _backends.map(function(b, i) {
    var btn;
    if (b.state === 'fetching') {
      btn = '<button class="row-btn cancel" onclick="cancelOne(' + i + ')" title="Cancel">✕</button>';
    } else {
      btn = '<button class="row-btn refresh" onclick="refreshOne(' + i + ')" title="Refresh">↻</button>';
    }
    return '<div class="row">' +
             '<div class="name">' + esc(b.name) + '</div>' +
             '<div class="url" title="' + esc(b.url) + '">' + esc(b.url) + '</div>' +
             '<div class="status">' +
               '<span class="status-text ' + b.state + '">' + esc(b.label) + '</span>' +
               btn +
             '</div>' +
           '</div>';
  }).join('');
}

function _setBackend(i, state, label) {
  if (i < 0 || i >= _backends.length) return;
  _backends[i].state = state;
  _backends[i].label = label;
  render();
}

// Each title-write needs a unique salt so KVO observes a change even when
// the same backend is clicked twice in a row.
function _salt() { return Math.random().toString(36).slice(2); }

function refreshOne(i) { document.title = 'STATUS_REFRESH_ONE:' + i + ':' + _salt(); }
function cancelOne(i)  { document.title = 'STATUS_CANCEL:' + i + ':' + _salt(); }
function refreshAll()  { document.title = 'STATUS_REFRESH_ALL:' + _salt(); }
function closeWindow() { document.title = 'STATUS_CLOSE'; }

render();
</script>
</body></html>""".replace("__ROWS__", rows_json)


class _TitleObserver(NSObject):
    """KVO on webview.title; dispatches popup-level events."""

    def initWithPopup_(self, popup):
        self = objc.super(_TitleObserver, self).init()
        if self is not None:
            self._popup = popup
        return self

    def observeValueForKeyPath_ofObject_change_context_(self, keyPath, obj, change, context):
        try:
            title = obj.title() or ""
        except Exception:
            return

        if title == "STATUS_CLOSE":
            self._popup.close()
            return

        if title.startswith("STATUS_REFRESH_ONE:"):
            handler = self._popup._refresh_handler
            idx = self._parse_idx(title)
            if handler is not None and idx is not None:
                self._safe_call(handler, idx)
            return

        if title.startswith("STATUS_CANCEL:"):
            handler = self._popup._cancel_handler
            idx = self._parse_idx(title)
            if handler is not None and idx is not None:
                self._safe_call(handler, idx)
            return

        if title.startswith("STATUS_REFRESH_ALL:"):
            handler = self._popup._refresh_all_handler
            if handler is not None:
                self._safe_call(handler)
            return

    @staticmethod
    def _parse_idx(title: str) -> int | None:
        parts = title.split(":")
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    @staticmethod
    def _safe_call(fn, *args):
        try:
            fn(*args)
        except Exception as e:
            logger.warning("popup handler raised: %s", e)


class StatusPopup:
    def __init__(self):
        self.window = None
        self.webview = None
        self.observer = None
        self._closed = False
        self._refresh_handler = None      # callable(idx: int) -> None
        self._refresh_all_handler = None  # callable() -> None
        self._cancel_handler = None       # callable(idx: int) -> None
        self._close_handler = None        # callable() -> None

    def set_refresh_handler(self, handler):
        self._refresh_handler = handler

    def set_refresh_all_handler(self, handler):
        self._refresh_all_handler = handler

    def set_cancel_handler(self, handler):
        self._cancel_handler = handler

    def set_close_handler(self, handler):
        """Invoked once when the popup is about to close (red X or Close)."""
        self._close_handler = handler

    def open(self, rows: list[tuple[str, str, str, str]]):
        """Open the popup window (call from main thread).

        rows: list of (name, url, state, label). State is one of
        idle/fetching/ok/fail; label is the display text for that row.
        """
        screen = NSScreen.mainScreen().frame()
        width, height = 640, max(260, 110 + len(rows) * 36)
        x = (screen.size.width - width) / 2
        y = (screen.size.height - height) / 2

        mask = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, width, height), mask, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("LLM Tools Status")
        self.window.setReleasedWhenClosed_(False)
        self.window.setLevel_(NSFloatingWindowLevel)

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        self.webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, width, height), cfg
        )
        self.webview.setAutoresizingMask_(18)
        self.window.setContentView_(self.webview)

        self.observer = _TitleObserver.alloc().initWithPopup_(self)
        self.webview.addObserver_forKeyPath_options_context_(self.observer, "title", 1, None)

        self.webview.loadHTMLString_baseURL_(_build_html(rows), None)

        # NSWindowWillClose covers both the red X and our own .close() path,
        # so the close handler fires exactly once regardless of how the
        # window goes away.
        def _on_will_close(notification):
            if self._closed:
                return
            self._cleanup_observer()
            self._closed = True
            if self._close_handler is not None:
                try:
                    self._close_handler()
                except Exception as e:
                    logger.warning("close handler raised: %s", e)

        NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            "NSWindowWillCloseNotification", self.window, None, _on_will_close
        )

        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def _cleanup_observer(self):
        if self.observer is not None and self.webview is not None:
            try:
                self.webview.removeObserver_forKeyPath_(self.observer, "title")
            except Exception:
                pass
            self.observer = None

    def update(self, idx: int, state: str, label: str):
        """Push a status change for row `idx`. Safe from any thread."""
        if self._closed:
            return
        from PyObjCTools import AppHelper
        js = f"_setBackend({idx}, {json.dumps(state)}, {json.dumps(label)});"

        def _do():
            if self._closed or self.webview is None:
                return
            try:
                self.webview.evaluateJavaScript_completionHandler_(js, None)
            except Exception as e:
                logger.debug("popup update failed: %s", e)

        AppHelper.callAfter(_do)

    def close(self):
        """Close the window (call from main thread). Idempotent."""
        if self.window is not None:
            try:
                self.window.close()  # triggers NSWindowWillClose -> _on_will_close
            except Exception:
                pass
