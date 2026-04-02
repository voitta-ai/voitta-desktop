"""FastMCP proxy server setup — mounts all MCP backends."""

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
    """FastMCP middleware that shows a tool gate popup on every tools/list request."""

    def __init__(self, app_ref):
        super().__init__()
        self._app_ref = app_ref

    async def on_list_tools(self, context, call_next):
        logger.info("ToolGateMiddleware: tools/list intercepted")
        try:
            tools = await call_next(context)

            if not tools:
                return tools

            tool_groups = self._app_ref._build_tool_tree()
            disabled = set(getattr(self._app_ref, "disabled_tools", set()))

            # Extract client metadata
            meta = {}
            try:
                ctx = context.fastmcp_context
                if ctx:
                    meta["session_id"] = ctx.session_id
                    params = ctx.session.client_params
                    if params and params.clientInfo:
                        meta["client_name"] = params.clientInfo.name
                        meta["client_version"] = params.clientInfo.version
            except Exception:
                pass

            from ui.tool_gate import show_tool_gate
            gate_result = await show_tool_gate(tool_groups, disabled, meta)

            if gate_result is None:
                return []

            disabled_set = set(gate_result)
            return [t for t in tools if t.name not in disabled_set]
        except Exception as e:
            logger.error("ToolGateMiddleware error: %s", e, exc_info=True)
            raise


def run_mcp_proxy(app_ref, port: int, jira_mcp_port: int):
    """Run unified FastMCP proxy server mounting all backends. Blocks forever."""
    logger.info("run_mcp_proxy starting (port=%d)", port)
    main_server = FastMCPServer(
        "voitta-desktop",
        instructions=build_instructions(),
        middleware=[ToolGateMiddleware(app_ref)],
    )

    # ── Core backends (custom auth) ─────────────────────────────────

    rag_proxy = ResilientFastMCPProxy(
        client_factory=make_rag_client_factory(app_ref),
        name="voitta-rag",
        backend_name="RAG",
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

    # ── Simple backends (driven from backends.py) ───────────────────

    for b in simple_backends():
        proxy = ResilientFastMCPProxy(
            client_factory=lambda b=b: ProxyClient(
                StreamableHttpTransport(url=b["url"], headers=b.get("headers", {}))
            ),
            name=b["prefix"].replace("_", "-"),
            backend_name=b["label"],
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
    for b in simple_backends():
        logger.info("  %s -> %s", b["label"], b["url"])
    main_server.run(transport="streamable-http", host="127.0.0.1", port=port)
