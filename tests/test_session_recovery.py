"""Expired upstream HTTP sessions recover without replaying actions or disturbing
another downstream session. Use real HTTP: the SDK translates any POST 404 into
McpError(32600, 'Session terminated'), not a failed background connection task.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from contextlib import asynccontextmanager

import httpx
import mcp.types
import pytest
import uvicorn
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from mcpproxy.resilient import ResilientFastMCPProxy, VoittaStatefulProxyClient
from test_listing_connections import _until, tool_cache  # noqa: F401 - fixture


class ExpiringServer:
    """Expire sessions without restarting the server. Optionally lose a response
    AFTER executing a tool, so an unsafe replay would increment actions twice."""
    def __init__(self):
        self.mcp = FastMCP("expiring")
        self.expired = set()
        self.sessions = {}
        self.actions = 0
        self.rejected = 0
        self.lose_response_for = None
        self.reject_status = None
        self.reject_batch = 1
        self.reject_ready = asyncio.Event()
        self.held = asyncio.Event()
        self.finish_hold = asyncio.Event()

        @self.mcp.tool()
        async def session(ctx: Context) -> str:
            self.sessions[ctx.session_id] = ctx.session
            return ctx.session_id

        @self.mcp.tool()
        async def mutate() -> str:
            self.actions += 1
            return "done"

        @self.mcp.tool()
        async def hold() -> str:
            self.held.set()
            await self.finish_hold.wait()
            return "finished once"

        @self.mcp.tool()
        async def ordinary_error() -> str:
            raise ToolError("Session terminated")

        self.app = self.mcp.http_app()

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        sid = headers.get(b"mcp-session-id", b"").decode()
        post = scope.get("type") == "http" and scope.get("method") == "POST"
        if post and (self.reject_status or sid in self.expired):
            self.rejected += 1
            if self.rejected >= self.reject_batch:
                self.reject_ready.set()
            await self.reject_ready.wait()
            await send({"type": "http.response.start", "status": self.reject_status or 404,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Session not found"})
            return
        if post and sid and sid == self.lose_response_for:
            # Background listings/notifications must not consume this injection.
            incoming, body = [], bytearray()
            while True:
                message = await receive()
                incoming.append(message)
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            request = json.loads(body)
            original_receive = receive
            async def replay_request():
                return incoming.pop(0) if incoming else await original_receive()
            receive = replay_request
            if request.get("method") == "tools/call" and request.get("params", {}).get("name") == "mutate":
                self.lose_response_for = None
                async def discard_response(message):
                    pass
                await self.app(scope, receive, discard_response)
                self.expired.add(sid)
                # The server has executed the operation. Discard its real response.
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Session not found"})
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await _until(lambda: server.started or task.done())
        assert server.started
        yield f"http://127.0.0.1:{port}/mcp", server
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def proxy_at(url, name="a"):
    proxy = ResilientFastMCPProxy(
        client_factory=lambda: VoittaStatefulProxyClient(StreamableHttpTransport(url)),
        name=name, backend_name=name, cache_listings=True)
    assert (await proxy.force_refresh())[0]
    return proxy


def text(result):
    return result.content[0].text


@pytest.mark.asyncio
async def test_expiry_recovers_next_call_without_replaying_or_affecting_others(tool_cache, caplog):
    caplog.set_level(logging.INFO, logger="voitta-desktop.mcp")
    upstream, other = ExpiringServer(), ExpiringServer()
    async with serve(upstream) as (url, server), serve(other) as (url2, _):
        main = FastMCP("desktop")
        main.mount(await proxy_at(url), prefix="a")
        main.mount(await proxy_at(url2, "b"), prefix="b")
        async with Client(main) as window, Client(main) as another:
            first = text(await window.call_tool("a_session"))
            second = text(await another.call_tool("a_session"))
            backend_b = text(await window.call_tool("b_session"))
            upstream.expired.add(first)
            with pytest.raises(ToolError, match="not replayed"):
                await window.call_tool("a_mutate")
            assert upstream.actions == 0  # rejected before execution, still no replay
            assert text(await another.call_tool("a_session")) == second
            assert text(await window.call_tool("b_session")) == backend_b
            replacement = text(await window.call_tool("a_session"))
            assert replacement not in (first, second)
            assert text(await window.call_tool("a_session")) == replacement

            # A 404 whose operation DID run must not be replayed either.
            upstream.lose_response_for = replacement
            # Another window and a same-session read must leave the injection armed.
            assert text(await another.call_tool("a_session")) == second
            assert text(await window.call_tool("a_session")) == replacement
            assert upstream.lose_response_for == replacement
            with pytest.raises(ToolError, match="not replayed"):
                await window.call_tool("a_mutate")
            assert upstream.actions == 1
            assert text(await window.call_tool("a_session")) != replacement
            assert upstream.actions == 1
        await _until(lambda: not server.server_state.connections)
    records = [r.getMessage() for r in caplog.records if r.name == "voitta-desktop.mcp"]
    assert sum("session invalidated" in r for r in records) == 2
    assert sum("upstream recovered" in r for r in records) == 2
    assert not any(sid in r for sid in (first, second, replacement) for r in records)


@pytest.mark.asyncio
async def test_concurrent_expiry_waits_for_in_flight_call_and_reconnects_once(tool_cache):
    upstream = ExpiringServer()
    async with serve(upstream) as (url, _):
        main = FastMCP("desktop")
        main.mount(await proxy_at(url), prefix="a")
        async with Client(main) as window:
            first = text(await window.call_tool("a_session"))
            held = asyncio.create_task(window.call_tool("a_hold"))
            await asyncio.wait_for(upstream.held.wait(), 5)
            upstream.expired.add(first)
            upstream.reject_batch = 3
            try:
                errors = await asyncio.wait_for(asyncio.gather(
                    *(window.call_tool("a_mutate") for _ in range(3)), return_exceptions=True), 5)
                assert all(isinstance(e, ToolError) and "not replayed" in str(e) for e in errors)
                next_calls = [asyncio.create_task(window.call_tool("a_session")) for _ in range(4)]
                await asyncio.sleep(0.1)
                assert all(not t.done() for t in next_calls)  # no teardown under the held operation
            finally:
                upstream.finish_hold.set()
            assert text(await asyncio.wait_for(held, 5)) == "finished once"
            results = await asyncio.wait_for(asyncio.gather(*next_calls), 5)
            ids = {text(r) for r in results}
            assert len(ids) == 1 and first not in ids
            assert len(upstream.sessions) == 2
            assert upstream.actions == 0


@pytest.mark.asyncio
async def test_tool_error_and_timeout_do_not_trigger_expiry_or_replay(tool_cache):
    upstream = ExpiringServer()
    async with serve(upstream) as (url, _):
        client = VoittaStatefulProxyClient(StreamableHttpTransport(url))
        try:
            async with client:
                first = text(await client.call_tool("session"))
                result = await client.call_tool_mcp("ordinary_error", {})
                assert result.isError
            assert not client._expired
            with pytest.raises(TimeoutError):
                async with client:
                    raise TimeoutError("ambiguous action outcome")
            with pytest.raises(McpError):
                async with client:
                    raise McpError(mcp.types.ErrorData(code=-32600, message="Session terminated"))
            assert not client._expired
            async with client:
                assert text(await client.call_tool("session")) == first
        finally:
            await client._disconnect(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_initialization_failures_are_not_treated_as_expired_sessions(status, caplog):
    upstream = ExpiringServer()
    upstream.reject_status = status
    async with serve(upstream) as (url, _):
        client = VoittaStatefulProxyClient(StreamableHttpTransport(url))
        try:
            with pytest.raises((McpError, httpx.HTTPStatusError, RuntimeError)):
                async with client:
                    pytest.fail("initialization should fail")
            assert upstream.rejected == 1
            assert not client._expired
            assert not any("session invalidated" in r.getMessage() for r in caplog.records)
        finally:
            await client._disconnect(force=True)


@pytest.mark.asyncio
async def test_server_pings_are_answered_between_tool_calls_and_after_recovery(tool_cache):
    upstream = ExpiringServer()
    async with serve(upstream) as (url, _):
        main = FastMCP("desktop")
        main.mount(await proxy_at(url), prefix="a")
        async with Client(main) as window:
            first = text(await window.call_tool("a_session"))
            # No downstream requests while these server->client heartbeats run.
            for _ in range(3):
                await asyncio.wait_for(upstream.sessions[first].send_ping(), 2)
            upstream.expired.add(first)
            with pytest.raises(ToolError, match="not replayed"):
                await window.call_tool("a_mutate")
            second = text(await window.call_tool("a_session"))
            await asyncio.wait_for(upstream.sessions[second].send_ping(), 2)
            assert upstream.actions == 0

@pytest.mark.asyncio
async def test_cancelled_in_flight_call_does_not_block_recovery():
    upstream = ExpiringServer()
    async with serve(upstream) as (url, _):
        client = VoittaStatefulProxyClient(StreamableHttpTransport(url))
        entered = asyncio.Event()

        async def in_flight():
            async with client:
                entered.set()
                await asyncio.Event().wait()

        pending = None
        try:
            async with client:
                first = text(await client.call_tool("session"))
            pending = asyncio.create_task(in_flight())
            await asyncio.wait_for(entered.wait(), 5)
            upstream.expired.add(first)
            with pytest.raises(McpError, match="not replayed"):
                async with client:
                    await client.call_tool("session")
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert client._active_calls == 0
            async with asyncio.timeout(5):
                async with client:
                    assert text(await client.call_tool("session")) != first
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await client._disconnect(force=True)


@pytest.mark.asyncio
async def test_failed_reinitialization_is_not_retried_in_a_loop():
    upstream = ExpiringServer()
    async with serve(upstream) as (url, _):
        client = VoittaStatefulProxyClient(StreamableHttpTransport(url))
        try:
            async with client:
                first = text(await client.call_tool("session"))
            upstream.expired.add(first)
            with pytest.raises(McpError, match="not replayed"):
                async with client:
                    await client.call_tool("mutate")
            upstream.reject_status = 401
            rejected = upstream.rejected
            with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
                async with client:
                    pytest.fail("authentication failure must reach the caller")
            assert upstream.rejected == rejected + 1
            assert client._active_calls == 0
            upstream.reject_status = None
            async with client:
                assert text(await client.call_tool("session")) != first
            assert upstream.actions == 0
        finally:
            await client._disconnect(force=True)

@pytest.mark.asyncio
async def test_dead_runner_after_fourteen_calls_recovers_once_and_drains_active_calls(caplog):
    caplog.set_level(logging.INFO, logger="voitta-desktop.mcp")
    upstream = ExpiringServer()
    async with serve(upstream) as (url, _):
        client = VoittaStatefulProxyClient(StreamableHttpTransport(url))
        other = client.new()
        entered, release = asyncio.Event(), asyncio.Event()
        waiting = []

        async def in_flight():
            async with client:
                entered.set()
                await release.wait()

        async def next_call():
            async with client:
                return text(await client.call_tool("session"))

        pending = None
        try:
            for _ in range(14):
                first = await next_call()
            assert client._active_calls == 0
            assert client._session_state.nesting_counter == 14
            async with other:
                other_id = text(await other.call_tool("session"))
            pending = asyncio.create_task(in_flight())
            await asyncio.wait_for(entered.wait(), 5)
            runner = client._session_state.session_task
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            assert runner.done()
            waiting = [asyncio.create_task(next_call()) for _ in range(4)]
            await asyncio.sleep(0.1)
            assert all(not call.done() for call in waiting)
            release.set()
            await asyncio.wait_for(pending, 5)
            results = await asyncio.wait_for(asyncio.gather(*waiting), 5)
            assert len(set(results)) == 1 and results[0] != first
            assert client._generation == 2
            assert upstream.actions == 0
            async with other:
                assert text(await other.call_tool("session")) == other_id
            assert await next_call() == results[0]
            assert client._generation == 2
        finally:
            release.set()
            tasks = ([pending] if pending else []) + waiting
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client._disconnect(force=True)
            await other._disconnect(force=True)
    messages = [r.getMessage() for r in caplog.records if r.name == "voitta-desktop.mcp"]
    assert sum("reason=transport_task_ended" in msg for msg in messages) == 1
    assert sum("upstream recovered" in msg for msg in messages) == 1
