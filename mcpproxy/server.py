"""FastMCP proxy server setup — mounts every MCP backend from app_ref.mcp_servers."""

import asyncio
import base64
import json
import logging
import re
from urllib.parse import urlparse

import httpx

from fastmcp import FastMCP as FastMCPServer
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.types import Image

from optimizers.image import vt_object_store
from .resilient import ResilientFastMCPProxy

logger = logging.getLogger("voitta-desktop.mcp")


# ── Auth-factory builders, one per auth.type ─────────────────────────────────
#
# Every factory returns a thunk that builds a fresh ProxyClient on each
# upstream call. The thunk reads headers from the live app_ref state, so
# token refreshes and config edits propagate without rebuilding the proxy.

def _server_url(server: dict) -> str:
    """Return the HTTP endpoint for a server, regardless of kind. For
    subprocess servers, this is the locally-bound port we'll connect to."""
    if server.get("kind") == "subprocess":
        port = server.get("subprocess", {}).get("port", 0)
        return f"http://localhost:{port}/mcp"
    return (server.get("url") or "").strip()


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_loopback_https(url: str) -> bool:
    """True if `url` is HTTPS pointed at loopback. Loopback HTTPS endpoints
    typically use self-signed certs that fail standard chain validation; the
    cert isn't protecting against anything an attacker could reach on
    127.0.0.1, so we skip verification for these URLs.

    Matches localhost, 127.0.0.1, ::1 (with or without brackets). Strict on
    scheme — only ``https://`` qualifies; ``http://`` is unaffected.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _make_transport(url: str, headers: dict) -> StreamableHttpTransport:
    """Build a transport, skipping TLS verification for loopback HTTPS."""
    if _is_loopback_https(url):
        return StreamableHttpTransport(
            url=url, headers=headers,
            httpx_client_factory=lambda *a, **kw: httpx.AsyncClient(verify=False),
        )
    return StreamableHttpTransport(url=url, headers=headers)


def _make_static_headers_factory(url: str, headers: dict):
    """Static headers — used by none/bearer/api_key/basic/custom_headers."""
    def factory():
        return ProxyClient(_make_transport(url, dict(headers)))
    return factory


def _make_voitta_rag_legacy_factory(app_ref, url: str):
    """Multi-app X-Auth-Token-{Microsoft,Google} scheme. Builds the header
    set on each call from the live auth state of every rag-enabled app."""
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
        logger.debug("voitta_rag_legacy factory: url=%s, %d headers", url, len(headers))
        return ProxyClient(_make_transport(url, headers))
    return factory


def _make_oauth_app_factory(app_ref, url: str, backend: str, app_type: str):
    """Per-user OAuth Bearer token — looks up the active Auth-tab app for the
    given (backend, app_type) pair and uses its current access token. Used
    for Google Workspace today; generalises to any future OAuth backend."""
    def factory():
        headers = {}
        active_id = app_ref._active_app.get((backend, app_type))
        if active_id:
            state = app_ref._auth.get((active_id, backend), {})
            if state.get("token"):
                headers["Authorization"] = f"Bearer {state['token']}"
                profile = state.get("profile") or {}
                if profile.get("email"):
                    headers["X-Auth-Email"] = profile["email"]
                if profile.get("name"):
                    headers["X-Auth-Name"] = profile["name"]
        logger.debug("oauth_app factory: url=%s, backend=%s, headers=%s",
                     url, backend, list(headers.keys()))
        return ProxyClient(_make_transport(url, headers))
    return factory


def _build_factory(server: dict, app_ref):
    """Pick the right factory for a server entry based on auth.type."""
    url = _server_url(server)
    auth = server.get("auth", {}) or {}
    auth_type = auth.get("type", "none")

    if auth_type == "voitta_rag_legacy":
        return _make_voitta_rag_legacy_factory(app_ref, url)

    if auth_type == "oauth_app":
        backend = auth.get("backend", "google_workspace")
        app_type = auth.get("app_type", "google")
        return _make_oauth_app_factory(app_ref, url, backend, app_type)

    if auth_type == "bearer":
        token = (auth.get("token") or "").strip()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return _make_static_headers_factory(url, headers)

    if auth_type == "api_key":
        header = (auth.get("header") or "X-API-Key").strip() or "X-API-Key"
        value = (auth.get("value") or "").strip()
        headers = {header: value} if value else {}
        return _make_static_headers_factory(url, headers)

    if auth_type == "basic":
        u = auth.get("username") or ""
        p = auth.get("password") or ""
        if u or p:
            creds = base64.b64encode(f"{u}:{p}".encode()).decode()
            headers = {"Authorization": f"Basic {creds}"}
        else:
            headers = {}
        return _make_static_headers_factory(url, headers)

    if auth_type == "custom_headers":
        headers = {
            (h.get("name") or "").strip(): (h.get("value") or "")
            for h in (auth.get("headers") or [])
            if (h.get("name") or "").strip()
        }
        return _make_static_headers_factory(url, headers)

    # type == "none" or unknown → no auth headers
    return _make_static_headers_factory(url, {})


def build_instructions(app_ref, mcp_servers: list[dict]) -> str:
    """Build the LLM instructions block describing what's currently exposed.

    A backend appears iff it has ≥1 tool not in `app_ref.disabled_tools`
    right now — i.e. the prompt mirrors the tools array as filtered by the
    tool gate. Per-backend prose is the upstream `initialize.instructions`
    (when captured) appended with the locally-configured `description`
    (when set). Either may be empty; if both are empty the line is omitted.

    Called per new MCP session via the patched `create_initialization_options`
    on the FastMCP server, so each fresh client handshake reads current state.
    """
    disabled = set(getattr(app_ref, "disabled_tools", set()))
    per_prefix_tools: dict[str, list[str]] = getattr(app_ref, "_mcp_tools", {}) or {}
    upstream_map: dict[str, str] = getattr(app_ref, "_mcp_upstream_instructions", {}) or {}

    lines = [
        "You are connected through Voitta Desktop, a unified MCP proxy. "
        "All tool names are prefixed by backend:"
    ]
    for s in mcp_servers:
        prefix = (s.get("prefix") or "").strip()
        if not prefix:
            continue
        names = per_prefix_tools.get(prefix) or []
        if not names:
            continue
        if not any(n not in disabled for n in names):
            continue
        upstream = (upstream_map.get(prefix) or "").strip()
        local = (s.get("description") or "").strip()
        prose = " ".join(p for p in (upstream, local) if p).strip()
        if not prose:
            lines.append(f"  • {prefix}_*")
        else:
            lines.append(f"  • {prefix}_* — {prose}")
    return "\n".join(lines)


def tool_tree_groups(mcp_servers: list[dict]) -> list[tuple[str, str]]:
    """Return (prefix, label) pairs for the settings tool tree."""
    return [
        ((s.get("prefix") or "").strip(), s.get("name") or s.get("prefix") or "")
        for s in mcp_servers
        if (s.get("prefix") or "").strip()
    ]


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

        # Codex fast path — signs every handshake and reconnects often, so
        # an interactive popup is friction for no security gain. When
        # suppression is on (default), filter using the saved disabled_tools
        # silently and skip the popup entirely. Match on the clientInfo.name
        # prefix Codex sends ("codex-mcp-client", "codex", etc.).
        if (getattr(self._app_ref, "suppress_codex_popup", True)
                and isinstance(client_name, str)
                and client_name.lower().startswith("codex")):
            tools = await call_next(context)
            disabled = set(getattr(self._app_ref, "disabled_tools", set()))
            filtered = [t for t in tools if t.name not in disabled]
            logger.info("tool_gate: codex silent pass (%d/%d tools, client=%s)",
                        len(filtered), len(tools), client_name)
            return filtered

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
                # Cancel = no tools allowed. Disable ALL of them so any
                # reuse-window calls also block. (An empty set would mean
                # "filter nothing" and leak every tool back to the LLM.)
                self._last_disabled = {t.name for t in tools}
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


def run_mcp_proxy(app_ref, port: int):
    """Run unified FastMCP proxy server mounting every entry in
    ``app_ref.mcp_servers``. Blocks forever."""
    logger.info("run_mcp_proxy starting (port=%d)", port)
    gate = ToolGateMiddleware(app_ref)
    app_ref._tool_gate = gate

    mcp_servers = list(app_ref.mcp_servers)
    main_server = FastMCPServer(
        "voitta-desktop",
        instructions="",  # refreshed per-session below
        middleware=[gate],
    )
    # Disable tools/list_changed notifications — they cause Claude Code
    # to re-fetch tools on every backend mount and settings poll,
    # triggering unwanted tool gate popups.
    main_server._mcp_server.notification_options.tools_changed = False

    # Mount every server. Each entry produces one ResilientFastMCPProxy with
    # a factory derived from its auth.type. Empty-prefix entries are skipped
    # with a warning — the prefix is the tool-name namespace and required.
    proxies: list[tuple[str, str, ResilientFastMCPProxy]] = []
    for server in mcp_servers:
        prefix = (server.get("prefix") or "").strip()
        if not prefix:
            logger.warning("mcp_server skipped: empty prefix (name=%r)", server.get("name"))
            continue
        url = _server_url(server)
        if not url:
            logger.warning("mcp_server %r skipped: no URL", server.get("name"))
            continue
        factory = _build_factory(server, app_ref)
        proxy = ResilientFastMCPProxy(
            client_factory=factory,
            name=prefix.replace("_", "-"),
            backend_name=server.get("name") or prefix,
            cache_listings=True,
            app_ref=app_ref, prefix=prefix,
        )
        main_server.mount(proxy, prefix=prefix)
        proxies.append((server.get("name") or prefix, url, proxy))

    # Expose proxies for the menu's "Refresh LLM Tools" popup. URLs are
    # display-only — if the user edits a URL via Settings later, they need
    # to restart for the running proxy to reflect it.
    app_ref._mcp_backends = proxies

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

    # ── Dynamic instructions ────────────────────────────────────────
    #
    # `create_initialization_options()` is called once per new MCP session
    # (see mcp.server.streamable_http_manager). We refresh `instructions`
    # right before each call, so every fresh client handshake — including
    # the one Claude Code does after `/mcp` toggles a server — reads
    # current state: only backends with ≥1 currently-enabled tool, upstream
    # description appended with the locally configured one.
    #
    # On the first init we also kick off a one-shot background fetch of
    # each backend's upstream `initialize.instructions` so subsequent prompt
    # rebuilds can include them. Failures are tolerated and just leave the
    # local description as the sole prose.
    low = main_server._mcp_server
    _orig_init_opts = low.create_initialization_options
    _primed = {"done": False}

    async def _prime_upstream_instructions():
        for _name, _url, proxy in proxies:
            try:
                await proxy.fetch_upstream_instructions()
            except Exception as e:
                logger.debug("upstream instructions prime failed for %s: %s", _name, e)

    def _patched_init_opts(*args, **kwargs):
        if not _primed["done"]:
            _primed["done"] = True
            try:
                asyncio.get_event_loop().create_task(_prime_upstream_instructions())
            except Exception as e:
                logger.debug("could not schedule upstream-instructions prime: %s", e)
        try:
            low.instructions = build_instructions(app_ref, list(app_ref.mcp_servers))
        except Exception as e:
            logger.warning("build_instructions failed: %s", e)
            low.instructions = ""
        return _orig_init_opts(*args, **kwargs)

    low.create_initialization_options = _patched_init_opts

    # ── Logging ─────────────────────────────────────────────────────

    logger.info("FastMCP proxy on http://127.0.0.1:%d/mcp", port)
    for name, url, _ in proxies:
        logger.info("  %s -> %s", name, url)
    main_server.run(transport="streamable-http", host="127.0.0.1", port=port)
