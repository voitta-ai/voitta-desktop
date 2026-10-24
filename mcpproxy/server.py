"""FastMCP proxy server setup — mounts every MCP backend from app_ref.mcp_servers."""

import asyncio
import base64
import contextvars
import json
import logging
import os
import re
import shutil
import time
import weakref
from urllib.parse import urlparse

import httpx
import mcp.types

from fastmcp import FastMCP as FastMCPServer
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.client.transports import StreamableHttpTransport, StdioTransport
from fastmcp.utilities.types import Image

from optimizers.image import vt_object_store
from .resilient import ResilientFastMCPProxy, VoittaStatefulProxyClient

logger = logging.getLogger("voitta-desktop.mcp")


# ── Auth-factory builders, one per auth.type ─────────────────────────────────
#
# HTTP factories return a thunk that builds a StatefulProxyClient (our
# VoittaStatefulProxyClient, whose teardown can't take the server down). fastmcp
# keys one upstream connection per *server session* off that client
# (StatefulProxyClient.new_stateful) and tears it down on the session's own
# exit stack — so a Claude Code window pays DNS + TLS + MCP initialize once,
# not on every tool call. For a remote backend on an 80–140 ms WAN that was
# 3–7 s per call; it is now the wire time.
#
# Headers are read when the client is BUILT, which is once per session. A
# token that changes mid-session therefore reaches new sessions, not live
# ones — the same restart-to-apply contract Settings already states for URL
# edits. There is deliberately no header-diffing or silent reconnect here:
# a dead upstream surfaces as the real error and the library reconnects on
# the session's next call.
#
# Stdio factories (npx / command) create the transport ONCE at setup time and
# close over it. Creating a new NpxStdioTransport per call would spawn a fresh
# npx process on every tool listing and tool call — a process-per-call leak.
# They stay on plain ProxyClient: the transport already keeps the process
# alive, and a per-session subprocess would be the wrong lifetime.

def _server_url(server: dict) -> str:
    """Return the HTTP endpoint for an http-kind server.
    Returns "" for stdio subprocess servers (npx/command templates)."""
    kind = server.get("kind", "http")
    if kind == "subprocess":
        template = (server.get("subprocess") or {}).get("template", "")
        if template in ("npx", "command"):
            return ""
        port = (server.get("subprocess") or {}).get("port", 0)
        return f"http://localhost:{port}/mcp"
    return (server.get("url") or "").strip()


def _is_stdio_server(server: dict) -> bool:
    """True for subprocess servers managed via stdio (npx / command templates)."""
    if server.get("kind") != "subprocess":
        return False
    template = (server.get("subprocess") or {}).get("template", "")
    return template in ("npx", "command")


def _stdio_display_url(server: dict) -> str:
    """Human-readable identifier for a stdio server (used in logs/UI only)."""
    sp = server.get("subprocess") or {}
    template = sp.get("template", "")
    if template == "npx":
        return f"stdio:npx {sp.get('package', '?')}"
    if template == "command":
        cmd = sp.get("command") or []
        return f"stdio:{' '.join(str(c) for c in cmd[:3])}"
    return "stdio:?"


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
        return VoittaStatefulProxyClient(_make_transport(url, dict(headers)))
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
        return VoittaStatefulProxyClient(_make_transport(url, headers))
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
        return VoittaStatefulProxyClient(_make_transport(url, headers))
    return factory


_NODE_SEARCH_PATHS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.nvm/versions/node/*/bin"),  # nvm installs
    "/usr/local/opt/node/bin",                          # homebrew keg-only
]


def _resolve_npx() -> tuple[str, dict[str, str]]:
    """Find the npx binary, searching well-known Node install locations in
    addition to the current PATH. Returns (npx_path, augmented_env).

    Raises ValueError if npx cannot be found anywhere.
    """
    import glob

    extra_dirs: list[str] = []
    for pattern in _NODE_SEARCH_PATHS:
        if "*" in pattern:
            extra_dirs.extend(glob.glob(pattern))
        else:
            extra_dirs.append(pattern)

    # Build a PATH that includes the extra dirs before the system PATH so
    # Homebrew / nvm installs take precedence over a bare /usr/bin stub.
    augmented_path = ":".join(extra_dirs + [os.environ.get("PATH", "")])
    augmented_env = {**os.environ, "PATH": augmented_path}

    # Prefer the version already in PATH; fall back to the extra dirs.
    npx = shutil.which("npx") or shutil.which("npx", path=augmented_path)
    if not npx:
        raise ValueError(
            f"Command 'npx' not found. Install Node.js (e.g. 'brew install node') "
            f"and restart Voitta Desktop."
        )
    return npx, augmented_env


def _make_npx_stdio_factory(package: str, args: list):
    """Stdio factory for npx-based MCP servers (e.g. chrome-devtools-mcp).

    Resolves npx at factory-creation time so startup errors are caught early
    and logged against the server name. Transport is created once — reusing
    it avoids spawning a new process per tool call.
    """
    npx, env = _resolve_npx()
    # Build the npx argument list the same way NpxStdioTransport does, but
    # use StdioTransport directly so we can supply the resolved path + env.
    npx_args = ["--prefer-offline", package] + list(args or [])
    transport = StdioTransport(command=npx, args=npx_args, env=env, keep_alive=True)
    def factory():
        return ProxyClient(transport)
    return factory


def _make_command_stdio_factory(command: list):
    """Stdio factory for arbitrary command-based MCP servers.
    Transport is created once to avoid spawning a new process per call."""
    if not command:
        raise ValueError("command list is empty")
    # Augment PATH with Node/Homebrew dirs so node-based commands resolve.
    import glob
    extra_dirs = [d for p in _NODE_SEARCH_PATHS for d in (glob.glob(p) if "*" in p else [p])]
    env = {**os.environ, "PATH": ":".join(extra_dirs + [os.environ.get("PATH", "")])}
    transport = StdioTransport(command=command[0], args=list(command[1:]), env=env, keep_alive=True)
    def factory():
        return ProxyClient(transport)
    return factory


def _build_factory(server: dict, app_ref):
    """Pick the right factory for a server entry based on kind/template/auth."""
    # Stdio subprocess servers bypass all HTTP auth logic.
    if _is_stdio_server(server):
        sp = server.get("subprocess") or {}
        template = sp.get("template", "")
        if template == "npx":
            package = (sp.get("package") or "").strip()
            if not package:
                raise ValueError(f"mcp_server {server.get('name')!r}: npx template requires 'package'")
            args = sp.get("args") or []
            return _make_npx_stdio_factory(package, args)
        if template == "command":
            command = sp.get("command") or []
            if not command:
                raise ValueError(f"mcp_server {server.get('name')!r}: command template requires 'command' list")
            return _make_command_stdio_factory(command)

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

    Follows the Claude Code convention: one markdown section per connected
    backend, containing the upstream server's own initialize.instructions
    followed by the locally-configured description. Tool names are NOT
    listed here — the tools array carries that detail; the model maps
    prefix_* names to sections by the shared prefix.

    A backend is included iff it has ≥1 tool not in disabled_tools right now,
    so the prompt stays in sync with the tools array after /mcp toggles.
    """
    disabled = set(getattr(app_ref, "disabled_tools", set()))
    per_prefix_tools: dict[str, list[str]] = getattr(app_ref, "_mcp_tools", {}) or {}
    upstream_map: dict[str, str] = getattr(app_ref, "_mcp_upstream_instructions", {}) or {}

    blocks = []
    for s in mcp_servers:
        prefix = (s.get("prefix") or "").strip()
        if not prefix:
            continue
        names = per_prefix_tools.get(prefix) or []
        if not names:
            continue
        if not any(n not in disabled for n in names):
            continue
        name = (s.get("name") or prefix).strip()
        upstream = (upstream_map.get(prefix) or "").strip()
        local = (s.get("description") or "").strip()
        body_parts = [p for p in (upstream, local) if p]
        body = "\n".join(body_parts) if body_parts else f"Tool prefix: {prefix}_*"
        blocks.append(f"## {name}\n{body}")

    if not blocks:
        return "You are connected through Voitta Desktop, a unified MCP proxy. No backends are currently active."

    sections = "\n\n".join(blocks)
    return (
        "You are connected through Voitta Desktop, a unified MCP proxy. "
        "All tool names are prefixed by backend name (e.g. vim_search, freecad_create_document). "
        "The following MCP servers are currently active:\n\n"
        f"{sections}"
    )


def tool_tree_groups(mcp_servers: list[dict]) -> list[tuple[str, str]]:
    """Return (prefix, label) pairs for the settings tool tree."""
    return [
        ((s.get("prefix") or "").strip(), s.get("name") or s.get("prefix") or "")
        for s in mcp_servers
        if (s.get("prefix") or "").strip()
    ]


from fastmcp.server.middleware import Middleware as FastMCPMiddleware


def login_rejection(exc: BaseException) -> str | None:
    """"401 Unauthorized" (or 403) if ``exc``, or anything it wraps, is a
    backend refusing our credentials; None otherwise."""
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
            return f"{e.response.status_code} {e.response.reason_phrase}"
        text = str(e)
        if "401 Unauthorized" in text or "invalid_token" in text:
            return "401 Unauthorized"
        stack.extend(getattr(e, "exceptions", None) or ())
        stack.extend((e.__cause__, e.__context__))
    return None


# Per-window tools (the voitta-llm mod's /mcp hook). Claude Code runs the MCP
# entry's headersHelper (llmgw: claude-window-header) on every connection; it
# names the Claude Code process the connection belongs to as "<pid>@<start>".
# The mod computes the same id for its own window, so /mcp can give exactly
# that window its own tool list.
WINDOW_HEADER = "x-voitta-claude-window"
_WINDOW_ID = re.compile(r"\d{1,9}@[A-Za-z0-9_:]{1,64}")

# Set only while the gate itself sends a tool-list-changed notice to one window.
_OWN_TOOLS_NOTICE: contextvars.ContextVar[bool] = contextvars.ContextVar("voitta_own_tools_notice", default=False)


def _guard_tool_list_notices(session) -> None:
    """Let a session carry a tool-list-changed notice only when the gate sends
    it to that one window. Anything else that tries (a library update that
    starts broadcasting on backend mounts, say) is dropped and logged; that
    broadcast is what made every window re-list and re-prompt in April
    (696c7b7)."""
    if getattr(session, "_voitta_notice_guard", False):
        return
    original = session.send_notification

    async def send_notification(notification, related_request_id=None):
        root = getattr(notification, "root", notification)
        if isinstance(root, mcp.types.ToolListChangedNotification) and not _OWN_TOOLS_NOTICE.get():
            logger.warning("tool_gate: dropped a tool-list-changed notice Voitta did not send itself")
            return None
        return await original(notification, related_request_id)

    session.send_notification = send_notification
    session._voitta_notice_guard = True


def _window_from_request() -> str | None:
    try:
        from fastmcp.server.dependencies import get_http_request
        value = get_http_request().headers.get(WINDOW_HEADER) or ""
    except Exception:
        return None
    value = value.strip()
    return value if _WINDOW_ID.fullmatch(value) else None


class ToolGateMiddleware(FastMCPMiddleware):
    """Shows a tool gate popup on external tools/list requests.

    - First listing of an MCP session: show the popup, remember the answer
    - Later listings in that SAME session: reuse it, no popup
    - A different session prompts again
    - rearm() forgets everything, so the next listing prompts

    Answers are remembered **per session**, which keeps two requirements
    apart that a single global answer used to conflate:

    * *Retry coalescing.* An MCP client abandons a tools/list after about
      five seconds — far less than a human takes to read a tool tree — then
      retries. The popup deliberately outlives that cancellation and
      publishes its answer here (see ui/tool_gate.py), so the retry has to
      find it. That needs a window of minutes.
    * *Fresh sessions prompt.* Gating is per client session; a new session
      must get its own popup rather than inheriting an older answer.

    A global answer with a minutes-long window satisfies the first and
    breaks the second — every new session silently inherited whatever the
    previous one chose. Keying by session satisfies both, so the window can
    be generous without suppressing anyone's prompt.
    """

    # Generous: the session key already isolates callers, so this only bounds
    # memory and lets a very long-lived session eventually be re-gated.
    ANSWER_TTL_S = 3600.0
    MAX_REMEMBERED = 64

    # Sessions that report no id share one slot. That still coalesces a
    # client's own retries, which is what the slot is for.
    ANON_KEY = "<no-session>"

    # A backend that rejects our credentials (an expired Google login, say)
    # fails every tool call; say so once an hour per backend.
    AUTH_WARN_WINDOW_S = 3600.0

    def __init__(self, app_ref):
        super().__init__()
        self._app_ref = app_ref
        # session key -> (disabled tool names, answered at)
        self._answers: dict[str, tuple[set[str], float]] = {}
        self._lock: asyncio.Lock | None = None
        self._auth_warned: dict[str, float] = {}
        # Per-window tools: which Claude Code window each MCP session belongs
        # to (only sessions that may be refreshed: header present, client
        # Claude Code, its popup suppressed), in memory only.
        self._session_window: "weakref.WeakKeyDictionary[object, str]" = weakref.WeakKeyDictionary()
        self._window_sessions: dict[str, "weakref.WeakSet[object]"] = {}

    # ---- per-window tools (the voitta-llm mod's /mcp) -------------------------

    @property
    def _window_choices(self) -> dict[str, set[str]]:
        """window -> disabled tool names, chosen with /mcp. Kept on the app so a
        rebuilt MCP server (health.py) doesn't drop them; never written to disk."""
        choices = getattr(self._app_ref, "_window_tool_choices", None)
        if choices is None:
            choices = {}
            try:
                self._app_ref._window_tool_choices = choices
            except Exception:
                pass
        return choices

    async def on_initialize(self, context, call_next):
        try:
            session = context.fastmcp_context.session
            _guard_tool_list_notices(session)
            params = getattr(context.message, "params", None)
            client = (params.clientInfo.name if params and params.clientInfo else "") or ""
            window = _window_from_request()
            if window and self._silent_client(client) == "claude":
                tools_cap = session._init_options.capabilities.tools
                if tools_cap is not None:
                    # Only this connection hears that its tool list can change.
                    tools_cap.listChanged = True
                self._session_window[session] = window
                self._window_sessions.setdefault(window, weakref.WeakSet()).add(session)
                logger.info("tool_gate: Claude Code window %s connected (its tools can be set with /mcp)",
                            window.split("@")[0])
        except Exception:
            logger.debug("tool_gate: on_initialize bookkeeping failed", exc_info=True)
        return await call_next(context)

    def window_status(self, window: str) -> str | None:
        """Why /mcp can't show this window's popup, or None if it can."""
        if getattr(self._app_ref, "terminal_mode", False) or not getattr(self._app_ref, "suppress_claude_popup", False):
            return "not_needed"
        if not _WINDOW_ID.fullmatch(window or "") or not list(self._window_sessions.get(window, ())):
            return "not_linked"
        from ui.tool_gate import gate_is_open
        if gate_is_open():
            return "busy"
        return None

    async def window_popup(self, window: str) -> dict:
        """Show the tool popup for one Claude Code window and apply the answer
        to that window only: remembered in memory, then a tool-list-changed
        notice to its connection(s) so it re-lists right away."""
        from ui.tool_gate import show_tool_gate
        saved = set(getattr(self._app_ref, "disabled_tools", set()))
        current = self._window_choices.get(window, saved)
        meta = {"For": "this Claude Code window only", "Kept": "until the window closes or Voitta restarts"}
        result = await show_tool_gate(self._app_ref._build_tool_tree(), current, meta,
                                      title="Tools for this Claude Code window",
                                      heading="Tools for this Claude Code window")
        if result is None:
            logger.info("tool_gate: window %s kept its tools (popup cancelled)", window.split("@")[0])
            return {"status": "unchanged"}
        self._window_choices[window] = set(result)
        refreshed = 0
        for session in list(self._window_sessions.get(window, ())):
            token = _OWN_TOOLS_NOTICE.set(True)
            try:
                await session.send_tool_list_changed()
                refreshed += 1
            except Exception as e:
                logger.info("tool_gate: window %s: a closed connection skipped (%s)", window.split("@")[0], e)
            finally:
                _OWN_TOOLS_NOTICE.reset(token)
        logger.info("tool_gate: window %s has its own tools now (%d disabled); %d connection(s) told to re-list",
                    window.split("@")[0], len(result), refreshed)
        return {"status": "applied", "disabled": len(result), "refreshed": refreshed}

    async def on_call_tool(self, context, call_next):
        try:
            return await call_next(context)
        except Exception as e:
            self._warn_if_login_rejected(getattr(context.message, "name", "") or "", e)
            raise

    def _warn_if_login_rejected(self, tool: str, exc: BaseException):
        why = login_rejection(exc)
        if not why:
            return
        prefixes = [(s.get("prefix") or "").strip() for s in getattr(self._app_ref, "mcp_servers", None) or []]
        prefix = max((p for p in prefixes if p and tool.startswith(p + "_")), key=len, default="")
        server = next((s for s in self._app_ref.mcp_servers if (s.get("prefix") or "").strip() == prefix), {}) \
            if prefix else {}
        backend = server.get("name") or prefix or "?"
        now = time.monotonic()
        if now - self._auth_warned.get(backend, -self.AUTH_WARN_WINDOW_S) < self.AUTH_WARN_WINDOW_S:
            return
        self._auth_warned[backend] = now
        logger.warning("[%s] the backend rejected Voitta's credentials (%s) on %s; its tools fail until it is "
                       "signed in again (Settings → Accounts). Repeats are not logged for an hour.",
                       backend, why, tool or "a tool call")

    def rearm(self) -> None:
        """Forget every remembered answer so the next listing re-prompts."""
        count = len(self._answers)
        self._answers.clear()
        logger.info("tool_gate: re-armed, dropped %d remembered answer(s); "
                    "the next listing will prompt", count)

    def _recall(self, key: str) -> tuple[set[str], float] | None:
        """Return this session's answer, or None if absent or stale."""
        entry = self._answers.get(key)
        if entry is None:
            return None
        disabled, answered_at = entry
        if time.time() - answered_at > self.ANSWER_TTL_S:
            del self._answers[key]
            return None
        return disabled, answered_at

    def _remember(self, key: str, disabled: set[str]) -> None:
        self._answers[key] = (disabled, time.time())
        if len(self._answers) > self.MAX_REMEMBERED:
            oldest = min(self._answers, key=lambda k: self._answers[k][1])
            del self._answers[oldest]

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            import asyncio
            self._lock = asyncio.Lock()
        return self._lock

    def _silent_client(self, client_name) -> str | None:
        """"codex" or "claude" when this client's popup is suppressed."""
        if not isinstance(client_name, str):
            return None
        name = client_name.lower()
        if name.startswith("codex") and getattr(self._app_ref, "suppress_codex_popup", True):
            return "codex"
        if name.startswith("claude") and getattr(self._app_ref, "suppress_claude_popup", False):
            return "claude"
        return None

    async def on_list_tools(self, context, call_next):
        # Terminal mode: no interactive popup — apply stored disabled_tools
        # silently, identical to the Codex fast path.
        if getattr(self._app_ref, "terminal_mode", False):
            tools = await call_next(context)
            disabled = set(getattr(self._app_ref, "disabled_tools", set()))
            return [t for t in tools if t.name not in disabled]

        client_name, session_id = self._get_client_info(context)

        # Skip internal calls
        if client_name is None or client_name == "settings":
            logger.warning("tool_gate: pass-through (%s)", client_name or "no session")
            return await call_next(context)

        # Silent pass: apply the saved disabled_tools and skip the popup for
        # clients the user opted out of being asked about — Codex (on by
        # default: it signs every handshake and reconnects often, so a popup
        # is friction for no security gain) and Claude Code (off by default).
        # Matched on the clientInfo.name prefix ("codex-mcp-client",
        # "claude-code", ...).
        silent = self._silent_client(client_name)
        if silent:
            tools = await call_next(context)
            session = self._session_of(context) if silent == "claude" else None
            window = self._session_window.get(session) if session is not None else None
            choice = self._window_choices.get(window) if window else None
            disabled = choice if choice is not None else set(getattr(self._app_ref, "disabled_tools", set()))
            filtered = [t for t in tools if t.name not in disabled]
            logger.info("tool_gate: %s silent pass (%d/%d tools, client=%s%s)",
                        silent, len(filtered), len(tools), client_name,
                        f", window {window.split('@')[0]}'s own tools" if choice is not None else "")
            return filtered

        # Serialize popup access — second request waits for first popup to finish
        async with self._get_lock():
            key = session_id or self.ANON_KEY
            remembered = self._recall(key)
            if remembered is not None:
                disabled, answered_at = remembered
                logger.info("tool_gate: reusing this session's answer "
                            "(client=%s, session=%s, %.1fs ago)",
                            client_name, key, time.time() - answered_at)
                tools = await call_next(context)
                return [t for t in tools if t.name not in disabled]

            return await self._show_gate(context, call_next, client_name, session_id)

    @staticmethod
    def _session_of(context):
        try:
            return context.fastmcp_context.session
        except Exception:
            return None

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

            all_names = {t.name for t in tools}

            key = session_id or self.ANON_KEY

            def _publish(result: list[str] | None) -> None:
                """Remember the user's answer for this session.

                Called from the AppKit main thread, usually *after* this
                request has been cancelled — the client's timeout is far
                shorter than a human's reading time. Remembering it here is
                what lets the retry be served without a second popup.

                Cancel means no tools allowed, so every name goes on the
                disabled list. An empty set would mean "filter nothing" and
                leak the whole toolset back to the LLM.
                """
                self._remember(key, all_names if result is None else set(result))

            gate_result = await show_tool_gate(
                tool_groups, disabled, meta, on_result=_publish
            )

            # _publish already ran on the way here — it is the single writer.
            if gate_result is None:
                return []
            remembered = self._recall(key)
            disabled_now = remembered[0] if remembered else set(gate_result)
            return [t for t in tools if t.name not in disabled_now]
        except asyncio.CancelledError:
            # Must re-raise, not return []. The MCP SDK already sent an error
            # response when the client cancelled; returning a value here makes
            # it respond a second time and assert. See ui/tool_gate.py.
            logger.warning("tool_gate: client cancelled while the popup was open")
            raise
        except Exception as e:
            logger.error("tool_gate: popup error: %s", e, exc_info=True)
            raise


def build_mcp_proxy(app_ref, port: int):
    """Assemble the unified FastMCP proxy mounting every entry in
    ``app_ref.mcp_servers``. Returns the server; does not start it."""
    logger.info("building MCP proxy (port=%d)", port)
    gate = ToolGateMiddleware(app_ref)
    app_ref._tool_gate = gate

    mcp_servers = list(app_ref.mcp_servers)

    # Terminal mode: subprocess-kind servers require OAuth/local processes
    # that aren't supported outside macOS. Filter them out silently.
    if getattr(app_ref, "terminal_mode", False):
        skipped = [s for s in mcp_servers if s.get("kind") == "subprocess"]
        if skipped:
            logger.info(
                "terminal mode: skipping %d subprocess server(s): %s",
                len(skipped),
                ", ".join(s.get("name", s.get("prefix", "?")) for s in skipped),
            )
        mcp_servers = [s for s in mcp_servers if s.get("kind") != "subprocess"]
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
        if not url and not _is_stdio_server(server):
            logger.warning("mcp_server %r skipped: no URL", server.get("name"))
            continue
        try:
            factory = _build_factory(server, app_ref)
        except ValueError as exc:
            logger.warning("mcp_server %r skipped: %s", server.get("name"), exc)
            continue
        proxy = ResilientFastMCPProxy(
            client_factory=factory,
            name=prefix.replace("_", "-"),
            backend_name=server.get("name") or prefix,
            cache_listings=True,
            app_ref=app_ref, prefix=prefix,
        )
        main_server.mount(proxy, prefix=prefix)
        display_url = url or _stdio_display_url(server)
        proxies.append((server.get("name") or prefix, display_url, proxy))

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

        if obj["type"] in ("bash", "tool_result", "tool_use", "tool_pair"):
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
                # Called from inside a request handler, so a running loop is
                # guaranteed; get_event_loop() here would be deprecated.
                asyncio.get_running_loop().create_task(_prime_upstream_instructions())
            except RuntimeError as e:
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
    return main_server


async def serve_mcp_proxy(app_ref, port: int) -> None:
    """Build the proxy and serve it on the caller's event loop.

    ``FastMCP.run()`` creates and owns an event loop, which is why this used
    to need a thread of its own. The host awaits on whatever loop is already
    running, so the MCP proxy shares the one in runtime.py with everything
    else. It is fastmcp's HTTP app under our own uvicorn server, watched and
    rebuilt in place if it breaks (see health.py); the menu bar reads
    ``app_ref._mcp_health``.
    """
    from .health import McpServerHost
    host = McpServerHost(app_ref, port, build_mcp_proxy)
    app_ref._mcp_host = host
    app_ref._mcp_health = host.health
    await host.serve()


def run_mcp_proxy(app_ref, port: int) -> None:
    """Blocking entry point, kept for the terminal driver and for tests."""
    asyncio.run(serve_mcp_proxy(app_ref, port))
