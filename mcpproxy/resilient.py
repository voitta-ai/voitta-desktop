"""Resilient proxy provider with disk-backed tool/resource caching."""

import json
import logging
import re
from pathlib import Path

import mcp.types
from fastmcp import FastMCP as FastMCPServer
from fastmcp.server.providers.proxy import FastMCPProxy, ProxyClient, ProxyProvider, ProxyTool

logger = logging.getLogger("voitta-desktop.mcp")

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

    async def _list_tools(self):
        try:
            tools = await super()._list_tools()
            if self._cache_listings and tools:
                _save_cache(self._backend_name, "tools", tools)
            self._stash_tool_names(tools)
            return self._filter_disabled(tools)
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for tool listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "tools", mcp.types.Tool, client_factory=self.client_factory)
                if cached is not None:
                    logger.info("[%s] Returning %d cached tools", self._backend_name, len(cached))
                    self._stash_tool_names(cached)
                    return self._filter_disabled(cached)
            return []

    async def _list_resources(self):
        try:
            resources = await super()._list_resources()
            if self._cache_listings and resources:
                _save_cache(self._backend_name, "resources", resources)
            return resources
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for resource listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "resources", mcp.types.Resource)
                if cached is not None:
                    logger.info("[%s] Returning %d cached resources", self._backend_name, len(cached))
                    return cached
            return []

    async def _list_resource_templates(self):
        try:
            templates = await super()._list_resource_templates()
            if self._cache_listings and templates:
                _save_cache(self._backend_name, "templates", templates)
            return templates
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for template listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "templates", mcp.types.ResourceTemplate)
                if cached is not None:
                    logger.info("[%s] Returning %d cached templates", self._backend_name, len(cached))
                    return cached
            return []

    async def _list_prompts(self):
        try:
            prompts = await super()._list_prompts()
            if self._cache_listings and prompts:
                _save_cache(self._backend_name, "prompts", prompts)
            return prompts
        except Exception as e:
            logger.warning("[%s] Upstream unavailable for prompt listing: %s", self._backend_name, e)
            if self._cache_listings:
                cached = _load_cache(self._backend_name, "prompts", mcp.types.Prompt)
                if cached is not None:
                    logger.info("[%s] Returning %d cached prompts", self._backend_name, len(cached))
                    return cached
            return []


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
