"""Stored conversations — the window behind the menu bar's "Stored conversations…".

Lists what /voitta-store kept (``~/.voitta-desktop/conversations``), shows a
conversation's whole history in the transcript viewer the Session Explorer
uses (compacted turns included), its models and routing, and exports or
deletes the copies. The data work is in ``llmgw/conversations.py``; this file
is the window and its RPC bridge, the same shape as the Session Explorer's.
Export's Save panel, Reveal in Finder and Copy need the main thread; the
rest runs on the runtime's thread pool.

Host attributes consumed: ``self._llm`` (its ``conversations_dir``),
``self._stored_item`` (the menu entry), ``self._promote_for_keyboard``,
``self._demote_after_keyboard``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import objc
from AppKit import (
    NSApp, NSBackingStoreBuffered, NSNotificationCenter, NSPasteboard,
    NSPasteboardTypeString, NSSavePanel, NSWindow, NSWorkspace,
)
from Foundation import NSURL, NSMakeRect, NSObject, NSRunLoop, NSSize, NSTimer
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore

from llmgw import config as llm_config
from llmgw import conversations
from middleware.transcripts import TranscriptStore
from runtime import runtime
from ui._native import _FocusTrigger
from ui.chart import _safe_json
from ui.main_thread import on_main_thread
from ui.session_explorer import page_html

logger = logging.getLogger("voitta-desktop.stored")

_transcripts = TranscriptStore()
_MAIN_THREAD_METHODS = ("export", "reveal", "copy")


@on_main_thread
def _stored_inject_js(app_ref, gen, js):
    """Push JS into the window's webview from any thread, generation-guarded."""
    if getattr(app_ref, "_stored_gen", 0) != gen:
        return
    refs = getattr(app_ref, "_stored_refs", None)
    if not refs:
        return
    try:
        refs[1].evaluateJavaScript_completionHandler_(js, None)
    except Exception:
        logger.debug("stored-conversations JS injection failed", exc_info=True)


class _StoredBridge(NSObject):
    """WKScriptMessageHandler + window-close observer for the window."""

    def initWithApp_gen_(self, app_ref, gen):
        self = objc.super(_StoredBridge, self).init()
        if self is not None:
            self._app = app_ref
            self._gen = gen
            self._closed = False
        return self

    @objc.python_method
    def _resolver(self, rid):
        app_ref, gen = self._app, self._gen

        def resolve(payload):
            _stored_inject_js(app_ref, gen, f"window._voittaRPC && window._voittaRPC.resolve("
                                            f"{int(rid)}, {json.dumps(payload)})")
        return resolve

    # WKScriptMessageHandler (main thread)
    def userContentController_didReceiveScriptMessage_(self, _ucc, message):
        try:
            req = json.loads(str(message.body()))
            rid = int(req["id"])
            method = str(req.get("method", ""))
            params = req.get("params") or {}
        except Exception:
            logger.debug("stored bridge: bad message", exc_info=True)
            return
        resolve = self._resolver(rid)
        app_ref = self._app
        if method in _MAIN_THREAD_METHODS:
            try:
                app_ref._stored_main(method, params, resolve)
            except Exception as e:
                logger.exception("stored %s failed", method)
                resolve({"error": f"{type(e).__name__}: {e}"})
            return

        def _work():
            try:
                payload = app_ref._stored_rpc(method, params)
            except Exception as e:
                logger.exception("stored rpc %s failed", method)
                payload = {"error": f"{type(e).__name__}: {e}"}
            resolve(payload)

        runtime.run_blocking(_work)

    # Window close — break the WKUserContentController→handler retain cycle.
    def windowWillClose_(self, _notification):
        if self._closed:
            return
        self._closed = True
        NSNotificationCenter.defaultCenter().removeObserver_(self)
        app = self._app
        refs = getattr(app, "_stored_refs", None)
        if refs is not None and getattr(app, "_stored_gen", 0) == self._gen:
            try:
                refs[1].configuration().userContentController().removeScriptMessageHandlerForName_("voitta")
            except Exception:
                pass
            app._stored_refs = None
        try:
            app._demote_after_keyboard()
        except Exception:
            pass


def stored_page_html(initial: dict) -> str:
    return page_html("stored_conversations", f"var _initialList = {_safe_json(initial)};")


class StoredConversationsMixin:
    """Mixin: the Stored conversations window, its RPC backend and the menu count."""

    def _stored_dir(self) -> Path:
        llm = getattr(self, "_llm", None)
        return llm.conversations_dir if llm is not None else llm_config.DATA_DIR.parent / "conversations"

    # ── Menu entry ───────────────────────────────────────────────────────────

    def _stored_menu_title(self) -> str:
        n = conversations.count(self._stored_dir())
        return f"Stored conversations… ({n})" if n else "Stored conversations…"

    def _stored_watch(self):
        """Keep the menu count (and an open window) current; called once at menu build.

        Runs before the runtime has started, so nothing here may use it. The
        first /voitta-store build's single-file copies are counted as they
        are and moved to folders when the window first lists them."""
        conversations.on_change(self._stored_changed)

    @on_main_thread
    def _stored_changed(self):
        item = getattr(self, "_stored_item", None)
        if item is not None:
            item.title = self._stored_menu_title()
        if getattr(self, "_stored_refs", None):
            _stored_inject_js(self, getattr(self, "_stored_gen", 0), "window._voittaPing && window._voittaPing()")

    # ── Window lifecycle ─────────────────────────────────────────────────────

    def show_stored_conversations(self, _sender=None):
        self._show_stored_conversations()

    @on_main_thread
    def _show_stored_conversations(self):
        refs = getattr(self, "_stored_refs", None)
        if refs:
            try:
                if refs[0].isVisible():
                    NSApp.activateIgnoringOtherApps_(True)
                    refs[0].makeKeyAndOrderFront_(None)
                    return
            except Exception:
                pass
            self._stored_refs = None

        self._stored_gen = getattr(self, "_stored_gen", 0) + 1
        gen = self._stored_gen

        mask = 1 | 2 | 4 | 8  # titled | closable | miniaturizable | resizable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(140, 140, 1180, 780), mask, NSBackingStoreBuffered, False)
        window.setTitle_("Voitta Desktop — Stored conversations")
        window.setReleasedWhenClosed_(False)
        window.setContentMinSize_(NSSize(900, 540))
        window.center()

        bridge = _StoredBridge.alloc().initWithApp_gen_(self, gen)
        config = WKWebViewConfiguration.alloc().init()
        config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        config.userContentController().addScriptMessageHandler_name_(bridge, "voitta")
        webview = WKWebView.alloc().initWithFrame_configuration_(window.contentView().bounds(), config)
        webview.setAutoresizingMask_(18)
        window.contentView().addSubview_(webview)
        webview.loadHTMLString_baseURL_(stored_page_html(self._stored_list()), None)

        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            bridge, "windowWillClose:", "NSWindowWillCloseNotification", window)
        self._stored_refs = (window, webview, bridge)

        self._promote_for_keyboard()
        NSApp.activateIgnoringOtherApps_(True)
        window.makeKeyAndOrderFront_(None)
        trigger = _FocusTrigger.alloc().init()
        trigger.setWindow_field_(window, webview)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.1, trigger, "focus:", None, False)
        NSRunLoop.mainRunLoop().addTimer_forMode_(timer, "NSDefaultRunLoopMode")

    # ── RPC backend (thread pool) ────────────────────────────────────────────

    def _stored_list(self) -> dict:
        return {"conversations": conversations.list_conversations(self._stored_dir())}

    def _stored_path(self, conv_id: str) -> Path:
        sid, _, agent = conv_id.partition("@")
        return conversations.transcript_path(self._stored_dir(), sid, agent or None)

    def _stored_rpc(self, method: str, params: dict) -> dict:
        folder = self._stored_dir()
        if method == "list":
            return self._stored_list()
        if method == "transcript":
            conv_id = str(params.get("conv_id", ""))
            try:
                path = self._stored_path(conv_id)
            except (ValueError, FileNotFoundError) as e:
                return {"empty": str(e)}
            result = _transcripts.parse(path, full_history=True)
            conversations.annotate_turns(result["turns"], conversations.routing(folder, conv_id.split("@")[0]))
            result["conv_id"] = conv_id
            return result
        if method == "image":
            hit = _transcripts.get_image(self._stored_path(str(params.get("conv_id", ""))),
                                         str(params.get("uuid", "")), int(params.get("seq", 0)))
            if hit is None:
                return {"error": "image not found"}
            return {"media_type": hit[0], "data": hit[1]}
        if method == "raw":
            return {"json": _transcripts.raw_entry(self._stored_path(str(params.get("conv_id", ""))),
                                                   str(params.get("uuid", "")))}
        if method == "routing":
            return {"records": conversations.routing(folder, str(params.get("session", "")))}
        if method == "delete":
            gone = conversations.delete(folder, [str(s) for s in params.get("sessions") or []])
            logger.info("deleted %d stored conversation(s)", gone)
            return {"deleted": gone, "list": self._stored_list()}
        return {"error": f"unknown method: {method}"}

    # ── Main-thread actions ──────────────────────────────────────────────────

    def _stored_main(self, method: str, params: dict, resolve):
        folder = self._stored_dir()
        if method == "copy":
            board = NSPasteboard.generalPasteboard()
            board.clearContents()
            board.setString_forType_(str(params.get("text", "")), NSPasteboardTypeString)
            resolve({})
        elif method == "reveal":
            path = conversations.conversation_dir(folder, str(params.get("session", "")))
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(str(path))])
            resolve({})
        elif method == "export":
            self._stored_export(folder, [str(s) for s in params.get("sessions") or []],
                                str(params.get("format", "")), resolve)

    def _stored_export(self, folder: Path, ids: list[str], fmt: str, resolve):
        if fmt not in conversations.EXPORTS or not ids:
            resolve({"error": "nothing to export"})
            return
        if len(ids) == 1:
            name = conversations.export_name(conversations.load_meta(folder, ids[0]), fmt)
        else:
            name = f"voitta-conversations-{time.strftime('%Y%m%d-%H%M')}.zip"
        panel = NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(name)
        panel.setCanCreateDirectories_(True)
        panel.setMessage_("Export " + (f"{len(ids)} stored conversations as a .zip"
                                       if len(ids) > 1 else "the stored conversation"))

        def done(result):
            if result != 1:  # NSModalResponseOK
                resolve({"cancelled": True})
                return
            dest = Path(panel.URL().path())

            def work():
                try:
                    conversations.export(folder, ids, fmt, dest)
                    logger.info("exported %d conversation(s) as %s to %s", len(ids), fmt, dest)
                    resolve({"path": str(dest), "name": dest.name})
                except Exception as e:
                    logger.exception("export failed")
                    resolve({"error": f"{type(e).__name__}: {e}"})

            runtime.run_blocking(work)

        refs = getattr(self, "_stored_refs", None)
        panel.beginSheetModalForWindow_completionHandler_(refs[0] if refs else None, done)
