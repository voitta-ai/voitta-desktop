"""The provider's own upstream calls close their connections, and are rare.

On 2026-10-16 Voitta Desktop held 504 open connections to kvm.voitta.ai and
nginx there refused every new one. Every tool call listed the backend's tools
again through a background refresh, and that listing used a
StatefulProxyClient, whose __aexit__ never closes: one connection left open per
tool call. These tests run a real upstream over HTTP and count its open TCP
connections.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import StatefulProxyClient

from mcpproxy import resilient
from mcpproxy import server as proxy_server
from mcpproxy.resilient import ResilientFastMCPProxy, ResilientProxyProvider


class _CountListings(Middleware):
    def __init__(self):
        self.count = 0

    async def on_list_tools(self, context, call_next):
        self.count += 1
        return await call_next(context)


def _upstream() -> tuple[FastMCP, _CountListings]:
    upstream = FastMCP("upstream")
    listings = _CountListings()
    upstream.add_middleware(listings)

    @upstream.tool()
    async def ping(ctx: Context) -> str:
        return ctx.session_id

    return upstream, listings


@asynccontextmanager
async def _serve(upstream: FastMCP):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(upstream.http_app(), log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("upstream did not start")
        yield f"http://127.0.0.1:{port}/mcp", server
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def _until(condition, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.fixture
def tool_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(resilient, "TOOL_CACHE_DIR", tmp_path)


@pytest.mark.asyncio
async def test_fastmcp_stateful_client_keeps_its_connection_after_exit():
    """Why the provider must not list with a StatefulProxyClient. If this ever
    fails, fastmcp closes it on exit now and the closing factory in
    ResilientProxyProvider may be revisited."""
    upstream, _ = _upstream()
    async with _serve(upstream) as (url, server):
        client = StatefulProxyClient(StreamableHttpTransport(url))
        async with client:
            await client.list_tools()
        await asyncio.sleep(0.3)
        assert server.server_state.connections, "the connection was closed on exit"
        await client._disconnect(force=True)  # what a server session's exit stack does
        await _until(lambda: not server.server_state.connections)


@pytest.mark.asyncio
async def test_tool_lookups_refresh_once_and_leave_no_connection_open(tool_cache):
    upstream, listings = _upstream()
    async with _serve(upstream) as (url, server):
        factory = proxy_server._make_static_headers_factory(url, {})
        prov = ResilientProxyProvider(factory, backend_name="t", cache_listings=True)
        ok, count, err = await prov.force_refresh()
        assert ok and count == 1, err
        assert listings.count == 1

        for _ in range(100):  # each tool call looks the tool up through this
            await prov._list_tools()
            await asyncio.sleep(0)
        await asyncio.gather(*prov._refresh_tasks.values())

        assert listings.count == 2, "one background refresh, not one per lookup"
        await _until(lambda: not server.server_state.connections)


@pytest.mark.asyncio
async def test_tool_calls_in_one_session_share_one_upstream_session(tool_cache):
    upstream, _ = _upstream()
    async with _serve(upstream) as (url, server):
        proxy = ResilientFastMCPProxy(
            client_factory=proxy_server._make_static_headers_factory(url, {}),
            name="b", backend_name="t", cache_listings=True)
        main = FastMCP("downstream")
        main.mount(proxy, prefix="b")
        assert (await proxy.force_refresh())[0]

        async with Client(main) as window:
            sessions = set()
            for _ in range(5):
                result = await window.call_tool("b_ping", {})
                sessions.add(result.content[0].text)
            assert len(sessions) == 1, "every call in the window reuses one upstream session"
            await asyncio.gather(*proxy._resilient_provider._refresh_tasks.values())

        # The window's connection closes with its session; the refresh's closed itself.
        await _until(lambda: not server.server_state.connections)
