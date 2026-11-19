"""A client's cancel must stop the call it names, here and on the backend.

2026-11-18 21:32: Claude Code cancelled a voitta-kvm key_sequence 0.6 s in;
Desktop logged "Failed to validate notification: RequestResponder must be used
as a context manager" and the backend typed a whole email anyway. See
mcpproxy/cancel.py for the two gaps these tests pin.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time

import anyio
import mcp.types as t
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from mcp.shared.message import SessionMessage

from mcpproxy.cancel import install
from mcpproxy.resilient import ResilientFastMCPProxy
from mcpproxy.server import _make_static_headers_factory

install()

def _msg(d: dict) -> SessionMessage:
    return SessionMessage(t.JSONRPCMessage.model_validate(d))

def _drain(stream) -> list:
    out = []
    while True:
        try:
            out.append(stream.receive_nowait().message.root)
        except anyio.WouldBlock:
            return out

def _server(ran: list) -> FastMCP:
    srv = FastMCP("s")

    @srv.tool()
    async def slow() -> str:
        ran.append("start")
        await asyncio.sleep(1.0)
        ran.append("done")
        return "ok"

    @srv.tool()
    def fast() -> str:
        return "fast"

    return srv

async def _run_in_memory(srv: FastMCP, script) -> list:
    """Run srv's session over memory streams, feed it script(send), return what it wrote."""
    c2s_w, c2s_r = anyio.create_memory_object_stream(100)
    s2c_w, s2c_r = anyio.create_memory_object_stream(100)
    low = srv._mcp_server
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: low.run(c2s_r, s2c_w, low.create_initialization_options()))
        await c2s_w.send(_msg({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}))
        await s2c_r.receive()
        await c2s_w.send(_msg({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        await anyio.sleep(0.1)
        await script(c2s_w)
        out = _drain(s2c_r)
        tg.cancel_scope.cancel()
    return out

def _call(i: int, name: str) -> SessionMessage:
    return _msg({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": {}}})

def _cancel(i: int) -> SessionMessage:
    return _msg({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": i, "reason": "user"}})

async def test_cancel_read_together_with_its_call_stops_it_before_it_runs(caplog):
    """The 21:32 case: call and cancel read back to back, before the handler entered."""
    ran: list = []

    async def script(send):
        send.send_nowait(_call(7, "slow"))
        send.send_nowait(_cancel(7))
        await anyio.sleep(1.5)

    with caplog.at_level(logging.WARNING):
        out = await _run_in_memory(_server(ran), script)
    assert "done" not in ran
    assert ran == [], "the tool started: the cancel came too late to keep it from running"
    errors = [m for m in out if isinstance(m, t.JSONRPCError) and m.id == 7]
    assert len(errors) == 1 and errors[0].error.message == "Request cancelled", out
    assert not [m for m in out if isinstance(m, t.JSONRPCResponse) and m.id == 7]
    assert "Failed to validate notification" not in caplog.text

async def test_cancel_of_a_running_call_still_stops_it():
    ran: list = []

    async def script(send):
        await send.send(_call(7, "slow"))
        await anyio.sleep(0.3)
        await send.send(_cancel(7))
        await anyio.sleep(1.2)

    out = await _run_in_memory(_server(ran), script)
    assert ran == ["start"]
    assert [m for m in out if isinstance(m, t.JSONRPCError) and m.id == 7]

async def test_cancel_for_an_unknown_or_finished_request_is_ignored(caplog):
    ran: list = []

    async def script(send):
        await send.send(_cancel(999))
        await send.send(_call(8, "fast"))
        await anyio.sleep(0.3)
        await send.send(_cancel(8))           # finished already
        await send.send(_call(9, "fast"))
        await anyio.sleep(0.3)

    with caplog.at_level(logging.WARNING):
        out = await _run_in_memory(_server(ran), script)
    ok = {m.id for m in out if isinstance(m, t.JSONRPCResponse)}
    assert {8, 9} <= ok, out
    assert "Failed to validate notification" not in caplog.text

# ── through the proxy, to a backend over HTTP ────────────────────────────────

def _watch_disconnects(app, seen: list):
    """ASGI wrapper: note when the connection of a tools/call POST is closed by the client."""
    async def wrapped(scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await app(scope, receive, send)
        body = []

        async def recv():
            m = await receive()
            if m["type"] == "http.request":
                body.append(m.get("body", b""))
            elif m["type"] == "http.disconnect" and b"tools/call" in b"".join(body):
                seen.append(time.monotonic())
            return m

        return await app(scope, recv, send)
    return wrapped

@contextlib.asynccontextmanager
async def _serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(250):
            if server.started:
                break
            await asyncio.sleep(0.02)
        yield port
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)
        sock.close()

async def _wait_for(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()

@pytest.mark.parametrize("stateless", [False, True], ids=["stateful-backend", "stateless-backend"])
async def test_cancel_through_the_proxy_reaches_the_backend(stateless):
    ev: list = []
    backend = FastMCP("backend")

    @backend.tool()
    async def slow() -> str:
        ev.append(("start", time.monotonic()))
        try:
            await asyncio.sleep(5)
            ev.append(("done", time.monotonic()))
            return "done"
        except asyncio.CancelledError:
            ev.append(("cancelled", time.monotonic()))
            raise

    closed: list = []
    app = _watch_disconnects(backend.http_app(path="/mcp", stateless_http=stateless), closed)
    async with _serve(app) as port:
        proxy = ResilientFastMCPProxy(
            client_factory=_make_static_headers_factory(f"http://127.0.0.1:{port}/mcp", {}),
            name="kvm", backend_name="Fake KVM", cache_listings=False, app_ref=None, prefix="kvm")
        main = FastMCP("voitta-desktop")
        main.mount(proxy, namespace="kvm")
        async with Client(main) as client:
            call = asyncio.create_task(client.call_tool("kvm_slow", {}))
            assert await _wait_for(lambda: any(e[0] == "start" for e in ev), 10), "the backend never started"
            t0 = time.monotonic()
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await call
            if stateless:
                # A stateless server has no session to take a notice: its request closing is the signal.
                assert await _wait_for(lambda: closed, 3), "the backend's tools/call request was never closed"
                assert closed[0] - t0 < 1.0
            else:
                assert await _wait_for(lambda: any(e[0] == "cancelled" for e in ev), 3), ev
                assert next(e[1] for e in ev if e[0] == "cancelled") - t0 < 1.0
            assert not any(e[0] == "done" for e in ev)
            # The proxy keeps working after a cancel, and nothing was replayed.
            assert sum(e[0] == "start" for e in ev) == 1
