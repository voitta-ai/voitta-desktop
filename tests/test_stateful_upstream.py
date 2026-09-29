"""Remote backends hold one upstream connection per server session.

Before this, every tool call built a fresh ProxyClient: DNS + TLS + MCP
initialize + the call, serially, on every invocation. For a remote backend
on an 80–140 ms WAN that was 3–7 s per call. fastmcp's StatefulProxyClient
caches one connection per *server session* and closes it on the session's
exit stack — the fix is to use it, at the right seam.

The seam matters. ``ResilientProxyProvider`` makes its own upstream calls
(listings, the capability handshake, the instructions fetch) from background
tasks with no server session in scope; ``new_stateful`` reads
``get_context().session`` and would raise there. So the provider keeps an
ephemeral factory for itself and hands the session-bound one only to the
ProxyTools that run inside a request. These tests pin that split.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import mcp.types
import pytest
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import ProxyClient, ProxyTool, StatefulProxyClient

from mcpproxy import server as proxy_server
from mcpproxy.resilient import ResilientProxyProvider


def _stateful_factory():
    return StatefulProxyClient(StreamableHttpTransport("https://upstream.invalid/mcp"))


def _ephemeral_factory():
    return ProxyClient(StreamableHttpTransport("https://upstream.invalid/mcp"))


# -- the factories ------------------------------------------------------------

def test_static_header_factory_yields_stateful_client() -> None:
    client = proxy_server._make_static_headers_factory("https://x.invalid/mcp", {"Authorization": "Bearer t"})()
    assert isinstance(client, StatefulProxyClient)


def test_voitta_rag_factory_yields_stateful_client() -> None:
    class _App:
        _active_app: dict = {}
        _auth: dict = {}

    client = proxy_server._make_voitta_rag_legacy_factory(_App(), "https://x.invalid/mcp")()
    assert isinstance(client, StatefulProxyClient)


def test_oauth_factory_yields_stateful_client() -> None:
    class _App:
        _active_app: dict = {}
        _auth: dict = {}

    client = proxy_server._make_oauth_app_factory(_App(), "https://x.invalid/mcp", "b", "google")()
    assert isinstance(client, StatefulProxyClient)


def test_stdio_factories_stay_ephemeral() -> None:
    """A subprocess transport already keeps its process alive; binding it to
    a server session would be the wrong lifetime (one process per window)."""
    with patch.object(proxy_server, "_resolve_npx", return_value=("/usr/bin/true", {})):
        client = proxy_server._make_npx_stdio_factory("pkg", [])()
    assert type(client) is ProxyClient


# -- the seam -----------------------------------------------------------------

def test_provider_keeps_ephemeral_factory_for_its_own_calls() -> None:
    prov = ResilientProxyProvider(_stateful_factory, backend_name="t")
    # The base factory the provider lists/handshakes with is untouched…
    assert prov.client_factory is _stateful_factory
    # …and the one it builds tools with is the session-bound method.
    assert prov._tool_client_factory.__name__ == "new_stateful"
    assert prov._tool_client_factory is not _stateful_factory


def test_provider_with_ephemeral_backend_uses_it_for_tools_too() -> None:
    prov = ResilientProxyProvider(_ephemeral_factory, backend_name="t")
    assert prov._tool_client_factory is _ephemeral_factory


def test_tool_factory_refuses_to_run_outside_a_request() -> None:
    """The reason the split exists: outside a session, new_stateful must
    raise rather than silently mint an unbound connection."""
    prov = ResilientProxyProvider(_stateful_factory, backend_name="t")
    with pytest.raises(RuntimeError):
        prov._tool_client_factory()


@pytest.mark.asyncio
async def test_live_listing_lists_ephemerally_but_builds_tools_stateful() -> None:
    """The base class listed and built with one factory; ours must use two."""
    prov = ResilientProxyProvider(_stateful_factory, backend_name="t")
    listed = [mcp.types.Tool(name="ping", inputSchema={"type": "object"})]

    class _FakeClient:
        opened = 0

        async def __aenter__(self):
            _FakeClient.opened += 1
            return self

        async def __aexit__(self, *a):
            return False

        async def list_tools(self):
            return listed

    with patch.object(prov, "client_factory", lambda: _FakeClient()):
        tools = await prov._list_tools_live()

    assert _FakeClient.opened == 1, "the listing itself used the ephemeral factory"
    assert len(tools) == 1 and isinstance(tools[0], ProxyTool)
    # Every tool carries the session-bound factory, not the listing one.
    assert tools[0]._client_factory is prov._tool_client_factory


@pytest.mark.asyncio
async def test_live_listing_preserves_base_error_semantics() -> None:
    prov = ResilientProxyProvider(_stateful_factory, backend_name="t")

    class _NoTools:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def list_tools(self):
            raise mcp.McpError(mcp.types.ErrorData(code=mcp.types.METHOD_NOT_FOUND, message="no"))

    class _Broken(_NoTools):
        async def list_tools(self):
            raise mcp.McpError(mcp.types.ErrorData(code=-32000, message="boom"))

    with patch.object(prov, "client_factory", lambda: _NoTools()):
        assert await prov._list_tools_live() == []
    with patch.object(prov, "client_factory", lambda: _Broken()):
        with pytest.raises(mcp.McpError):
            await prov._list_tools_live()


def test_every_tool_building_path_uses_the_session_factory() -> None:
    """Three code paths construct ProxyTools; none may use the listing factory.
    Guards against a future edit reintroducing ``super()._list_tools()``."""
    import inspect
    from mcpproxy import resilient

    src = inspect.getsource(resilient)
    assert "super()._list_tools()" not in src
    assert "ProxyProvider._list_tools(self)" not in src
    # _load_cache is the disk path; it must receive the tool factory.
    assert "client_factory=self._tool_client_factory" in src
