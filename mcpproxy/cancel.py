"""A client's cancel reaches the backend that is running the call.

What went wrong (2026-11-18 21:32, a voitta-kvm key_sequence): Claude Code
sent ``notifications/cancelled`` 0.6 s into a tools/call. The call ran to the
end on the backend anyway and typed a whole email. Two gaps, both in the MCP
SDK (mcp 1.26), both closed here:

1. The cancel was lost on the way in. The SDK's receive loop puts a request in
   ``_in_flight`` and hands it to a handler task that enters the responder
   when it gets to run. When Desktop's event loop is busy (a 1 MB LLM request
   being optimized), the request and its cancel are read back to back, and the
   cancel reaches ``RequestResponder.cancel()`` before the handler task has
   entered: the SDK raises "RequestResponder must be used as a context
   manager", logs "Failed to validate notification" and drops the cancel. The
   handler then starts and forwards the call. Here such a cancel is kept, and
   the handler's cancel scope is cancelled as it is entered, so the call never
   leaves Desktop.

2. A cancel that did stop Desktop's handler stopped nothing upstream. The SDK
   client neither tells the server (``notifications/cancelled``) nor ends the
   HTTP request carrying the call; that request runs in the transport's own
   task group. Here a cancelled client request sends the server a
   ``notifications/cancelled`` (what a stateful server acts on), and the
   streamable-HTTP transport closes that request's POST when it sees the
   notice go out (what a stateless server such as voitta-kvm acts on: its
   request ends).

Nothing is replayed or retried. ``initialize`` is never cancelled (the spec
forbids it). A cancel for an id that is unknown or already finished is ignored
as before.

``install()`` patches the SDK classes once per process; it is idempotent.
"""

from __future__ import annotations

import logging

import anyio
import mcp.types as t
from mcp.client.session import ClientSession
from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.shared.session import BaseSession, RequestResponder

logger = logging.getLogger("voitta-desktop.mcp")

CANCEL_NOTICE_TIMEOUT_S = 2.0   # sending the backend its cancel notice never holds a cancelled call longer
_installed = False

def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    _patch_responder()
    _patch_client_session()
    _patch_streamable_http()

# ── 1. inbound: a cancel that arrives before the handler entered ─────────────

def _patch_responder() -> None:
    orig_enter = RequestResponder.__enter__
    orig_cancel = RequestResponder.cancel

    def __enter__(self):
        r = orig_enter(self)
        if getattr(self, "_voitta_cancel_pending", False):
            self._cancel_scope.cancel()       # the handler stops at its first await, before forwarding anything
        return r

    async def cancel(self):
        if getattr(self, "_voitta_cancel_pending", False):
            return                             # a repeated cancel
        if self._entered or self._completed:
            return await orig_cancel(self)
        # What orig_cancel does, minus the scope it cannot reach yet: mark it
        # done (its exit then drops it from _in_flight) and answer as the SDK does.
        self._voitta_cancel_pending = True
        self._completed = True
        logger.info("cancel for request %s arrived before its handler started: it will not run", self.request_id)
        await self._session._send_response(  # type: ignore[reportPrivateUsage]
            request_id=self.request_id,
            response=t.ErrorData(code=0, message="Request cancelled", data=None),
        )

    RequestResponder.__enter__ = __enter__
    RequestResponder.cancel = cancel

# ── 2a. outbound: a cancelled client request tells its server ────────────────

def _patch_client_session() -> None:
    orig_send_request = BaseSession.send_request

    async def send_request(self, request, *args, **kwargs):
        if not isinstance(self, ClientSession) or isinstance(getattr(request, "root", None), t.InitializeRequest):
            return await orig_send_request(self, request, *args, **kwargs)
        request_id = self._request_id          # the id orig_send_request takes: it reads it before any await
        try:
            return await orig_send_request(self, request, *args, **kwargs)
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True), anyio.move_on_after(CANCEL_NOTICE_TIMEOUT_S):
                try:
                    await self.send_notification(t.ClientNotification(t.CancelledNotification(
                        params=t.CancelledNotificationParams(requestId=request_id,
                                                             reason="cancelled by the client"))))
                    logger.info("cancelled upstream request %s (%s)", request_id,
                                getattr(getattr(request, "root", None), "method", "?"))
                except Exception as e:            # a dead transport: nothing left to stop
                    logger.debug("could not send the cancel for upstream request %s: %s", request_id,
                                 type(e).__name__)
            raise

    BaseSession.send_request = send_request

# ── 2b. outbound: the cancel notice also ends that request's HTTP POST ───────

def _patch_streamable_http() -> None:
    orig_post = StreamableHTTPTransport._handle_post_request

    async def _handle_post_request(self, ctx):
        root = ctx.session_message.message.root
        posts = self.__dict__.setdefault("_voitta_posts", {})
        if isinstance(root, t.JSONRPCRequest):
            with anyio.CancelScope() as scope:
                posts[root.id] = scope
                try:
                    await orig_post(self, ctx)
                finally:
                    if posts.get(root.id) is scope:
                        posts.pop(root.id, None)
            if scope.cancelled_caught:
                logger.info("closed the HTTP request of cancelled upstream request %s", root.id)
            return
        if isinstance(root, t.JSONRPCNotification) and root.method == "notifications/cancelled":
            scope = posts.get((root.params or {}).get("requestId"))
            if scope is not None:
                scope.cancel()                 # ends the POST: a stateless server sees its request close
            try:
                await orig_post(self, ctx)     # and the notice itself, for a server that keeps sessions
            except Exception as e:             # never let a refused notice end the transport's writer
                logger.debug("upstream did not take the cancel notice: %s", type(e).__name__)
            return
        await orig_post(self, ctx)

    StreamableHTTPTransport._handle_post_request = _handle_post_request
