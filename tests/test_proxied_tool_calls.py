"""A mounted backend's tools must be *callable*, not merely listable.

Nothing exercised the tools/call path through ``ResilientFastMCPProxy``, and
that is exactly where fastmcp 3.4.7 broke us. Its ``ProxyProvider`` grew a
``_get_tool`` that reads a private ``_tools_cache`` filled only by its own
``_list_tools``; we override ``_list_tools`` to serve the disk cache and never
set that field, so the call path hit a bare ``assert cache is not None``.
fastmcp turns the failure into ``NotFoundError``, surfaced to the client as
``Unknown tool: 'vim_search'``.

The symptom was nasty precisely because listing kept working: every backend
advertised its full tool set and every call failed, with nothing in the log.
These tests call through the mount, so a provider that can only *list* fails
them.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP

from mcpproxy.resilient import ResilientFastMCPProxy

PREFIX = "vim"


def _upstream() -> FastMCP:
    server = FastMCP("upstream")

    @server.tool()
    def list_indexed_folders() -> str:
        """Stand-in for the RAG backend's cheapest tool."""
        return "McKinsey Quarterly"

    @server.tool()
    def search(query: str) -> str:
        return f"hit: {query}"

    return server


def _mounted(upstream: FastMCP) -> FastMCP:
    """Wire a backend exactly as mcpproxy.server does, minus the network."""
    proxy = ResilientFastMCPProxy(
        client_factory=lambda: Client(upstream),
        name=PREFIX,
        backend_name="Test Backend",
        cache_listings=False,        # keeps the test off the disk cache
        app_ref=None,
        prefix=PREFIX,
    )
    main = FastMCP("voitta-desktop")
    main.mount(proxy, prefix=PREFIX)
    return main


@pytest.mark.asyncio
async def test_prefixed_tool_can_be_called_not_just_listed():
    """The regression: listing succeeded while every call raised Unknown tool."""
    main = _mounted(_upstream())
    async with Client(main) as client:
        names = {t.name for t in await client.list_tools()}
        assert f"{PREFIX}_list_indexed_folders" in names, names

        result = await client.call_tool(f"{PREFIX}_list_indexed_folders", {})
        assert "McKinsey" in str(result.content), result.content


@pytest.mark.asyncio
async def test_call_passes_arguments_through():
    main = _mounted(_upstream())
    async with Client(main) as client:
        result = await client.call_tool(f"{PREFIX}_search", {"query": "budget"})
        assert "hit: budget" in str(result.content), result.content


@pytest.mark.asyncio
async def test_provider_resolves_by_unprefixed_name():
    """The mount strips the prefix before asking the provider, so the
    provider's own lookup must answer on the upstream's bare name."""
    proxy = ResilientFastMCPProxy(
        client_factory=lambda: Client(_upstream()),
        name=PREFIX, backend_name="Test Backend",
        cache_listings=False, app_ref=None, prefix=PREFIX,
    )
    provider = proxy._resilient_provider

    assert await provider.get_tool("list_indexed_folders") is not None
    # A name that does not exist must be a clean miss, not an exception.
    assert await provider.get_tool("no_such_tool") is None


@pytest.mark.asyncio
async def test_unknown_tool_still_reports_missing():
    main = _mounted(_upstream())
    async with Client(main) as client:
        with pytest.raises(Exception) as err:
            await client.call_tool(f"{PREFIX}_does_not_exist", {})
        assert "does_not_exist" in str(err.value)
