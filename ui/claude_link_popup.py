"""Diff popup for Claude Code link/unlink (Connect/Disconnect button).

Modeled after refresh_popup.py — small WebKit window with KVO on title for
the OK/Cancel signal. The window shows the file path being modified and
the list of plan changes (label, old, new), with colors mapped to the
change kind.
"""
from __future__ import annotations

import json
import logging

import objc
from AppKit import (
    NSApp, NSBackingStoreBuffered, NSFloatingWindowLevel,
    NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSScreen,
)
from Foundation import NSMakeRect, NSObject, NSNotificationCenter
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore

from claude_link import Plan, CLAUDE_SETTINGS_PATH

logger = logging.getLogger("voitta-desktop.claude_link_popup")


def _change_rows_html(plan: Plan) -> str:
    rows = []
    for c in plan.claude_changes:
        rows.append({
            "scope": "~/.claude/settings.json",
            "label": c.label,
            "old": c.old,
            "new": c.new,
            "kind": c.kind,
        })
    if plan.voitta_upstream_change is not None:
        c = plan.voitta_upstream_change
        rows.append({
            "scope": "Voitta Desktop",
            "label": c.label,
            "old": c.old,
            "new": c.new,
            "kind": c.kind,
        })
    return json.dumps(rows)


def _build_html(plan: Plan, title: str, ok_label: str) -> str:
    rows_json = _change_rows_html(plan)
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
    --add: #2ea043;
    --remove: #ff3b30;
    --change: #d97706;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1c1e;
      --card-bg: #2c2c2e;
      --card-border: #3a3a3c;
      --text: #f5f5f7;
      --text-secondary: #98989d;
      --accent: #0a84ff;
      --add: #30d158;
      --remove: #ff453a;
      --change: #f59e0b;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 13px; color: var(--text); background: var(--bg);
    padding: 18px;
    -webkit-user-select: none; user-select: none;
  }
  h1 { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
  .subtitle {
    font-size: 12px; color: var(--text-secondary); margin-bottom: 14px;
  }
  .scope-group {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 10px;
    margin-bottom: 10px;
    overflow: hidden;
  }
  .scope-header {
    padding: 8px 12px;
    background: var(--card-border);
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-family: ui-monospace, "SF Mono", monospace;
  }
  .row {
    padding: 9px 12px;
    border-bottom: 1px solid var(--card-border);
    font-size: 12px;
  }
  .row:last-child { border-bottom: none; }
  .label {
    font-family: ui-monospace, "SF Mono", monospace;
    font-weight: 600;
    margin-bottom: 4px;
  }
  .label.add { color: var(--add); }
  .label.remove { color: var(--remove); }
  .label.change { color: var(--change); }
  .label .badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 700;
    margin-right: 6px;
    background: var(--card-border);
    color: var(--text);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .label.add    .badge { background: var(--add);    color: #fff; }
  .label.remove .badge { background: var(--remove); color: #fff; }
  .label.change .badge { background: var(--change); color: #fff; }
  .values {
    font-family: ui-monospace, "SF Mono", monospace;
    font-size: 11px;
    color: var(--text-secondary);
    word-break: break-all;
    line-height: 1.5;
  }
  .arrow { color: var(--text-secondary); margin: 0 6px; }
  .empty {
    padding: 24px; text-align: center;
    color: var(--text-secondary); font-size: 12px;
  }
  .bottom {
    display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px;
  }
  .btn {
    padding: 5px 14px; border-radius: 6px;
    border: 1px solid var(--card-border);
    background: var(--card-bg); color: var(--text);
    font-size: 12px; cursor: pointer;
  }
  .btn:hover { background: var(--card-border); }
  .btn-primary {
    border-color: var(--accent); color: #fff; background: var(--accent);
  }
  .btn-primary:hover { opacity: 0.9; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="subtitle">__SUBTITLE__</div>
<div id="content"></div>
<div class="bottom">
  <button class="btn" onclick="cancel()">Cancel</button>
  <button class="btn btn-primary" onclick="confirm()">__OK_LABEL__</button>
</div>
<script>
var _rows = __ROWS__;

function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }

function fmtVal(v) {
  if (v == null) return '<span style="font-style:italic;">(absent)</span>';
  return esc(v);
}

function render() {
  var c = document.getElementById('content');
  if (!_rows.length) {
    c.innerHTML = '<div class="empty">No changes — already in target state.</div>';
    return;
  }
  // Group by scope.
  var groups = {};
  var order = [];
  _rows.forEach(function(r) {
    if (!(r.scope in groups)) { groups[r.scope] = []; order.push(r.scope); }
    groups[r.scope].push(r);
  });
  c.innerHTML = order.map(function(scope) {
    var rows = groups[scope].map(function(r) {
      var badge = (r.kind === 'add') ? 'add' : (r.kind === 'remove' ? 'remove' : 'change');
      var values;
      if (r.kind === 'add') {
        values = '<span class="arrow">+</span> ' + fmtVal(r.new);
      } else if (r.kind === 'remove') {
        values = fmtVal(r.old) + ' <span class="arrow">→</span> <span style="font-style:italic;">(removed)</span>';
      } else {
        values = fmtVal(r.old) + ' <span class="arrow">→</span> ' + fmtVal(r.new);
      }
      return '<div class="row">' +
               '<div class="label ' + badge + '"><span class="badge">' + badge + '</span>' + esc(r.label) + '</div>' +
               '<div class="values">' + values + '</div>' +
             '</div>';
    }).join('');
    return '<div class="scope-group">' +
             '<div class="scope-header">' + esc(scope) + '</div>' +
             rows +
           '</div>';
  }).join('');
}

function _salt() { return Math.random().toString(36).slice(2); }
function confirm() { document.title = 'CLAUDE_LINK_OK:' + _salt(); }
function cancel()  { document.title = 'CLAUDE_LINK_CANCEL:' + _salt(); }

render();
</script>
</body></html>""".replace("__TITLE__", title) \
                .replace("__SUBTITLE__", f"Reviewing changes to {CLAUDE_SETTINGS_PATH}") \
                .replace("__OK_LABEL__", ok_label) \
                .replace("__ROWS__", rows_json)


class _PopupObserver(NSObject):
    """KVO on webview.title; dispatch confirm/cancel back to popup."""
    def initWithPopup_(self, popup):
        self = objc.super(_PopupObserver, self).init()
        if self is not None:
            self._popup = popup
            self._handled = False
        return self

    def observeValueForKeyPath_ofObject_change_context_(self, keyPath, obj, change, context):
        if self._handled:
            return
        try:
            title = obj.title() or ""
        except Exception:
            return
        if title.startswith("CLAUDE_LINK_OK:"):
            self._handled = True
            self._popup._on_confirm()
        elif title.startswith("CLAUDE_LINK_CANCEL:"):
            self._handled = True
            self._popup._on_cancel()


class ClaudeLinkPopup:
    """Diff popup for the Connect/Disconnect button."""

    def __init__(self):
        self.window = None
        self.webview = None
        self.observer = None
        self._closed = False
        self._confirm_handler = None
        self._cancel_handler = None

    def set_handlers(self, on_confirm, on_cancel):
        self._confirm_handler = on_confirm
        self._cancel_handler = on_cancel

    def open(self, plan: Plan, *, title: str, ok_label: str):
        screen = NSScreen.mainScreen().frame()
        rows = len(plan.claude_changes) + (1 if plan.voitta_upstream_change else 0)
        width, height = 580, max(280, 150 + rows * 60)
        x = (screen.size.width - width) / 2
        y = (screen.size.height - height) / 2

        mask = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, width, height), mask, NSBackingStoreBuffered, False
        )
        self.window.setTitle_(title)
        self.window.setReleasedWhenClosed_(False)
        self.window.setLevel_(NSFloatingWindowLevel)

        cfg = WKWebViewConfiguration.alloc().init()
        cfg.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        self.webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, width, height), cfg
        )
        self.webview.setAutoresizingMask_(18)
        self.window.setContentView_(self.webview)

        self.observer = _PopupObserver.alloc().initWithPopup_(self)
        self.webview.addObserver_forKeyPath_options_context_(self.observer, "title", 1, None)
        self.webview.loadHTMLString_baseURL_(_build_html(plan, title, ok_label), None)

        # Red-X close behaves like Cancel.
        def _on_will_close(notification):
            if self._closed:
                return
            self._cleanup_observer()
            self._closed = True
            if self.observer is not None and not self.observer._handled:
                if self._cancel_handler is not None:
                    try: self._cancel_handler()
                    except Exception as e: logger.warning("cancel handler raised: %s", e)

        NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            "NSWindowWillCloseNotification", self.window, None, _on_will_close
        )

        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def _cleanup_observer(self):
        if self.observer is not None and self.webview is not None:
            try: self.webview.removeObserver_forKeyPath_(self.observer, "title")
            except Exception: pass

    def _on_confirm(self):
        try:
            if self._confirm_handler is not None:
                self._confirm_handler()
        finally:
            self.close()

    def _on_cancel(self):
        try:
            if self._cancel_handler is not None:
                self._cancel_handler()
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cleanup_observer()
        if self.window is not None:
            try: self.window.close()
            except Exception: pass
