"""FastMCP proxy server setup — mounts RAG, Google Workspace, and Jira backends."""

import logging

from fastmcp import FastMCP as FastMCPServer
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.client.transports import StreamableHttpTransport

from .resilient import ResilientFastMCPProxy

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


def run_mcp_proxy(app_ref, port: int, jira_mcp_port: int):
    """Run unified FastMCP proxy server mounting all backends. Blocks forever."""
    main_server = FastMCPServer(
        "voitta-desktop",
        instructions=(
            "You are connected through Voitta Desktop, a unified MCP proxy. "
            "All tool names are prefixed by backend:\n"
            "  \u2022 voitta_rag_*   \u2014 RAG search, memory, file retrieval\n"
            "  \u2022 google_workspace_* \u2014 Google Workspace (Gmail, Drive, Sheets, Docs, Calendar)\n"
            "  \u2022 jira_*         \u2014 Jira issues, sprints, boards\n"
            "If a google_workspace_* tool fails with an auth error, "
            "ask the user to log in via the Voitta Desktop menu bar icon."
        ),
    )

    # RAG proxy with dynamic per-provider auth headers
    rag_proxy = ResilientFastMCPProxy(
        client_factory=make_rag_client_factory(app_ref),
        name="voitta-rag",
        backend_name="RAG",
    )
    main_server.mount(rag_proxy, prefix="voitta_rag")

    # Google Workspace proxy with dynamic Bearer token
    google_proxy = ResilientFastMCPProxy(
        client_factory=make_google_client_factory(app_ref),
        name="google-workspace",
        backend_name="Google Workspace",
        cache_listings=True,
    )
    main_server.mount(google_proxy, prefix="google_workspace")

    # Jira proxy (credentials already in subprocess .env)
    jira_proxy = FastMCPServer.as_proxy(
        f"http://localhost:{jira_mcp_port}/mcp",
        name="jira",
    )
    main_server.mount(jira_proxy, prefix="jira")

    logger.info("FastMCP proxy on http://127.0.0.1:%d/mcp", port)
    logger.info("  RAG -> %s", app_ref.voitta_rag_url)
    logger.info("  Google -> %s", app_ref.edit_proxy_url)
    logger.info("  Jira -> http://localhost:%d/mcp", jira_mcp_port)
    main_server.run(transport="streamable-http", host="127.0.0.1", port=port)
