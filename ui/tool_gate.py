"""Tool gate popup — shows a tool tree with toggles when an MCP client requests tools/list.

The popup outlives the request that opens it. See the "Gate state" block
below for why, and for the cancellation contract callers must honour.
"""

import asyncio
import json
import logging
import threading

import objc
from AppKit import (
    NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBackingStoreBuffered, NSApp, NSScreen, NSFloatingWindowLevel,
)
from Foundation import NSMakeRect, NSObject, NSTimer, NSRunLoop
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore

logger = logging.getLogger("voitta-desktop.tool_gate")


def _build_html(tool_groups: list[dict], disabled_tools: set[str], meta: dict | None = None) -> str:
    """Build the tool gate popup HTML."""
    groups_json = json.dumps(tool_groups)
    disabled_json = json.dumps(sorted(disabled_tools))
    meta = meta or {}

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root {{
    --bg: #f5f5f7; --card-bg: #ffffff; --card-border: #d2d2d7;
    --text: #1d1d1f; --text-secondary: #86868b; --accent: #0071e3;
    --input-bg: #ffffff; --input-border: #c7c7cc; --section-bg: #e8e8ed;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1c1c1e; --card-bg: #2c2c2e; --card-border: #3a3a3c;
      --text: #f5f5f7; --text-secondary: #98989d; --accent: #0a84ff;
      --input-bg: #1c1c1e; --input-border: #48484a; --section-bg: #38383a;
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 13px; color: var(--text); background: var(--bg);
    padding: 16px; display: flex; flex-direction: column; height: 100vh;
    -webkit-user-select: none; user-select: none;
  }}
  h2 {{
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px; color: var(--text-secondary); margin-bottom: 8px;
  }}
  .meta {{
    display: grid; grid-template-columns: auto 1fr; gap: 2px 10px;
    font-size: 11px; margin-bottom: 10px; padding: 8px 10px;
    background: var(--section-bg); border-radius: 6px;
  }}
  .meta-label {{ color: var(--text-secondary); }}
  .meta-value {{ color: var(--text); font-family: ui-monospace, "SF Mono", monospace; font-size: 10px; -webkit-user-select: text; user-select: text; cursor: text; }}
  .scroll {{ flex: 1; overflow-y: auto; margin-bottom: 12px; }}
  .tool-tree {{ list-style: none; padding: 0; margin: 0; }}
  .tool-group {{ margin-bottom: 2px; }}
  .tool-group-header {{
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; cursor: pointer; border-radius: 6px;
  }}
  .tool-group-header:hover {{ background: var(--section-bg); }}
  .tool-group-arrow {{
    font-size: 10px; width: 12px; text-align: center;
    color: var(--text-secondary); transition: transform 0.15s;
  }}
  .tool-group-arrow.open {{ transform: rotate(90deg); }}
  .tool-group-label {{ font-size: 13px; font-weight: 600; flex: 1; }}
  .tool-group-count {{ font-size: 11px; color: var(--text-secondary); }}
  .tool-children {{ list-style: none; padding: 0 0 0 32px; margin: 0; display: none; }}
  .tool-children.open {{ display: block; }}
  .tool-item {{
    display: flex; align-items: center; gap: 8px;
    padding: 4px 12px; border-radius: 4px;
    font-size: 12px; font-family: ui-monospace, "SF Mono", monospace; color: var(--text);
  }}
  .tool-item:hover {{ background: var(--section-bg); }}
  .toggle {{
    position: relative; width: 32px; height: 18px;
    background: var(--input-border); border-radius: 9px;
    cursor: pointer; flex-shrink: 0; transition: background 0.2s;
  }}
  .toggle.on {{ background: var(--accent); }}
  .toggle.partial {{ background: var(--accent); opacity: 0.5; }}
  .toggle::after {{
    content: ''; position: absolute; top: 2px; left: 2px;
    width: 14px; height: 14px; background: white;
    border-radius: 50%; transition: transform 0.2s;
  }}
  .toggle.on::after, .toggle.partial::after {{ transform: translateX(14px); }}
  .bottom-bar {{
    display: flex; justify-content: flex-end; gap: 8px;
    padding-top: 12px; border-top: 1px solid var(--card-border);
  }}
  .btn {{
    padding: 6px 20px; border-radius: 6px; font-size: 13px;
    font-weight: 500; cursor: pointer; border: 1px solid var(--input-border);
    background: var(--card-bg); color: var(--text);
  }}
  .btn:hover {{ background: var(--section-bg); }}
  .btn-primary {{
    background: var(--accent); color: white; border-color: var(--accent);
  }}
  .btn-primary:hover {{ opacity: 0.9; }}
  .tool-filter {{
    width: 100%; padding: 7px 10px;
    border: 1px solid var(--input-border); border-radius: 6px;
    background: var(--input-bg); color: var(--text);
    font-size: 12px; margin-bottom: 8px; outline: none;
  }}
  .tool-filter:focus {{
    border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(0,113,227,0.2);
  }}
</style>
</head>
<body>
<h2>MCP Tools — Select tools to expose</h2>
<div class="meta">
  {"".join(f'<span class="meta-label">{k}</span><span class="meta-value">{v}</span>' for k, v in meta.items())}
</div>
<input class="tool-filter" placeholder="Filter tools\u2026" oninput="filterTools(this.value)">
<div class="scroll">
  <ul class="tool-tree" id="tool-tree"></ul>
</div>
<div class="bottom-bar">
  <button class="btn" onclick="doCancel()">Cancel</button>
  <button class="btn btn-primary" onclick="doOk()">OK</button>
</div>
<script>
var _disabledSet = new Set({disabled_json});
var _toolGroups = {groups_json};

function renderToolTree() {{
  var container = document.getElementById('tool-tree');
  container.innerHTML = '';
  _toolGroups.forEach(function(group) {{
    var li = document.createElement('li');
    li.className = 'tool-group';
    li.dataset.prefix = group.prefix;

    var header = document.createElement('div');
    header.className = 'tool-group-header';

    var arrow = document.createElement('span');
    arrow.className = 'tool-group-arrow';
    arrow.textContent = '\\u25B6';

    var toggle = document.createElement('span');
    toggle.className = 'toggle';
    updateGroupToggle(toggle, group);

    var label = document.createElement('span');
    label.className = 'tool-group-label';
    label.textContent = group.label;

    var count = document.createElement('span');
    count.className = 'tool-group-count';
    var enabledCount = group.tools.filter(function(t) {{ return !_disabledSet.has(t); }}).length;
    count.textContent = enabledCount + '/' + group.tools.length;

    toggle.onclick = function(e) {{
      e.stopPropagation();
      toggleGroup(group, toggle, count);
    }};

    header.appendChild(arrow);
    header.appendChild(toggle);
    header.appendChild(label);
    header.appendChild(count);

    header.onclick = function() {{
      arrow.classList.toggle('open');
      childList.classList.toggle('open');
    }};

    var childList = document.createElement('ul');
    childList.className = 'tool-children';

    group.tools.forEach(function(toolName) {{
      var tli = document.createElement('li');
      tli.className = 'tool-item';
      tli.dataset.tool = toolName;

      var tToggle = document.createElement('span');
      tToggle.className = 'toggle' + (_disabledSet.has(toolName) ? '' : ' on');

      var tLabel = document.createElement('span');
      tLabel.textContent = toolName;

      tToggle.onclick = function() {{
        if (_disabledSet.has(toolName)) {{
          _disabledSet.delete(toolName);
          tToggle.className = 'toggle on';
        }} else {{
          _disabledSet.add(toolName);
          tToggle.className = 'toggle';
        }}
        updateGroupToggle(toggle, group);
        updateGroupCount(count, group);
      }};

      tli.appendChild(tToggle);
      tli.appendChild(tLabel);
      childList.appendChild(tli);
    }});

    li.appendChild(header);
    li.appendChild(childList);
    container.appendChild(li);
  }});
}}

function updateGroupToggle(el, group) {{
  var disabledCount = group.tools.filter(function(t) {{ return _disabledSet.has(t); }}).length;
  if (disabledCount === 0) el.className = 'toggle on';
  else if (disabledCount === group.tools.length) el.className = 'toggle';
  else el.className = 'toggle partial';
}}

function updateGroupCount(el, group) {{
  var enabledCount = group.tools.filter(function(t) {{ return !_disabledSet.has(t); }}).length;
  el.textContent = enabledCount + '/' + group.tools.length;
}}

function toggleGroup(group, toggleEl, countEl) {{
  var disabledCount = group.tools.filter(function(t) {{ return _disabledSet.has(t); }}).length;
  var enableAll = disabledCount > 0;
  group.tools.forEach(function(t) {{
    if (enableAll) _disabledSet.delete(t);
    else _disabledSet.add(t);
  }});
  updateGroupToggle(toggleEl, group);
  updateGroupCount(countEl, group);
  var groupEl = toggleEl.closest('.tool-group');
  groupEl.querySelectorAll('.tool-item .toggle').forEach(function(childToggle) {{
    var name = childToggle.parentElement.dataset.tool;
    childToggle.className = 'toggle' + (_disabledSet.has(name) ? '' : ' on');
  }});
}}

function filterTools(query) {{
  query = query.toLowerCase();
  document.querySelectorAll('.tool-group').forEach(function(group) {{
    var children = group.querySelector('.tool-children');
    var arrow = group.querySelector('.tool-group-arrow');
    var items = children.querySelectorAll('.tool-item');
    var anyVisible = false;
    items.forEach(function(item) {{
      var match = !query || item.dataset.tool.toLowerCase().includes(query);
      item.style.display = match ? '' : 'none';
      if (match) anyVisible = true;
    }});
    group.style.display = anyVisible ? '' : 'none';
    if (query && anyVisible) {{
      arrow.classList.add('open');
      children.classList.add('open');
    }}
  }});
}}

function doOk() {{
  document.title = 'GATE_OK';
}}

function doCancel() {{
  document.title = 'GATE_CANCEL';
}}

function getDisabledTools() {{
  return JSON.stringify(Array.from(_disabledSet).sort());
}}

renderToolTree();
</script>
</body>
</html>"""


# ── Gate state ───────────────────────────────────────────────────────────────
#
# The popup deliberately outlives the MCP request that opened it. An MCP
# client's request timeout is about five seconds, which no human beats while
# reading a tool list, so the request is cancelled long before the answer
# arrives. Rather than tearing the window down and re-prompting on every
# retry, the window stays up and publishes its answer through `on_result`
# the moment the user clicks — the retry then finds it already cached and
# is served without a second popup.
#
# State lives here rather than in the middleware because the window's
# lifetime, not the request's, is what has to be tracked.

_gate_window = None       # live NSWindow, or None
_gate_pending = False     # True from callAfter() until the window exists
_gate_on_result = None    # callback invoked with the answer, awaited or not
_gate_waiters: list[tuple] = []   # (loop, event, holder) still awaiting
_gate_lock = threading.Lock()


def gate_is_open() -> bool:
    """True while a popup is up, or about to be."""
    with _gate_lock:
        return _gate_pending or _gate_window is not None


def _finish(result: list[str] | None) -> None:
    """Single terminal path for the popup: publish the answer and clean up.

    Always runs on the AppKit main thread — from the KVO callback, the JS
    completion handler, or the window-close notification. Re-entrant calls
    are no-ops, which is what makes closing the window here safe even though
    that close fires the notification that calls back into this function.
    """
    global _gate_window, _gate_pending, _gate_on_result

    with _gate_lock:
        if not _gate_pending and _gate_window is None:
            return
        window, _gate_window = _gate_window, None
        _gate_pending = False
        on_result, _gate_on_result = _gate_on_result, None
        waiters, _gate_waiters[:] = list(_gate_waiters), []

    if window is not None:
        try:
            window.close()
        except Exception:
            logger.debug("gate window already closed", exc_info=True)

    # Publish before waking waiters, and even when there are none — caching
    # the answer for the client's retry is the entire point.
    if on_result is not None:
        try:
            on_result(result)
        except Exception:
            logger.exception("gate on_result callback failed")

    for loop, event, holder in waiters:
        holder[0] = result
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # that caller's loop is already gone


class _GateTitleObserver(NSObject):
    """KVO observer that watches the webview title for OK/Cancel signals.

    The popup's HTML signals the user's choice by setting document.title —
    the cheapest channel out of a WKWebView that needs no message-handler
    plumbing on the native side.
    """

    def initWithWindow_(self, window):
        self = objc.super(_GateTitleObserver, self).init()
        if self is not None:
            self._window = window
            self._handled = False
        return self

    def detachFrom_(self, obj) -> bool:
        """Stop observing. Returns False if someone else already did.

        Named for the Objective-C selector convention (trailing underscore =
        one argument). PyObjC bridges every method on an NSObject subclass,
        so a plain ``detach`` would be published as the zero-argument
        selector ``detach`` and rejected at class-creation time.
        """
        if self._handled:
            return False
        self._handled = True
        try:
            obj.removeObserver_forKeyPath_(self, "title")
        except Exception:
            logger.debug("KVO already removed", exc_info=True)
        return True

    def observeValueForKeyPath_ofObject_change_context_(
        self, keyPath, obj, change, context
    ):
        if self._handled:
            return
        try:
            title = obj.title()
        except Exception:
            return
        if not title:
            return

        if title == "GATE_OK":
            if not self.detachFrom_(obj):
                return

            def _on_js_result(result, error):
                if error or not result:
                    logger.warning("Gate JS eval failed: %s", error)
                    _finish([])
                    return
                try:
                    disabled = json.loads(result)
                except Exception as e:
                    logger.warning("Gate JSON parse failed: %s", e)
                    _finish([])
                    return
                logger.info("Gate: %d tools disabled by user", len(disabled))
                _finish(disabled)

            obj.evaluateJavaScript_completionHandler_("getDisabledTools()", _on_js_result)
        elif title == "GATE_CANCEL":
            if self.detachFrom_(obj):
                _finish(None)


async def show_tool_gate(
    tool_groups: list[dict],
    disabled_tools: set[str],
    meta: dict | None = None,
    on_result=None,
) -> list[str] | None:
    """Show the tool gate popup and await the user's answer.

    Returns the list of disabled tool names (OK), or None (Cancel).

    ``on_result`` is invoked with that same value from the main thread the
    moment the user answers — *including* when this coroutine has already
    been cancelled. Callers should treat it, not the return value, as the
    authoritative delivery path: MCP clients time out in about five seconds,
    which no human beats, so in practice the answer usually arrives after
    the awaiting request is gone.

    Raises ``CancelledError`` if the caller is cancelled, leaving the popup
    open on purpose. Re-raising matters: the MCP SDK has already sent an
    error response by then, and only an escaping cancellation stops it
    responding a second time and asserting. See the module docstring.
    """
    global _gate_pending, _gate_on_result

    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    holder: list[list[str] | None] = [None]

    def _show():
        global _gate_window
        html = _build_html(tool_groups, disabled_tools, meta)

        screen = NSScreen.mainScreen().frame()
        width, height = 480, 520
        x = (screen.size.width - width) / 2
        y = (screen.size.height - height) / 2
        frame = NSMakeRect(x, y, width, height)

        mask = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, mask, NSBackingStoreBuffered, False
        )
        window.setTitle_("MCP Tools Gate")
        window.setReleasedWhenClosed_(False)
        window.setLevel_(NSFloatingWindowLevel)

        config = WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, width, height), config
        )
        webview.setAutoresizingMask_(18)
        window.setContentView_(webview)

        observer = _GateTitleObserver.alloc().initWithWindow_(window)
        webview.addObserver_forKeyPath_options_context_(observer, "title", 1, None)

        webview.loadHTMLString_baseURL_(html, None)

        NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

        # Ensure webview gets first responder so first click passes through to JS
        from ui.menu import _FocusTrigger
        trigger = _FocusTrigger.alloc().init()
        trigger.setWindow_field_(window, webview)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, trigger, "focus:", None, False
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")

        # Window closed with the red X — treat as Cancel.
        def _on_close(notification):
            if observer.detachFrom_(webview):
                _finish(None)

        from Foundation import NSNotificationCenter
        NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            "NSWindowWillCloseNotification", window, None, _on_close
        )

        # Store refs to prevent GC
        window._gate_refs = (webview, observer)
        _gate_window = window

    with _gate_lock:
        already_open = _gate_pending or _gate_window is not None
        _gate_waiters.append((loop, event, holder))
        if not already_open:
            _gate_pending = True
            _gate_on_result = on_result

    if already_open:
        # A popup from an earlier — almost certainly cancelled — request is
        # still up. Attach to it instead of stacking a second window.
        logger.info("tool gate: popup already open, attaching to it")
    else:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_show)

    try:
        await event.wait()
    except asyncio.CancelledError:
        # The MCP client gave up waiting. Two things must happen here, and
        # both are load-bearing:
        #
        # 1. Re-raise. The SDK has ALREADY sent an error response via
        #    RequestResponder.cancel(), and mcp/server/lowlevel/server.py
        #    only suppresses its own duplicate respond() inside
        #    `except get_cancelled_exc_class()`. Swallowing the cancellation
        #    here — which this handler used to do — let the handler return a
        #    normal value, so the SDK fell through to a second respond() and
        #    tripped `assert not self._completed`. That AssertionError
        #    escaped into the anyio TaskGroup and destroyed the whole MCP
        #    session.
        #
        # 2. Leave the popup open. The user is still reading it; their answer
        #    reaches the middleware through on_result and is waiting in the
        #    cache by the time the client retries.
        with _gate_lock:
            _gate_waiters[:] = [w for w in _gate_waiters if w[1] is not event]
        logger.info("tool gate: caller cancelled; popup stays open for the retry")
        raise
    return holder[0]
