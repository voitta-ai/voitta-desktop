"""Resilient proxy provider with disk-backed tool/resource caching."""

import asyncio
import json
import logging
import re
from pathlib import Path

import mcp.types
from fastmcp import FastMCP as FastMCPServer
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient, ProxyProvider, ProxyTool

logger = logging.getLogger("voitta-desktop.mcp")

# Hard ceiling on a single backend's listing call.
# - LISTING_TIMEOUT_S applies to background refresh (cache exists).
# - LISTING_FIRST_FILL_TIMEOUT_S applies when no cache exists and we MUST
#   block on upstream — give plane Wi-Fi a real chance to populate. After
#   that one success, stale-while-revalidate keeps every subsequent popup
#   instant regardless of network.
LISTING_TIMEOUT_S = 6.0
LISTING_FIRST_FILL_TIMEOUT_S = 15.0

TOOL_CACHE_DIR = Path.home() / ".voitta_desktop_cache"


def _cache_path(backend_name: str, kind: str) -> Path:
    safe_name = re.sub(r"[^\w\-]", "_", backend_name).lower()
    return TOOL_CACHE_DIR / f"{safe_name}_{kind}.json"


def _proxy_tool_to_mcp_dict(item) -> dict:
    """Convert a ProxyTool to an mcp.types.Tool-compatible dict."""
    d = item.model_dump()
    if "inputSchema" not in d and "parameters" in d:
        d["inputSchema"] = d.pop("parameters")
    if "outputSchema" not in d and "output_schema" in d:
        d["outputSchema"] = d.pop("output_schema")
    for extra in ("version", "tags", "task_config", "serializer", "timeout"):
        d.pop(extra, None)
    return d


def _save_cache(backend_name: str, kind: str, items):
    try:
        TOOL_CACHE_DIR.mkdir(exist_ok=True)
        data = [_proxy_tool_to_mcp_dict(item) if kind == "tools" else item.model_dump()
                for item in items]
        _cache_path(backend_name, kind).write_text(
            json.dumps(data, default=lambda o: list(o) if isinstance(o, set) else str(o))
        )
        logger.info("[%s] Cached %d %s to disk", backend_name, len(data), kind)
    except Exception as e:
        logger.warning("[%s] Failed to write %s cache: %s", backend_name, kind, e)


def _load_cache(backend_name: str, kind: str, model_cls, client_factory=None):
    path = _cache_path(backend_name, kind)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if kind == "tools" and client_factory is not None:
            return [
                ProxyTool.from_mcp_tool(client_factory, mcp.types.Tool.model_validate(item))
                for item in data
            ]
        return [model_cls.model_validate(item) for item in data]
    except Exception as e:
        logger.warning("[%s] Failed to read %s cache: %s", backend_name, kind, e)
        return None


class ResilientProxyProvider(ProxyProvider):
    """ProxyProvider that catches all upstream errors and falls back to cache."""

    def __init__(self, client_factory, *, backend_name: str = "upstream", cache_listings: bool = False,
                 app_ref=None, prefix: str = ""):
        super().__init__(client_factory)
        self._backend_name = backend_name
        self._cache_listings = cache_listings
        self._app_ref = app_ref
        self._prefix = prefix

    def _is_tool_disabled(self, tool_name: str) -> bool:
        if self._app_ref is None:
            return False
        disabled = getattr(self._app_ref, "disabled_tools", set())
        full_name = f"{self._prefix}_{tool_name}" if self._prefix else tool_name
        return full_name in disabled

    def _stash_tool_names(self, tools):
        if self._app_ref is not None and tools:
            names = [f"{self._prefix}_{t.name}" if self._prefix else t.name for t in tools]
            self._app_ref._mcp_tools[self._prefix] = sorted(names)

    def _filter_disabled(self, tools):
        if not tools:
            return tools
        return [t for t in tools if not self._is_tool_disabled(t.name)]

    def _spawn_refresh(self, kind: str, coro_factory):
        """Kick off a background refresh for `kind` if one isn't already in flight.

        Stale-while-revalidate: when we serve cached data, we still want to
        keep the cache fresh, but we don't want to block the caller on the
        slow upstream. Dedupes per (backend, kind) so multiple rapid-fire
        listings don't pile up duplicate refresh tasks.
        """
        if not hasattr(self, "_refresh_tasks"):
            self._refresh_tasks: dict[str, asyncio.Task] = {}
        existing = self._refresh_tasks.get(kind)
        if existing and not existing.done():
            return
        async def _run():
            try:
                await asyncio.wait_for(coro_factory(), timeout=LISTING_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.debug("[%s] Background %s refresh timed out", self._backend_name, kind)
            except Exception as e:
                logger.debug("[%s] Background %s refresh failed: %s", self._backend_name, kind, e)
        self._refresh_tasks[kind] = asyncio.create_task(_run())

    async def _list_tools(self):
        # Stale-while-revalidate: if we have cache, serve it immediately and
        # refresh in background. The first listing on a fresh install still
        # blocks; everything after is instant regardless of network.
        if self._cache_listings:
            cached = _load_cache(self._backend_name, "tools", mcp.types.Tool, client_factory=self.client_factory)
            if cached is not None:
                self._stash_tool_names(cached)
                self._spawn_refresh("tools", self._refresh_tools)
                return cached

        try:
            tools = await asyncio.wait_for(super()._list_tools(), timeout=LISTING_FIRST_FILL_TIMEOUT_S)
            if self._cache_listings and tools:
                _save_cache(self._backend_name, "tools", tools)
            self._stash_tool_names(tools)
            return tools
        except asyncio.TimeoutError:
            logger.warning("[%s] Tool listing timed out after %.1fs", self._backend_name, LISTING_FIRST_FILL_TIMEOUT_S)
            return []
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for tool listing: %s", self._backend_name, e)
            return []

    async def _refresh_tools(self):
        tools = await super()._list_tools()
        if tools:
            _save_cache(self._backend_name, "tools", tools)
            self._stash_tool_names(tools)

    async def _list_resources(self):
        if self._cache_listings:
            cached = _load_cache(self._backend_name, "resources", mcp.types.Resource)
            if cached is not None:
                self._spawn_refresh("resources", self._refresh_resources)
                return cached
        try:
            resources = await asyncio.wait_for(super()._list_resources(), timeout=LISTING_FIRST_FILL_TIMEOUT_S)
            if self._cache_listings and resources:
                _save_cache(self._backend_name, "resources", resources)
            return resources
        except asyncio.TimeoutError:
            logger.warning("[%s] Resource listing timed out after %.1fs", self._backend_name, LISTING_FIRST_FILL_TIMEOUT_S)
            return []
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for resource listing: %s", self._backend_name, e)
            return []

    async def _refresh_resources(self):
        resources = await super()._list_resources()
        if resources:
            _save_cache(self._backend_name, "resources", resources)

    async def _list_resource_templates(self):
        if self._cache_listings:
            cached = _load_cache(self._backend_name, "templates", mcp.types.ResourceTemplate)
            if cached is not None:
                self._spawn_refresh("templates", self._refresh_templates)
                return cached
        try:
            templates = await asyncio.wait_for(super()._list_resource_templates(), timeout=LISTING_FIRST_FILL_TIMEOUT_S)
            if self._cache_listings and templates:
                _save_cache(self._backend_name, "templates", templates)
            return templates
        except asyncio.TimeoutError:
            logger.warning("[%s] Template listing timed out after %.1fs", self._backend_name, LISTING_FIRST_FILL_TIMEOUT_S)
            return []
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for template listing: %s", self._backend_name, e)
            return []

    async def _refresh_templates(self):
        templates = await super()._list_resource_templates()
        if templates:
            _save_cache(self._backend_name, "templates", templates)

    async def _list_prompts(self):
        if self._cache_listings:
            cached = _load_cache(self._backend_name, "prompts", mcp.types.Prompt)
            if cached is not None:
                self._spawn_refresh("prompts", self._refresh_prompts)
                return cached
        try:
            prompts = await asyncio.wait_for(super()._list_prompts(), timeout=LISTING_FIRST_FILL_TIMEOUT_S)
            if self._cache_listings and prompts:
                _save_cache(self._backend_name, "prompts", prompts)
            return prompts
        except asyncio.TimeoutError:
            logger.warning("[%s] Prompt listing timed out after %.1fs", self._backend_name, LISTING_FIRST_FILL_TIMEOUT_S)
            return []
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for prompt listing: %s", self._backend_name, e)
            return []

    async def _refresh_prompts(self):
        prompts = await super()._list_prompts()
        if prompts:
            _save_cache(self._backend_name, "prompts", prompts)


class ResilientFastMCPProxy(FastMCPProxy):
    """FastMCPProxy that uses ResilientProxyProvider for graceful upstream failure handling."""

    def __init__(self, *, client_factory, backend_name: str = "upstream", cache_listings: bool = False,
                 app_ref=None, prefix: str = "", **kwargs):
        FastMCPServer.__init__(self, **kwargs)
        self.client_factory = client_factory
        provider = ResilientProxyProvider(
            client_factory, backend_name=backend_name, cache_listings=cache_listings,
            app_ref=app_ref, prefix=prefix,
        )
        self.add_provider(provider)
