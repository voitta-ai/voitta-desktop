"""The MCP server must survive a backend whose teardown fails, and say when it is broken.

On 2026-10-13 a Claude Code window's MCP session closed, the session's backend
connection answered the disconnect with "Timed out while waiting for response",
and from then on the endpoint answered every request with HTTP 500 until the
app was restarted. These tests rebuild that over real HTTP: a session calls a
backend tool, the session closes, the backend's teardown raises.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport, StreamableHttpTransport
from fastmcp.server.providers.proxy import StatefulProxyClient
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from mcpproxy import health as health_mod
from mcpproxy.health import HealthWatcher, McpHealth, McpServerHost
from mcpproxy.resilient import ResilientFastMCPProxy, VoittaStatefulProxyClient


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _teardown_fails(base):
    """A per-session backend client whose teardown raises, like the 2026-10-13 backend."""
    class TeardownFails(base):
        async def _session_runner(self):
            await super()._session_runner()
            raise McpError(ErrorData(code=-32001, message="Timed out while waiting for response."))
    return TeardownFails


def _build_with(client_cls, builds=None):
    def build(_app_ref, _port):
        upstream = FastMCP("upstream")

        @upstream.tool()
        def ping() -> str:
            return "pong"

        proxy = ResilientFastMCPProxy(
            client_factory=lambda: client_cls(FastMCPTransport(upstream)),
            name="b", backend_name="Flaky Backend", cache_listings=False, app_ref=None, prefix="b")
        main = FastMCP("voitta-desktop")
        main.mount(proxy, prefix="b")
        if builds is not None:
            builds.append(main)
        return main
    return build


async def _start(build) -> tuple[McpServerHost, asyncio.Task]:
    host = McpServerHost(None, _free_port(), build)
    task = asyncio.create_task(host.serve())
    for _ in range(200):
        if host._server is not None and host._server.started:
            return host, task
        await asyncio.sleep(0.02)
    raise AssertionError("MCP server did not start")


async def _stop(host, task):
    if host._server is not None:
        host._server.should_exit = True
    try:
        await asyncio.wait_for(task, 10)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


async def _use_and_close(url):
    """One window: open a session, call the backend, close the session."""
    async with Client(StreamableHttpTransport(url)) as c:
        result = await c.call_tool("b_ping", {})
        assert "pong" in str(result.content)
    await asyncio.sleep(0.5)  # the server unwinds the session after the client's DELETE


async def _initialize_status(url) -> int:
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=body,
                              headers={"accept": "application/json, text/event-stream"})
        return r.status_code


# ---- the crash and the fix -------------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_client_teardown_error_breaks_the_whole_server():
    """fastmcp's own StatefulProxyClient: reproduces the outage. If this ever
    passes with a 200, fastmcp fixed it and VoittaStatefulProxyClient's
    override may be dropped."""
    host, task = await _start(_build_with(_teardown_fails(StatefulProxyClient)))
    try:
        await _use_and_close(host.url)
        assert await _initialize_status(host.url) == 500
        assert await _initialize_status(host.url) == 500
    finally:
        await _stop(host, task)


@pytest.mark.asyncio
async def test_our_client_logs_the_teardown_error_and_the_server_keeps_working(caplog):
    caplog.set_level(logging.WARNING, logger="voitta-desktop.mcp")
    host, task = await _start(_build_with(_teardown_fails(VoittaStatefulProxyClient)))
    try:
        await _use_and_close(host.url)
        assert await _initialize_status(host.url) == 200
        await _use_and_close(host.url)  # a second window works end to end
        warnings = [r.getMessage() for r in caplog.records if "closing its connection failed" in r.getMessage()]
        assert warnings and warnings[0].startswith("[Flaky Backend] closing its connection failed (McpError: "
                                                   "Timed out while waiting for response.)")
        assert host.health.snapshot()["state"] == "healthy"
    finally:
        await _stop(host, task)


# ---- health bookkeeping -------------------------------------------------------------------

def test_health_states(caplog):
    caplog.set_level(logging.INFO, logger="voitta-desktop.mcp")
    now = [1000.0]
    h = McpHealth(clock=lambda: now[0])
    assert h.snapshot()["state"] == "starting"
    h.ok()
    assert h.snapshot()["state"] == "healthy"
    h.failed("HTTP 500 for POST /mcp")
    h.failed("HTTP 500 for POST /mcp")
    h.client_error()                       # a 4xx in between: the server answered
    h.failed("HTTP 500 for POST /mcp")
    h.failed("HTTP 500 for POST /mcp")
    assert h.snapshot()["state"] == "healthy"
    h.failed("RuntimeError: Task group is not initialized")
    snap = h.snapshot()
    assert snap["state"] == "broken" and snap["since"] == 1000.0 and snap["failures"] == 3
    assert snap["last_error"] == "RuntimeError: Task group is not initialized"
    h.failed("HTTP 500 for GET /mcp")
    assert h.snapshot()["failures"] == 4
    now[0] = 1300.0
    h.ok()
    snap = h.snapshot()
    assert snap["state"] == "healthy" and snap["since"] is None
    assert snap["recovered"] == {"at": 1300.0, "since": 1000.0, "failures": 4, "note": None}
    logs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("MCP server broken: 3 requests in a row failed; last error: RuntimeError")
               for m in logs)
    assert any(m.startswith("MCP server recovered") and "4 failed request(s)" in m for m in logs)


@pytest.mark.asyncio
async def test_watcher_only_observes():
    h = McpHealth()

    async def app(scope, receive, send):
        if scope["path"] == "/boom":
            raise RuntimeError("Task group is not initialized")
        status = {"/ok": 200, "/missing": 404, "/err": 500}[scope["path"]]
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"x"})

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    w = HealthWatcher(app, h)
    for path in ("/ok", "/missing", "/err", "/err"):
        await w({"type": "http", "method": "POST", "path": path}, receive, send)
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [200, 404, 500, 500]
    assert h.snapshot()["state"] == "healthy" and h.consecutive == 2
    with pytest.raises(RuntimeError, match="Task group is not initialized"):  # passed through unchanged
        await w({"type": "http", "method": "POST", "path": "/boom"}, receive, send)
    assert h.snapshot()["state"] == "broken"


# ---- self-check and rebuild -------------------------------------------------------------

@pytest.fixture
def fast_checks(monkeypatch):
    monkeypatch.setattr(health_mod, "TICK_S", 0.1)
    monkeypatch.setattr(health_mod, "CHECK_EVERY_S", 0.3)
    monkeypatch.setattr(health_mod, "RECHECK_AFTER_S", 0.1)


async def _wait_for(predicate, seconds=15.0):
    for _ in range(int(seconds / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_breakage_is_detected_rebuilt_once_and_then_left_red(fast_checks, caplog):
    caplog.set_level(logging.INFO, logger="voitta-desktop.mcp")
    builds = []
    host, task = await _start(_build_with(_teardown_fails(StatefulProxyClient), builds))
    try:
        # break it the 2026-10-13 way; the idle self-check finds it, the host rebuilds it
        await _use_and_close(host.url)
        await _wait_for(lambda: host.rebuilt_at is not None)
        await _wait_for(lambda: host.health.snapshot()["state"] == "healthy")
        assert len(builds) == 2
        recovered = host.health.snapshot()["recovered"]
        assert recovered and recovered["since"] is not None

        # broken again right away: no second rebuild, it stays red and says why
        await _use_and_close(host.url)
        await _wait_for(lambda: host.health.snapshot()["state"] == "broken")
        await _wait_for(lambda: host.health.snapshot()["note"] is not None)
        await asyncio.sleep(1.0)
        snap = host.health.snapshot()
        assert snap["state"] == "broken" and len(builds) == 2
        assert "broke again within 10 minutes" in snap["note"]
        logs = [r.getMessage() for r in caplog.records]
        assert any("rebuilding it on port" in m for m in logs)
        assert any(m.startswith("MCP server rebuilt and listening again") for m in logs)
        assert any("not rebuilding again" in m for m in logs)
    finally:
        await _stop(host, task)


@pytest.mark.asyncio
async def test_a_failed_rebuild_stays_red(fast_checks):
    builds = []
    good = _build_with(_teardown_fails(StatefulProxyClient), builds)

    def build(app_ref, port):
        if builds:
            raise RuntimeError("backend config unreadable")
        return good(app_ref, port)

    host, task = await _start(build)
    try:
        await _use_and_close(host.url)
        await asyncio.wait_for(task, 15)  # the host gives up instead of looping
        snap = host.health.snapshot()
        assert snap["state"] == "broken"
        assert "rebuilding it failed (RuntimeError: backend config unreadable)" in snap["note"]
    finally:
        if not task.done():
            await _stop(host, task)


# ---- logging --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sessions_are_logged_but_not_the_self_check(caplog):
    caplog.set_level(logging.INFO, logger="voitta-desktop.mcp")
    caplog.set_level(logging.INFO, logger="mcp.server.streamable_http_manager")
    host, task = await _start(_build_with(VoittaStatefulProxyClient))
    try:
        await host.probe()
        await host.probe()
        assert not [r for r in caplog.records if "MCP session" in r.getMessage()]
        await _use_and_close(host.url)
        logs = [r.getMessage() for r in caplog.records]
        opened = [m for m in logs if m.startswith("MCP session ") and " opened by " in m]
        closed = [m for m in logs if m.startswith("MCP session ") and m.endswith("closed by the client")]
        assert len(opened) == 1 and len(closed) == 1
        assert not [m for m in logs if m.startswith("Created new transport")]  # the library's per-session line
    finally:
        await _stop(host, task)


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://127.0.0.1:9/mcp")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


def test_login_rejection_finds_401_inside_wrappers():
    from mcpproxy.server import login_rejection
    wrapped = RuntimeError("tool failed")
    wrapped.__cause__ = ExceptionGroup("g", [ValueError("other"), _status_error(401)])
    assert login_rejection(wrapped) == "401 Unauthorized"
    assert login_rejection(_status_error(403)) == "403 Forbidden"
    assert login_rejection(RuntimeError("Auth error returned: invalid_token")) == "401 Unauthorized"
    assert login_rejection(_status_error(500)) is None
    assert login_rejection(McpError(ErrorData(code=-32001, message="Timed out"))) is None


@pytest.mark.asyncio
async def test_gate_warns_once_per_hour_per_backend_and_reraises(caplog):
    from types import SimpleNamespace
    from mcpproxy.server import ToolGateMiddleware
    caplog.set_level(logging.WARNING, logger="voitta-desktop.mcp")
    app = SimpleNamespace(mcp_servers=[{"name": "Google Workspace", "prefix": "google_workspace"},
                                       {"name": "Google", "prefix": "google"}])
    gate = ToolGateMiddleware(app)

    async def rejected(_ctx):
        raise _status_error(401)

    ctx = SimpleNamespace(message=SimpleNamespace(name="google_workspace_list_messages"))
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):     # the error still reaches the caller
            await gate.on_call_tool(ctx, rejected)
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert warnings[0].startswith("[Google Workspace] the backend rejected Voitta's credentials "
                                  "(401 Unauthorized) on google_workspace_list_messages")


def test_menu_text():
    from ui.mcp_health_badge import describe
    assert describe(None) == (None, None, False)
    healthy = {"state": "healthy", "since": None, "failures": 0, "last_error": None, "note": None,
               "recovered": None, "last_ok": 1.0}
    assert describe(healthy) == (None, None, False)
    broken = {**healthy, "state": "broken", "since": 1000.0, "failures": 57,
              "last_error": "RuntimeError: Task group is not initialized"}
    tip, line, is_broken = describe(broken)
    assert is_broken and "57 failed requests" in tip and "Task group is not initialized" in tip
    assert line.startswith("⚠ MCP server failing since") and line.endswith("rebuilding…")
    tip, line, _ = describe({**broken, "note": "Not rebuilt: it broke again. Restart Voitta Desktop."})
    assert line.endswith("restart Voitta Desktop") and "Not rebuilt" in tip
    recovered = {**healthy, "recovered": {"at": 2000.0, "since": 1000.0, "failures": 3, "note": None}}
    assert describe(recovered, now=2100.0)[1].startswith("MCP server failed ")
    assert describe(recovered, now=2000.0 + 25 * 3600) == (None, None, False)


def test_red_frame_shows_and_hides_on_a_real_button():
    from AppKit import NSButton
    from Foundation import NSMakeRect
    from ui.mcp_health_badge import style_status_button
    button = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 48, 22))
    style_status_button(button, False, None)
    assert list(button.subviews()) == []                     # healthy: nothing added
    style_status_button(button, True, "MCP server failing since 12:13")
    [frame] = list(button.subviews())
    assert not frame.isHidden() and frame.frame().size.width == 48
    assert button.toolTip() == "MCP server failing since 12:13"
    assert frame.hitTest_((5, 5)) is None                     # clicks still reach the menu
    style_status_button(button, False, None)
    assert frame.isHidden() and button.toolTip() is None and len(button.subviews()) == 1
