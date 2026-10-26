"""/mcp per-window tools: one Claude Code window gets its own tool list.

Over real HTTP, with the real ToolGateMiddleware; only the AppKit popup is
replaced by a scripted answer. The rules under test:

* A connection hears "tool list can change" (listChanged) only if it is Claude
  Code, names its window (X-Voitta-Claude-Window) and Claude Code's popup is
  silenced. Everything else gets exactly what it got before.
* A window's choice applies to that window's connections only; the notice goes
  to that window only; nothing else can send the notice at all.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import socket

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import Implementation

import ui.tool_gate as tool_gate_ui
from llmgw import window_header
from mcpproxy.health import McpServerHost
from mcpproxy.server import ToolGateMiddleware, _OWN_TOOLS_NOTICE

WIN_A = "4242@Fri_Oct_23_22:26:07_2026"
WIN_B = "4343@Fri_Oct_23_22:27:00_2026"


class FakeApp:
    terminal_mode = False
    suppress_codex_popup = True
    suppress_claude_popup = True

    def __init__(self):
        self.disabled_tools: set[str] = set()

    def _build_tool_tree(self):
        return [{"name": "b", "tools": [{"name": "ping"}, {"name": "pong"}]}]


class Notices(MessageHandler):
    def __init__(self):
        super().__init__()
        self.count = 0

    async def on_tool_list_changed(self, message):
        self.count += 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(monkeypatch):
    app = FakeApp()
    answers: list = []   # what the scripted popup answers, in turn
    shown: list = []     # what it was shown

    async def fake_popup(tool_groups, disabled, meta=None, on_result=None, title="", heading=""):
        shown.append({"disabled": set(disabled), "title": title})
        return answers.pop(0)

    monkeypatch.setattr(tool_gate_ui, "show_tool_gate", fake_popup)
    monkeypatch.setattr(tool_gate_ui, "gate_is_open", lambda: False)

    def build(_app, _port):
        gate = ToolGateMiddleware(app)
        app._tool_gate = gate
        main = FastMCP("voitta", middleware=[gate])
        main._mcp_server.notification_options.tools_changed = False  # as build_mcp_proxy does

        @main.tool()
        def ping() -> str:
            return "pong"

        @main.tool()
        def pong() -> str:
            return "ping"
        return main

    host = McpServerHost(app, _free_port(), build)
    task = asyncio.create_task(host.serve())
    while not (host._server and host._server.started):
        await asyncio.sleep(0.02)
    yield app, host, answers, shown
    host._server.should_exit = True
    await asyncio.wait_for(task, 10)


def client(host, *, window=None, name="claude-code", notices=None):
    headers = {"X-Voitta-Claude-Window": window} if window else {}
    return Client(StreamableHttpTransport(host.url, headers=headers), message_handler=notices,
                  client_info=Implementation(name=name, version="2.1.310"))


async def tool_names(c) -> set[str]:
    return {t.name for t in await c.list_tools()}


async def settle():
    await asyncio.sleep(0.3)


# ---- who hears "tool list can change" ---------------------------------------------

async def test_only_claude_with_a_window_and_silenced_popup_hears_list_changes(server):
    app, host, _, _ = server

    async def caps(**kw):
        async with client(host, **kw) as c:
            return c.initialize_result.capabilities.tools.listChanged

    assert await caps(window=WIN_A) is True
    assert await caps() is False                                  # no header: as before
    assert await caps(window=WIN_A, name="codex-mcp-client") is False
    app.suppress_claude_popup = False
    assert await caps(window=WIN_A) is False                      # popup in use: as before


# ---- one window's choice ----------------------------------------------------------

async def test_a_windows_choice_applies_to_that_window_only(server):
    app, host, answers, shown = server
    a_notices, b_notices = Notices(), Notices()
    async with client(host, window=WIN_A, notices=a_notices) as a, \
            client(host, window=WIN_B, notices=b_notices) as b:
        assert await tool_names(a) == await tool_names(b) == {"ping", "pong"}

        gate = app._tool_gate
        assert gate.window_status(WIN_A) is None
        answers.append(["ping"])                       # disable ping for window A
        result = await gate.window_popup(WIN_A)
        assert result == {"status": "applied", "disabled": 1, "refreshed": 1}
        assert shown[-1]["title"] == "Tools for this Claude Code window"
        await settle()

        assert a_notices.count == 1 and b_notices.count == 0
        assert await tool_names(a) == {"pong"}
        assert await tool_names(b) == {"ping", "pong"}  # B keeps the saved selection

        # the popup opens on A's current choice; Cancel keeps it and sends nothing
        answers.append(None)
        assert await gate.window_popup(WIN_A) == {"status": "unchanged"}
        assert shown[-1]["disabled"] == {"ping"}
        await settle()
        assert a_notices.count == 1 and await tool_names(a) == {"pong"}

    # a new connection from window A (a reconnect) gets A's choice too
    async with client(host, window=WIN_A) as a2:
        assert await tool_names(a2) == {"pong"}
    assert app.disabled_tools == set()                  # the saved selection is untouched


async def test_window_status_reasons(server, monkeypatch):
    app, host, _, _ = server
    async with client(host, window=WIN_A):
        gate = app._tool_gate
        assert gate.window_status(WIN_B) == "not_linked"
        assert gate.window_status("garbage") == "not_linked"
        monkeypatch.setattr(tool_gate_ui, "gate_is_open", lambda: True)
        assert gate.window_status(WIN_A) == "busy"
        app.suppress_claude_popup = False
        assert gate.window_status(WIN_A) == "not_needed"


async def test_nothing_else_can_send_the_notice(server):
    app, host, _, _ = server
    notices = Notices()
    async with client(host, window=WIN_A, notices=notices) as a:
        await tool_names(a)
        [session] = list(app._tool_gate._window_sessions[WIN_A])
        await session.send_tool_list_changed()          # e.g. a library broadcasting on a backend mount
        await settle()
        assert notices.count == 0
        token = _OWN_TOOLS_NOTICE.set(True)
        try:
            await session.send_tool_list_changed()      # the gate's own, for this window
        finally:
            _OWN_TOOLS_NOTICE.reset(token)
        await settle()
        assert notices.count == 1


# ---- Voitta's routes (what the mod calls) -------------------------------------------

async def test_tool_popup_routes(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from llmgw.web import LlmAccounts

    class FakeGate:
        def __init__(self):
            self.release = asyncio.Event()

        def window_status(self, window):
            return None if window == WIN_A else "not_linked"

        async def window_popup(self, window):
            await self.release.wait()
            return {"status": "applied", "disabled": 3, "refreshed": 1}

    gate = FakeGate()
    llm = LlmAccounts(18999, data_dir=tmp_path / "llm")
    llm.tool_gate = lambda: gate
    app = web.Application()
    llm.add_routes(app.router)
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/_voitta/llm/api/tool-popup", json={"window": WIN_B})
        assert (await r.json()) == {"status": "not_linked"}
        r = await c.post("/_voitta/llm/api/tool-popup", json={"window": WIN_A})
        start = await r.json()
        assert start["status"] == "waiting"
        r = await c.get(f"/_voitta/llm/api/tool-popup/{start['id']}?wait=0.2")
        assert (await r.json()) == {"status": "waiting"}
        gate.release.set()
        r = await c.get(f"/_voitta/llm/api/tool-popup/{start['id']}?wait=5")
        assert (await r.json())["status"] == "applied"
        r = await c.post("/_voitta/llm/api/tool-popup", json={"window": WIN_A},
                         headers={"Origin": "https://evil.example"})
        assert r.status == 403
        llm.tool_gate = lambda: None                    # MCP server not running
        r = await c.post("/_voitta/llm/api/tool-popup", json={"window": WIN_A})
        assert (await r.json())["status"] == "not_linked"


# ---- the header script and the mod's copy ---------------------------------------------

def test_mod_runs_the_same_window_id_lines_as_the_header_script():
    src = pathlib.Path("llmgw/mod/voitta-llm/hooks/register.tsx").read_text()
    mod_sh = json.loads(re.search(r"^const WINDOW_ID_SH = (\".*\")$", src, re.M).group(1))
    assert mod_sh == window_header.WINDOW_ID_SH + "\nprintf '%s@%s' \"$p\" \"$s\""
    assert window_header.WINDOW_ID_SH in window_header.SCRIPT


def test_header_script_names_its_parent_process(tmp_path):
    import os
    import subprocess
    script = window_header.ensure_script(tmp_path / "bin" / "claude-window-header")
    assert os.access(script, os.X_OK)
    out = json.loads(subprocess.run([str(script)], capture_output=True, text=True, check=True).stdout)
    value = out[window_header.HEADER]
    assert value.split("@")[0] == str(os.getpid())
    assert re.fullmatch(r"\d+@[A-Za-z0-9_:]+", value)


async def test_installing_on_the_voitta_entry(tmp_path):
    cfg = tmp_path / ".claude.json"
    script = tmp_path / "bin" / "claude-window-header"
    calls: list[tuple] = []
    refuse = {"add": False}

    def write(entry):
        cfg.write_text(json.dumps({"numStartups": 3, "mcpServers": {"voitta": entry} if entry else {}}))

    async def run_claude(*args):
        calls.append(args)
        data = json.loads(cfg.read_text())
        if args[:2] == ("mcp", "remove"):
            data["mcpServers"].pop("voitta")
        elif args[:2] == ("mcp", "add-json"):
            if refuse["add"] and "headersHelper" in args[-1]:
                return 1, "invalid"
            data["mcpServers"]["voitta"] = json.loads(args[-1])
        cfg.write_text(json.dumps(data))
        return 0, "ok"

    write({"type": "http", "url": "http://127.0.0.1:18768/mcp", "headers": {"X-Keep": "1"}})
    status, steps = await window_header.ensure_on_voitta_entry(script, run_claude, cfg)
    assert status == "set"
    entry = window_header.voitta_entry(cfg)
    assert entry == {"type": "http", "url": "http://127.0.0.1:18768/mcp", "headers": {"X-Keep": "1"},
                     "headersHelper": str(script)}
    assert [c[:2] for c in calls] == [("mcp", "remove"), ("mcp", "add-json")]

    calls.clear()                                   # already set: nothing to do
    assert (await window_header.ensure_on_voitta_entry(script, run_claude, cfg))[0] == "set" and not calls

    write({"type": "http", "url": "u", "headersHelper": "/my/own"})   # the user's own helper: left alone
    assert (await window_header.ensure_on_voitta_entry(script, run_claude, cfg))[0] == "other_helper"
    assert not calls

    # a Voitta header script at another path (a deleted temp copy, an older home): replaced
    write({"type": "http", "url": "u", "headersHelper": "/gone/pytest-16/bin/claude-window-header"})
    assert window_header.status(script, cfg) == "stale"
    assert (await window_header.ensure_on_voitta_entry(script, run_claude, cfg))[0] == "set"
    assert window_header.voitta_entry(cfg)["headersHelper"] == str(script)
    calls.clear()

    write(None)                                     # no voitta entry: nothing written
    assert (await window_header.ensure_on_voitta_entry(script, run_claude, cfg))[0] == "no_entry"

    write({"type": "http", "url": "http://x/mcp"})  # the new entry refused: the original goes back
    refuse["add"] = True
    status, _ = await window_header.ensure_on_voitta_entry(script, run_claude, cfg)
    assert status == "missing" and window_header.voitta_entry(cfg) == {"type": "http", "url": "http://x/mcp"}
