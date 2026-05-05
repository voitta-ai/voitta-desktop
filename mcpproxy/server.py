"""FastMCP proxy server setup — mounts all MCP backends."""

import asyncio
import json
import logging

from fastmcp import FastMCP as FastMCPServer
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.client.transports import StreamableHttpTransport

import base64

from fastmcp.utilities.types import Image

from optimizers.image import vt_object_store
from .resilient import ResilientFastMCPProxy
from .backends import MCP_BACKENDS, simple_backends, build_instructions

logger = logging.getLogger("voitta-desktop.mcp")


def make_rag_client_factory(app_ref):
    """Return a factory that creates a ProxyClient with current RAG auth headers."""
    def factory():
        headers = {}
        for app_type in ("microsoft", "google"):
            active_id = app_ref._active_app.get(("rag", app_type))
            if not active_id:
                continue
            state = app_ref._auth.get((active_id, "rag"), {})
            if not state.get("token"):
                continue
            suffix = app_type.capitalize()
            headers[f"X-Auth-Token-{suffix}"] = f"Bearer {state['token']}"
            profile = state.get("profile") or {}
            if profile.get("email"):
                headers[f"X-Auth-Email-{suffix}"] = profile["email"]
            if profile.get("name"):
                headers[f"X-Auth-Name-{suffix}"] = profile["name"]
        url = f"{app_ref.voitta_rag_url.rstrip('/')}/mcp/mcp"
        logger.debug("RAG factory: url=%s, %d headers", url, len(headers))
        transport = StreamableHttpTransport(url=url, headers=headers)
        return ProxyClient(transport)
    return factory


def make_image_rag_client_factory(app_ref):
    """Return a factory that creates a ProxyClient for Voitta Image RAG with Bearer auth."""
    def factory():
        headers = {}
        key = (app_ref.voitta_image_rag_key or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = app_ref.voitta_image_rag_url
        logger.debug("Image RAG factory: url=%s, has_key=%s", url, bool(key))
        transport = StreamableHttpTransport(url=url, headers=headers)
        return ProxyClient(transport)
    return factory


def make_paperclip_client_factory(app_ref):
    """Return a factory that creates a ProxyClient for Paperclip with X-API-Key auth."""
    def factory():
        headers = {}
        key = (app_ref.paperclip_key or "").strip()
        if key:
            headers["X-API-Key"] = key
        url = app_ref.paperclip_url
        logger.debug("Paperclip factory: url=%s, has_key=%s", url, bool(key))
        transport = StreamableHttpTransport(url=url, headers=headers)
        return ProxyClient(transport)
    return factory


def make_google_client_factory(app_ref):
    """Return a factory that creates a ProxyClient with current Google Workspace Bearer token."""
    def factory():
        headers = {}
        active_id = app_ref._active_app.get(("google_workspace", "google"))
        if active_id:
            state = app_ref._auth.get((active_id, "google_workspace"), {})
            if state.get("token"):
                headers["Authorization"] = f"Bearer {state['token']}"
                profile = state.get("profile") or {}
                if profile.get("email"):
                    headers["X-Auth-Email"] = profile["email"]
                if profile.get("name"):
                    headers["X-Auth-Name"] = profile["name"]
        url = f"{app_ref.edit_proxy_url.rstrip('/')}/mcp"
        logger.debug("Google factory: url=%s, headers=%s", url, list(headers.keys()))
        transport = StreamableHttpTransport(url=url, headers=headers)
        return ProxyClient(transport)
    return factory


from fastmcp.server.middleware import Middleware as FastMCPMiddleware


class ToolGateMiddleware(FastMCPMiddleware):
    """Shows a tool gate popup on external tools/list requests.

    - First external request: show popup, remember result
    - Requests within REUSE_WINDOW_S: reuse last result (no popup)
    - After window expires: show popup again
    - Menu "MCP tool gate" re-arms for immediate popup
    """

    REUSE_WINDOW_S = 1.0

    def __init__(self, app_ref):
        super().__init__()
        self._app_ref = app_ref
        self._last_disabled: set[str] | None = None
        self._last_time: float = 0
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()
        return self._lock

    async def on_list_tools(self, context, call_next):
        import time

        client_name, session_id = self._get_client_info(context)

        # Skip internal calls
        if client_name is None or client_name == "settings":
            logger.warning("tool_gate: pass-through (%s)", client_name or "no session")
            return await call_next(context)

        # Serialize popup access — second request waits for first popup to finish
        async with self._get_lock():
            # Reuse recent result within the window
            if self._last_disabled is not None and (time.time() - self._last_time) < self.REUSE_WINDOW_S:
                logger.warning("tool_gate: reusing recent result (client=%s, %.1fs ago)",
                               client_name, time.time() - self._last_time)
                tools = await call_next(context)
                return [t for t in tools if t.name not in self._last_disabled]

            return await self._show_gate(context, call_next, client_name, session_id)

    def _get_client_info(self, context) -> tuple[str | None, str | None]:
        """Extract client name and session ID from context."""
        try:
            ctx = context.fastmcp_context
            if ctx is None or ctx.session is None:
                return None, None
            params = ctx.session.client_params
            name = params.clientInfo.name if params and params.clientInfo else "unknown"
            return name, ctx.session_id
        except Exception:
            return None, None

    async def _show_gate(self, context, call_next, client_name, session_id):
        """Show the tool gate popup and filter tools based on user selection."""
        logger.warning("tool_gate: showing popup (client=%s, session=%s)", client_name, session_id)
        try:
            tools = await call_next(context)
            if not tools:
                return tools

            tool_groups = self._app_ref._build_tool_tree()
            disabled = set(getattr(self._app_ref, "disabled_tools", set()))

            meta = {}
            try:
                from fastmcp.server.dependencies import get_http_request
                http_req = get_http_request()
                if http_req.client:
                    meta["remote"] = f"{http_req.client.host}:{http_req.client.port}"
            except Exception:
                pass
            try:
                ctx = context.fastmcp_context
                if ctx:
                    meta["session_id"] = ctx.session_id
                    if ctx.client_id:
                        meta["client_id"] = ctx.client_id
                    meta["transport"] = ctx.transport or "?"
                    params = ctx.session.client_params
                    if params:
                        ci = params.clientInfo
                        if ci:
                            meta["client"] = f"{ci.name} {ci.version}"
                            if ci.title:
                                meta["client_title"] = ci.title
                            if ci.websiteUrl:
                                meta["website"] = ci.websiteUrl
                            if hasattr(ci, "model_extra") and ci.model_extra:
                                for k, v in ci.model_extra.items():
                                    meta[f"client.{k}"] = str(v)
                        meta["protocol"] = str(params.protocolVersion)
                        caps = params.capabilities
                        if caps:
                            cap_list = []
                            for field in ("roots", "sampling", "elicitation", "tasks"):
                                if getattr(caps, field, None):
                                    cap_list.append(field)
                            if cap_list:
                                meta["capabilities"] = ", ".join(cap_list)
                            if caps.experimental:
                                meta["experimental"] = ", ".join(caps.experimental.keys())
                        if hasattr(params, "model_extra") and params.model_extra:
                            for k, v in params.model_extra.items():
                                meta[k] = str(v) if not isinstance(v, str) else v
            except Exception:
                pass

            from ui.tool_gate import show_tool_gate
            gate_result = await show_tool_gate(tool_groups, disabled, meta)

            import time
            if gate_result is None:
                self._last_disabled = set()  # cancel = no tools
                self._last_time = time.time()
                return []

            self._last_disabled = set(gate_result)
            self._last_time = time.time()
            return [t for t in tools if t.name not in self._last_disabled]
        except asyncio.CancelledError:
            logger.warning("tool_gate: client disconnected, returning empty tools")
            return []
        except Exception as e:
            logger.error("tool_gate: popup error: %s", e, exc_info=True)
            raise


def run_mcp_proxy(app_ref, port: int, jira_mcp_port: int):
    """Run unified FastMCP proxy server mounting all backends. Blocks forever."""
    logger.info("run_mcp_proxy starting (port=%d)", port)
    gate = ToolGateMiddleware(app_ref)
    app_ref._tool_gate = gate
    main_server = FastMCPServer(
        "voitta-desktop",
        instructions=build_instructions(),
        middleware=[gate],
    )
    # Disable tools/list_changed notifications — they cause Claude Code
    # to re-fetch tools on every backend mount and settings poll,
    # triggering unwanted tool gate popups.
    main_server._mcp_server.notification_options.tools_changed = False

    # ── Core backends (custom auth) ─────────────────────────────────

    rag_proxy = ResilientFastMCPProxy(
        client_factory=make_rag_client_factory(app_ref),
        name="voitta-rag",
        backend_name="RAG",
        cache_listings=True,
        app_ref=app_ref, prefix="voitta_rag",
    )
    main_server.mount(rag_proxy, prefix="voitta_rag")

    google_proxy = ResilientFastMCPProxy(
        client_factory=make_google_client_factory(app_ref),
        name="google-workspace",
        backend_name="Google Workspace",
        cache_listings=True,
        app_ref=app_ref, prefix="google_workspace",
    )
    main_server.mount(google_proxy, prefix="google_workspace")

    jira_proxy = ResilientFastMCPProxy(
        client_factory=lambda: ProxyClient(
            StreamableHttpTransport(url=f"http://localhost:{jira_mcp_port}/mcp")
        ),
        name="jira",
        backend_name="Jira",
        app_ref=app_ref, prefix="jira",
    )
    main_server.mount(jira_proxy, prefix="jira")

    image_rag_proxy = ResilientFastMCPProxy(
        client_factory=make_image_rag_client_factory(app_ref),
        name="voitta-image-rag",
        backend_name="Voitta Image RAG",
        cache_listings=True,
        app_ref=app_ref, prefix="vim",
    )
    main_server.mount(image_rag_proxy, prefix="vim")

    paperclip_proxy = ResilientFastMCPProxy(
        client_factory=make_paperclip_client_factory(app_ref),
        name="paperclip",
        backend_name="Paperclip",
        cache_listings=True,
        app_ref=app_ref, prefix="paperclip",
    )
    main_server.mount(paperclip_proxy, prefix="paperclip")

    # ── Simple backends (driven from backends.py) ───────────────────

    for b in simple_backends():
        proxy = ResilientFastMCPProxy(
            client_factory=lambda b=b: ProxyClient(
                StreamableHttpTransport(url=b["url"], headers=b.get("headers", {}))
            ),
            name=b["prefix"].replace("_", "-"),
            backend_name=b["label"],
            cache_listings=True,
            app_ref=app_ref, prefix=b["prefix"],
        )
        main_server.mount(proxy, prefix=b["prefix"])

    # ── Built-in tools ──────────────────────────────────────────────

    @main_server.tool()
    def get_vt_object(hash: str) -> Image | str:
        """Retrieve a previously removed image or object by its hash."""
        obj = vt_object_store.get(hash)
        if obj is None:
            return f"No object found for hash {hash}"

        if obj["type"] == "image":
            item = obj["data"]
            src = item.get("source", {})
            raw = base64.b64decode(src.get("data", ""))
            media_type = src.get("media_type", "image/png")
            fmt = media_type.split("/")[-1] if "/" in media_type else "png"
            return Image(data=raw, format=fmt)

        if obj["type"] in ("bash", "tool_result"):
            return obj["data"] if isinstance(obj["data"], str) else json.dumps(obj["data"])

        return f"Unknown object type: {obj['type']}"

    # ── Logging ─────────────────────────────────────────────────────

    logger.info("FastMCP proxy on http://127.0.0.1:%d/mcp", port)
    logger.info("  RAG -> %s", app_ref.voitta_rag_url)
    logger.info("  Google -> %s", app_ref.edit_proxy_url)
    logger.info("  Jira -> http://localhost:%d/mcp", jira_mcp_port)
    logger.info("  Image RAG -> %s (key=%s)", app_ref.voitta_image_rag_url,
                "set" if (app_ref.voitta_image_rag_key or "").strip() else "blank")
    logger.info("  Paperclip -> %s (key=%s)", app_ref.paperclip_url,
                "set" if (app_ref.paperclip_key or "").strip() else "blank")
    for b in simple_backends():
        logger.info("  %s -> %s", b["label"], b["url"])
    main_server.run(transport="streamable-http", host="127.0.0.1", port=port)
