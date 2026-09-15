"""Session Explorer — the conversations window.

Replaces the per-conversation menu entries: the menu now has a single
"<N> conversations" item that opens this window. Left: a two-level sidebar
(main conversations with their Task sub-agents as first-class children,
anonymous clients grouped). Right: two tabs per conversation —

  • Stats — the existing chart (``chart_template.html``, untouched)
    rendered into an iframe via srcdoc, same payload as the old popup.
  • Explorer — the full transcript read from Claude Code's own JSONL
    files (``middleware/transcripts.py``), turn-grouped, virtualized.

JS ↔ Python is a real RPC bridge: the page posts JSON to a
``WKScriptMessageHandler`` and Python answers with
``window._voittaRPC.resolve(id, payload)`` via evaluateJavaScript. All
heavy work (transcript parsing, chart HTML) runs on the shared runtime
loop's thread pool, never on the AppKit main thread.

Host attributes consumed: ``self._tracker``, ``self._optimizer_pipeline``,
``self._cache_sim``, ``self._fmt_tokens``, ``self._promote_for_keyboard``,
``self._demote_after_keyboard``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import objc
import rumps
from AppKit import (
    NSApp, NSBackingStoreBuffered, NSNotificationCenter, NSWindow,
)
from Foundation import NSMakeRect, NSObject, NSSize, NSTimer, NSRunLoop
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore

from runtime import runtime
from ui.chart import generate_chart_html, _safe_json
from ui.conv_payload import build_chart_args
from ui.main_thread import on_main_thread
from ui._native import _FocusTrigger

logger = logging.getLogger("voitta-desktop.explorer")

_ACTIVE_WINDOW_S = 30.0   # "streaming now" dot in the sidebar


@on_main_thread
def _explorer_inject_js(app_ref, gen, js):
    """Push JS into the explorer webview from any thread, generation-guarded."""
    if getattr(app_ref, "_explorer_gen", 0) != gen:
        return
    refs = getattr(app_ref, "_explorer_refs", None)
    if not refs:
        return
    try:
        refs[1].evaluateJavaScript_completionHandler_(js, None)
    except Exception:
        logger.debug("explorer JS injection failed", exc_info=True)


class _ExplorerBridge(NSObject):
    """WKScriptMessageHandler + window-close observer for the explorer."""

    def initWithApp_gen_(self, app_ref, gen):
        self = objc.super(_ExplorerBridge, self).init()
        if self is not None:
            self._app = app_ref
            self._gen = gen
            self._closed = False
        return self

    # WKScriptMessageHandler
    def userContentController_didReceiveScriptMessage_(self, _ucc, message):
        try:
            req = json.loads(str(message.body()))
            rid = req["id"]
            method = str(req.get("method", ""))
            params = req.get("params") or {}
        except Exception:
            logger.debug("explorer bridge: bad message", exc_info=True)
            return
        app_ref, gen = self._app, self._gen

        def _work():
            try:
                payload = app_ref._explorer_rpc(method, params)
            except Exception as e:
                logger.exception("explorer rpc %s failed", method)
                payload = {"error": f"{type(e).__name__}: {e}"}
            js = (f"window._voittaRPC && window._voittaRPC.resolve("
                  f"{int(rid)}, {json.dumps(payload)})")
            _explorer_inject_js(app_ref, gen, js)

        runtime.run_blocking(_work)

    # Window close — break the WKUserContentController→handler retain cycle
    # and drop the refs so notify_update stops pushing.
    def windowWillClose_(self, _notification):
        if self._closed:
            return
        self._closed = True
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        app = self._app
        refs = getattr(app, "_explorer_refs", None)
        if refs is not None and getattr(app, "_explorer_gen", 0) == self._gen:
            try:
                ucc = refs[1].configuration().userContentController()
                ucc.removeScriptMessageHandlerForName_("voitta")
            except Exception:
                pass
            app._explorer_refs = None
        try:
            app._demote_after_keyboard()
        except Exception:
            pass


class SessionExplorerMixin:
    """Mixin: the Session Explorer window + its RPC backend."""

    # ── Window lifecycle ─────────────────────────────────────────────────────

    def show_session_explorer(self, _sender=None):
        """Menu callback. rumps swallows callback exceptions — surface them."""
        try:
            self._show_session_explorer()
        except Exception as e:
            logger.exception("show_session_explorer failed: %s", e)
            try:
                rumps.alert(
                    title="Session Explorer failed to open",
                    message=f"{type(e).__name__}: {e}\n\nDetails in "
                            f"~/.voitta-desktop/logs/desktop.log",
                    ok="OK",
                )
            except Exception:
                pass

    def _show_session_explorer(self):
        refs = getattr(self, "_explorer_refs", None)
        if refs:
            win = refs[0]
            try:
                if win.isVisible():
                    NSApp.activateIgnoringOtherApps_(True)
                    win.makeKeyAndOrderFront_(None)
                    return
            except Exception:
                pass
            self._explorer_refs = None

        if not hasattr(self, "_explorer_gen"):
            self._explorer_gen = 0
        self._explorer_gen += 1
        gen = self._explorer_gen

        mask = 1 | 2 | 4 | 8  # titled | closable | miniaturizable | resizable
        frame = NSMakeRect(120, 120, 1180, 760)
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, mask, NSBackingStoreBuffered, False
        )
        window.setTitle_("Voitta Desktop — Session Explorer")
        window.setReleasedWhenClosed_(False)
        window.setContentMinSize_(NSSize(880, 520))
        window.center()

        bridge = _ExplorerBridge.alloc().initWithApp_gen_(self, gen)

        config = WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        config.userContentController().addScriptMessageHandler_name_(bridge, "voitta")
        webview = WKWebView.alloc().initWithFrame_configuration_(
            window.contentView().bounds(), config
        )
        webview.setAutoresizingMask_(18)
        window.contentView().addSubview_(webview)

        ui_dir = Path(__file__).parent
        html = (ui_dir / "session_explorer.html").read_text(encoding="utf-8")
        js = (ui_dir / "session_explorer.js").read_text(encoding="utf-8")
        initial = self._explorer_list()
        html = html.replace(
            "/*INJECT*/",
            f"var _initialList = {_safe_json(initial)};\n" + js,
        )
        webview.loadHTMLString_baseURL_(html, None)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            bridge, "windowWillClose:", "NSWindowWillCloseNotification", window
        )

        self._explorer_refs = (window, webview, bridge)

        self._promote_for_keyboard()
        NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)

        trigger = _FocusTrigger.alloc().init()
        trigger.setWindow_field_(window, webview)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, trigger, "focus:", None, False
        )
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")

    def _explorer_ping(self):
        """Event push: a proxied response just completed. Called from the
        runtime thread via notify_update — the webview refreshes what it
        is showing (debounced JS-side)."""
        if getattr(self, "_explorer_refs", None):
            _explorer_inject_js(self, getattr(self, "_explorer_gen", 0),
                                "window._voittaPing && window._voittaPing()")

    # ── RPC backend (background thread) ──────────────────────────────────────

    def _explorer_rpc(self, method: str, params: dict) -> dict:
        if method == "list":
            return self._explorer_list()
        if method == "stats":
            return self._explorer_stats(str(params.get("conv_id", "")))
        if method == "transcript":
            return self._explorer_transcript(
                str(params.get("conv_id", "")), params.get("cursor"))
        if method == "image":
            return self._explorer_image(
                str(params.get("conv_id", "")), str(params.get("uuid", "")),
                int(params.get("seq", 0)))
        if method == "raw":
            return self._explorer_raw(
                str(params.get("conv_id", "")), str(params.get("uuid", "")))
        return {"error": f"unknown method: {method}"}

    # ── Sidebar list ─────────────────────────────────────────────────────────

    def _conv_row(self, conv, now: float) -> dict:
        total_in = conv.input_tokens + conv.cache_read_input_tokens
        cache_pct = (conv.cache_read_input_tokens * 100) // max(total_in, 1)
        return {
            "id": conv.id,
            "label": conv.label,
            "parent_id": conv.parent_id,
            "agent_id": conv.agent_id,
            "model": conv.model,
            "tokens": conv.total_tokens,
            "tokens_fmt": self._fmt_tokens(conv.total_tokens),
            "cache_pct": cache_pct if conv.cache_read_input_tokens else None,
            "requests": conv.request_count,
            "last_active": conv.last_active,
            "active": (now - conv.last_active) < _ACTIVE_WINDOW_S,
        }

    def _explorer_list(self) -> dict:
        now = time.time()
        convs = self._tracker.get_conversations_sorted()
        live_ids = {c.id for c in convs}
        transcripts = self._tracker.transcripts

        mains, anon = [], []
        children: dict[str, list[dict]] = {}
        for c in convs:
            if not c.turns:
                continue
            row = self._conv_row(c, now)
            if c.parent_id:
                children.setdefault(c.parent_id, []).append(row)
            elif c.id.startswith("anon-"):
                anon.append(row)
            else:
                mains.append(row)

        # Merge transcript-only agents (agents that never hit the proxy, or
        # tracker children not yet matched) into each main's child list.
        for m in mains:
            sid = m["id"]
            rows = children.get(sid, [])
            matched = {r["agent_id"] for r in rows if r["agent_id"]}
            for agent in transcripts.list_agents(sid):
                if agent["agent_id"] in matched:
                    continue
                rows.append({
                    "id": f"{sid}@{agent['agent_id']}",
                    "label": agent["label"],
                    "parent_id": sid,
                    "agent_id": agent["agent_id"],
                    "model": "", "tokens": 0, "tokens_fmt": "",
                    "cache_pct": None, "requests": 0,
                    "last_active": 0, "active": False,
                    "transcript_only": True,
                })
            m["children"] = rows
            m["has_transcript"] = transcripts.find_main(sid) is not None

        agent_count = sum(len(m.get("children", [])) for m in mains)
        agent_tokens = sum(
            ch["tokens"] for m in mains for ch in m.get("children", []))
        footer = {
            "mains": len(mains),
            "agents": agent_count,
            "tokens": sum(m["tokens"] for m in mains),
            "tokens_fmt": self._fmt_tokens(sum(m["tokens"] for m in mains)),
            "agent_tokens_fmt": self._fmt_tokens(agent_tokens),
        }
        return {"mains": mains, "anon": anon, "footer": footer, "now": now}

    # ── Stats tab ────────────────────────────────────────────────────────────

    def _explorer_stats(self, conv_id: str) -> dict:
        conv = self._tracker.get_conversation(conv_id)
        if conv is not None and conv.turns:
            breakdown, turns = build_chart_args(
                conv, self._optimizer_pipeline, self._cache_sim)
            active = self._optimizer_pipeline.active_optimizers
            html = generate_chart_html(None, breakdown, turns, active)
            return {"html": html, "requests": conv.request_count}
        return {"empty": "No proxy traffic recorded for this conversation."}

    # ── Explorer tab ─────────────────────────────────────────────────────────

    def _transcript_path(self, conv_id: str):
        """Resolve a sidebar conversation id to its transcript file."""
        transcripts = self._tracker.transcripts
        if "@" in conv_id:  # transcript-only agent row: <sid>@<agent_id>
            sid, agent_id = conv_id.split("@", 1)
            return transcripts.agent_path(sid, agent_id)
        conv = self._tracker.get_conversation(conv_id)
        if conv is not None and conv.parent_id:
            if conv.agent_id:
                return transcripts.agent_path(conv.parent_id, conv.agent_id)
            return None  # unattributed sub-agent: no transcript match yet
        return transcripts.find_main(conv_id)

    def _explorer_transcript(self, conv_id: str, cursor) -> dict:
        path = self._transcript_path(conv_id)
        if path is None:
            overhead = self._explorer_overhead(conv_id)
            if overhead:
                # No transcript, but the proxy saw the thread: show the
                # context overhead with an explanatory note instead of a
                # fully empty pane.
                return {"turns": [], "cursor": 0, "conv_id": conv_id,
                        "overhead": overhead,
                        "note": ("No transcript on disk for this thread — "
                                 "showing request context only.")}
            return {"empty": (
                "No transcript found. Transcripts come from Claude Code's "
                "session files (~/.claude/projects); other clients and "
                "unmatched sub-agents don't have one.")}
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        if cursor is not None and size == cursor:
            return {"unchanged": True, "cursor": size}
        result = self._tracker.transcripts.parse(path)
        result["conv_id"] = conv_id
        result["overhead"] = self._explorer_overhead(conv_id)
        return result

    _TEXT_CAP = 16_000

    def _explorer_overhead(self, conv_id: str) -> dict | None:
        """Full system-prompt blocks + tool definitions for a conversation.

        Sourced from the tracker's copy of the latest request body — only
        live (proxied) conversations have it; transcript-only rows and
        previous-run seeds return None.
        """
        conv = self._tracker.get_conversation(conv_id)
        if conv is None or (conv.system_raw is None and not conv.tools_raw):
            return None

        cap = self._TEXT_CAP
        system_blocks = []
        raw = conv.system_raw
        if isinstance(raw, str):
            raw = [{"type": "text", "text": raw}]
        for block in raw or []:
            if not isinstance(block, dict):
                continue
            text = block.get("text", "")
            system_blocks.append({
                "text": text[:cap],
                "chars": len(text),
                "cache": bool(block.get("cache_control")),
            })

        tools = []
        tools_chars = 0
        for tool in conv.tools_raw or []:
            if not isinstance(tool, dict):
                continue
            desc = tool.get("description", "")
            schema_chars = len(json.dumps(tool.get("input_schema", {})))
            tools_chars += len(json.dumps(tool))
            tools.append({
                "name": tool.get("name", "?"),
                "desc": desc[:cap],
                "desc_chars": len(desc),
                "schema_chars": schema_chars,
                "cache": bool(tool.get("cache_control")),
            })

        if not system_blocks and not tools:
            return None
        return {
            "system": system_blocks,
            "system_chars": sum(b["chars"] for b in system_blocks),
            "tools": tools,
            "tools_chars": tools_chars,
        }

    def _explorer_image(self, conv_id: str, uuid: str, seq: int) -> dict:
        path = self._transcript_path(conv_id)
        if path is None:
            return {"error": "no transcript"}
        hit = self._tracker.transcripts.get_image(path, uuid, seq)
        if hit is None:
            return {"error": "image not found"}
        media_type, data = hit
        return {"media_type": media_type, "data": data}

    def _explorer_raw(self, conv_id: str, uuid: str) -> dict:
        path = self._transcript_path(conv_id)
        if path is None:
            return {"error": "no transcript"}
        return {"json": self._tracker.transcripts.raw_entry(path, uuid)}
