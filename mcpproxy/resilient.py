"""Resilient proxy provider with disk-backed tool/resource caching."""

import asyncio
import json
import logging
import re
import time
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
# - REFRESH_TIMEOUT_S applies to the user-triggered "Refresh LLM Tools"
#   menu action — generous because the user is explicitly waiting.
LISTING_TIMEOUT_S = 6.0
LISTING_FIRST_FILL_TIMEOUT_S = 15.0
REFRESH_TIMEOUT_S = 30.0

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

    # Suppress repeat "Upstream unavailable" warnings for the same backend +
    # error class within this window. Without this, an expired Google
    # Workspace token produces a 401-flavoured WARNING on every popup the
    # user opens — easily 50+ identical lines per hour.
    _WARN_WINDOW_S = 3600.0

    def __init__(self, client_factory, *, backend_name: str = "upstream", cache_listings: bool = False,
                 app_ref=None, prefix: str = ""):
        super().__init__(client_factory)
        self._backend_name = backend_name
        self._cache_listings = cache_listings
        self._app_ref = app_ref
        self._prefix = prefix
        self._warn_state: dict[tuple[str, str], dict] = {}

    def _warn_upstream(self, kind: str, exc: Exception):
        """Throttled WARNING for repeated upstream failures.

        First failure of a given (kind, exc-class) for this backend logs at
        WARNING. Repeats within _WARN_WINDOW_S log at DEBUG and increment a
        suppression counter. The next WARNING after the window includes
        "(suppressed N similar)" so nothing is silently lost.
        """
        now = time.monotonic()
        key = (kind, type(exc).__name__)
        state = self._warn_state.setdefault(key, {"last_warn_at": 0.0, "suppressed": 0})
        if now - state["last_warn_at"] < self._WARN_WINDOW_S:
            state["suppressed"] += 1
            logger.debug("[%s] Upstream unavailable for %s (suppressed): %s",
                         self._backend_name, kind, exc)
            return
        suffix = ""
        if state["suppressed"]:
            mins = int((now - state["last_warn_at"]) / 60)
            suffix = f" (suppressed {state['suppressed']} similar in last {mins} min)"
        state["last_warn_at"] = now
        state["suppressed"] = 0
        logger.warning("[%s] Upstream unavailable for %s: %s%s",
                       self._backend_name, kind, exc, suffix)

    def _stash_tool_names(self, tools):
        """Mirror the upstream tool list into app_ref._mcp_tools for the gate
        popup and the settings tool tree. Always writes (so empty upstream
        clears stale names instead of leaving them stuck)."""
        if self._app_ref is None:
            return
        names = [f"{self._prefix}_{t.name}" if self._prefix else t.name for t in (tools or [])]
        self._app_ref._mcp_tools[self._prefix] = sorted(names)

    async def fetch_upstream_instructions(self) -> str:
        """Open a one-shot client and return upstream `initialize.instructions`.

        Returns "" on any failure. Result is cached on app_ref so subsequent
        prompt rebuilds are free. Caller decides when to invoke — we don't
        wire this into _list_tools to keep the listing path lean.
        """
        cache = getattr(self._app_ref, "_mcp_upstream_instructions", None) if self._app_ref else None
        if cache is not None and self._prefix in cache:
            return cache[self._prefix]
        text = ""
        try:
            client = self.client_factory()
            async with client:
                ir = getattr(client, "initialize_result", None)
                text = (getattr(ir, "instructions", "") or "").strip()
        except Exception as e:
            logger.debug("[%s] upstream instructions fetch failed: %s", self._backend_name, e)
        if cache is not None:
            cache[self._prefix] = text
        return text

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
            self._warn_upstream("tool listing", e)
            return []

    async def _refresh_tools(self):
        tools = await super()._list_tools()
        if tools:
            _save_cache(self._backend_name, "tools", tools)
            self._stash_tool_names(tools)

    async def force_refresh(self) -> tuple[bool, int, str | None]:
        """Synchronously fetch fresh tools from upstream — used by the manual
        "Refresh LLM Tools" menu action.

        Cache replacement is conditional: the disk cache is only overwritten
        on a successful fetch. Failure, timeout, and cancellation leave both
        the disk cache and the in-memory stash intact, so clients keep being
        served the last-known-good tool list while the user retries or moves
        on. A successful but empty upstream response is treated as ground
        truth and wipes the cache (the backend has 0 tools right now).

        Returns (success, tool_count, error_message).
        """
        try:
            tools = await asyncio.wait_for(
                ProxyProvider._list_tools(self),
                timeout=REFRESH_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            return False, 0, "cancelled"
        except asyncio.TimeoutError:
            return False, 0, f"timeout after {REFRESH_TIMEOUT_S:.0f}s"
        except Exception as e:
            return False, 0, str(e)

        if tools:
            _save_cache(self._backend_name, "tools", tools)
        else:
            try:
                _cache_path(self._backend_name, "tools").unlink(missing_ok=True)
            except Exception as e:
                logger.debug("[%s] couldn't unlink stale tools cache: %s", self._backend_name, e)
        self._stash_tool_names(tools)
        return True, len(tools or []), None

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
            self._warn_upstream("resource listing", e)
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
            self._warn_upstream("template listing", e)
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
            self._warn_upstream("prompt listing", e)
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
        self._resilient_provider = ResilientProxyProvider(
            client_factory, backend_name=backend_name, cache_listings=cache_listings,
            app_ref=app_ref, prefix=prefix,
        )
        self.add_provider(self._resilient_provider)
        self._backend_name = backend_name
        # Tool-name prefix (e.g. "voitta_rag"); empty for unprefixed backends.
        # Read by Settings → Tools to reconstruct prefixed tool names from
        # the per-backend disk cache without a live HTTP listing.
        self._prefix = prefix
        # Last force_refresh outcome — None if successful or never attempted,
        # error string otherwise. Read by the Info-tab diagram to show ⚠ when
        # cache is empty AND last refresh failed.
        self._last_refresh_error: str | None = None

    async def force_refresh(self) -> tuple[bool, int, str | None]:
        """Delegate to provider — fetch fresh, return summary."""
        ok, count, err = await self._resilient_provider.force_refresh()
        self._last_refresh_error = None if ok else err
        return ok, count, err

    async def fetch_upstream_instructions(self) -> str:
        return await self._resilient_provider.fetch_upstream_instructions()

    def peek_cached(self) -> int:
        """Synchronously read the on-disk tool count for this backend.

        Returns 0 if the cache file is missing or unreadable. Used by the
        LLM Tools Status popup to render initial state without contacting
        upstream or running an event loop.
        """
        path = _cache_path(self._backend_name, "tools")
        if not path.exists():
            return 0
        try:
            return len(json.loads(path.read_text()))
        except Exception:
            return 0

    def peek_cached_names(self, prefix: str = "") -> list[str]:
        """Synchronously read tool names from the on-disk cache, optionally
        prefixed (e.g. ``voitta_rag_search``).

        Same disk file as :meth:`peek_cached`. Used by the Settings tools
        tree, which needs names — not just counts — to render per-tool
        toggles. Returns ``[]`` if the cache is missing or unreadable.
        """
        path = _cache_path(self._backend_name, "tools")
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except Exception:
            return []
        names: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(f"{prefix}_{name}" if prefix else name)
        return sorted(names)
