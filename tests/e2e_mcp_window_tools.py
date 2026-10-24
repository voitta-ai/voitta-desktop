#!/usr/bin/env python3
"""/mcp per-window tools with real, interactive Claude Code windows.

A test copy of Voitta runs here: the real ToolGateMiddleware behind a real MCP
server (two tools, ping and pong), and the real LLM proxy with Voitta's routes.
Only the AppKit popup is replaced: it counts what it is asked and answers from
a script, so nothing appears on screen. Windows connect with Voitta's header
script as their headersHelper, like a set-up machine.

  phase 1  popup silenced, a window WITHOUT the /mcp hook (the installed mod, or
           none): /mcp → Reconnect, /clear, an MCP server rebuild, idle.
           Expect: zero popups; every listing silent.
  phase 2  popup NOT silenced, same kind of window: today's behaviour, one popup
           per new MCP session and none otherwise (no listChanged given).
  phase 3  popup silenced, two windows WITH the /mcp hook (this repo's mod):
           /mcp in window A. Expect one popup, A's tools change without a
           reconnect, B is untouched and gets no notice.

    python tests/e2e_mcp_window_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pty
import re
import select
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastmcp import FastMCP                              # noqa: E402
from fastmcp.server.middleware import Middleware         # noqa: E402

import ui.tool_gate as tool_gate_ui                      # noqa: E402
from llmgw import window_header                          # noqa: E402
from llmgw.web import LlmAccounts                        # noqa: E402
from mcpproxy.health import McpServerHost                # noqa: E402
from mcpproxy.server import ToolGateMiddleware           # noqa: E402
from proxy import AnthropicProxy                         # noqa: E402

MOD = ROOT / "llmgw" / "mod" / "voitta-llm"
TMP = Path(tempfile.mkdtemp(prefix="voitta-mcp-e2e-"))
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>()][0-9A-Za-z]?")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    terminal_mode = False
    suppress_codex_popup = True
    suppress_claude_popup = True
    disabled_tools: set[str] = set()
    _tool_gate = None

    def _build_tool_tree(self):
        return [{"name": "voitta", "tools": [{"name": "ping"}, {"name": "pong"}]}]


APP = App()
POPUPS: list[dict] = []            # every popup asked for: {"title", "disabled", "t"}
LISTINGS: list[dict] = []          # every tools/list answered: {"window", "session", "tools", "t"}
INITS: list[dict] = []
NOTICES_ALLOWED = []


async def fake_popup(tool_groups, disabled, meta=None, on_result=None, title="MCP Tools Gate", heading=""):
    POPUPS.append({"title": title, "disabled": sorted(disabled), "t": time.time()})
    await asyncio.sleep(1.0)       # a person reading
    answer = ["ping"] if title.startswith("Tools for this") else ["pong"]
    if on_result:
        on_result(answer)
    return answer

tool_gate_ui.show_tool_gate = fake_popup
tool_gate_ui.gate_is_open = lambda: False


class Spy(Middleware):
    """Outermost: records what each connection was actually given."""

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        from fastmcp.server.dependencies import get_http_request
        INITS.append({"t": time.time(), "window": get_http_request().headers.get("x-voitta-claude-window"),
                      "listChanged": bool(context.fastmcp_context.session._init_options.capabilities.tools.listChanged)})
        return result

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        from fastmcp.server.dependencies import get_http_request
        LISTINGS.append({"t": time.time(), "window": get_http_request().headers.get("x-voitta-claude-window"),
                         "session": (context.fastmcp_context.session_id or "")[:8],
                         "tools": sorted(t.name for t in tools)})
        return tools


def build(app, _port):
    gate = ToolGateMiddleware(app)
    app._tool_gate = gate
    main = FastMCP("voitta", middleware=[Spy(), gate])
    main._mcp_server.notification_options.tools_changed = False

    @main.tool()
    def ping() -> str:
        return "pong"

    @main.tool()
    def pong() -> str:
        return "ping"
    return main


# ---- the test Voitta, on its own event loop ------------------------------------------

LOOP = asyncio.new_event_loop()
MCP_PORT, LLM_PORT = free_port(), free_port()
HOST = McpServerHost(APP, MCP_PORT, build)
LLM = LlmAccounts(LLM_PORT, data_dir=TMP / "llm")
LLM.tool_gate = lambda: APP._tool_gate


async def serve():
    proxy = AnthropicProxy(middlewares=[], port=LLM_PORT, upstream_url="https://api.anthropic.com", llm=LLM)
    await proxy.start()
    await HOST.serve()

threading.Thread(target=lambda: LOOP.run_until_complete(serve()), daemon=True).start()
while not (HOST._server and HOST._server.started):
    time.sleep(0.05)

SCRIPT = window_header.ensure_script(TMP / "bin" / "claude-window-header")
MCP_CFG = TMP / "mcp.json"
MCP_CFG.write_text(json.dumps({"mcpServers": {"voitta": {
    "type": "http", "url": f"http://127.0.0.1:{MCP_PORT}/mcp", "headersHelper": str(SCRIPT)}}}))
# The user's installed voitta-llm would load in every test window too (and run
# its own /mcp hook beside this repo's copy); turn it off in these windows only.
SETTINGS = json.dumps({"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{LLM_PORT}"},
                       "enabledPlugins": {"voitta-llm@voitta": False}})

ENV = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + ":" + os.environ["PATH"], TERM="xterm-256color")
for k in [k for k in ENV if k.startswith(("CLAUDE", "AI_AGENT")) or k == "CLAUDECODE"]:
    ENV.pop(k)


class Window:
    def __init__(self, name: str, with_mod: bool):
        self.name, self.buf = name, b""
        args = ["claude", "--strict-mcp-config", "--mcp-config", str(MCP_CFG), "--settings", SETTINGS,
                "--model", "claude-haiku-4-5"]
        if with_mod:
            args += ["--plugin-dir", str(MOD)]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(ROOT)
            os.execvpe("claude", args, ENV)

    def pump(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if r:
                try:
                    self.buf += ANSI.sub(b"", os.read(self.fd, 65536))
                except OSError:
                    return

    def text(self, n=3000) -> str:
        return re.sub(r"[ \t]+", " ", self.buf.decode("utf-8", "replace"))[-n:]

    def wait_for(self, pattern: str, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            self.pump(0.3)
            if re.search(pattern, self.text(), re.I):
                return True
        return False

    def send(self, keys: bytes, pause: float = 1.0):
        os.write(self.fd, keys)
        self.pump(pause)

    def highlighted(self) -> str:
        """The menu item after the last ❯ on screen."""
        t = self.buf.decode("utf-8", "replace")
        i = t.rfind("❯")
        return t[i + 1:i + 80].split("\r")[0].strip() if i >= 0 else ""

    def choose(self, label: str, tries: int = 12) -> bool:
        for _ in range(tries):
            if label in self.highlighted():
                return True
            self.send(b"\x1b[B", 0.5)
        return label in self.highlighted()

    def mcp_reconnect(self) -> bool:
        """/mcp → voitta → Reconnect, then out of the menu. The list also holds
        Claude Code's built-in servers (claude-in-chrome), so pick by name."""
        self.buf = b""
        self.send(b"/mcp", 1.0)
        self.send(b"\r", 0.5)
        self.wait_for(r"voitta", 60)          # with the hook, the menu waits for the popup
        self.pump(1.5)
        ok = self.choose("voitta")
        self.buf = b""
        self.send(b"\r", 2.0)                 # open voitta
        ok = self.choose("Reconnect") and ok
        self.send(b"\r", 4.0)                 # Reconnect
        self.send(b"\x1b", 1.0)
        self.send(b"\x1b", 1.0)
        return ok

    def close(self):
        for _ in range(2):
            try:
                os.write(self.fd, b"\x03")
            except OSError:
                pass
            time.sleep(0.4)
        try:
            os.kill(self.pid, 9)
        except ProcessLookupError:
            pass


def window_id(pid: int) -> str:
    import subprocess
    lstart = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    return f"{pid}@{'_'.join(lstart)}"


def report(title, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {title}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


results = []
try:
    # ---- phase 1: silenced, no /mcp hook ------------------------------------------
    APP.suppress_claude_popup = True
    t0, p0 = time.time(), len(POPUPS)
    w = Window("no-hook", with_mod=False)
    w.wait_for(r"\? for shortcuts|Try \"", 40)
    w.pump(4)
    reconnects = [w.mcp_reconnect()]
    w.send(b"/clear", 0.8)
    w.send(b"\r", 3)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), LOOP).result()
    HOST.rebuild("e2e: rebuild while a window is connected")
    time.sleep(3)
    reconnects.append(w.mcp_reconnect())
    w.pump(20)                                 # idle
    w.close()
    mine = [x for x in LISTINGS if x["t"] >= t0]
    inits = [x for x in INITS if x["t"] >= t0]
    results.append(report("phase 1: popup silenced, window without the /mcp hook → zero popups",
                          len(POPUPS) == p0 and all(reconnects) and len(inits) >= 3
                          and all(x["tools"] == ["ping", "pong"] for x in mine),
                          f"popups {len(POPUPS) - p0}, reconnects picked {reconnects}, listings {len(mine)}, "
                          f"connections {len(inits)}, "
                          f"listChanged given {sum(x['listChanged'] for x in inits)}/{len(inits)}"))

    # ---- phase 2: not silenced (today's behaviour) --------------------------------------
    APP.suppress_claude_popup = False
    t0, p0 = time.time(), len(POPUPS)
    w = Window("no-hook-popups-on", with_mod=False)
    w.wait_for(r"\? for shortcuts|Try \"", 40)
    w.pump(5)
    picked = w.mcp_reconnect()
    w.pump(20)
    w.close()
    inits = [x for x in INITS if x["t"] >= t0]
    popups = POPUPS[p0:]
    results.append(report("phase 2: popup in use → one popup per new connection, none otherwise, no listChanged",
                          picked and len(inits) >= 2 and len(popups) == len(inits)
                          and not any(x["listChanged"] for x in inits)
                          and all(p["title"] == "MCP Tools Gate" for p in popups),
                          f"connections {len(inits)}, popups {len(popups)}"))

    # ---- phase 3: silenced, two windows with the /mcp hook -------------------------------
    APP.suppress_claude_popup = True
    t0, p0 = time.time(), len(POPUPS)
    a, b = Window("A", with_mod=True), Window("B", with_mod=True)
    for x in (a, b):
        x.wait_for(r"\? for shortcuts|Try \"", 40)
    a.pump(4)
    b.pump(1)
    wa, wb = window_id(a.pid), window_id(b.pid)
    t_mcp = time.time()
    a.buf = b""
    a.send(b"/mcp", 1.0)
    a.send(b"\r", 0.5)
    # After the popup, Claude Code's own /mcp menu opens, as without Voitta.
    # "Manage MCP servers" is also /mcp's own line in the typeahead, so look for
    # the menu's section headings instead.
    menu_opened = a.wait_for(r"User\s*MCPs|Built-in\s*MCPs", 60)
    t_done = time.time()
    a.send(b"\x1b", 1.0)                      # closed the usual way
    a.send(b"\x1b", 1.0)
    b.pump(4)
    a.pump(2)
    window_popups = [p for p in POPUPS[p0:] if p["title"].startswith("Tools for this")]
    a_lists = [x for x in LISTINGS if x["window"] == wa and x["t"] >= t0]
    b_lists = [x for x in LISTINGS if x["window"] == wb and x["t"] >= t0]
    a_after = [x for x in a_lists if x["t"] > t_mcp]
    b_after = [x for x in b_lists if x["t"] > t_mcp]
    inits_a = [x for x in INITS if x["window"] == wa and x["t"] >= t0]
    ok = (len(POPUPS) - p0 == 1 and len(window_popups) == 1 and menu_opened
          and window_popups[0]["t"] < t_done
          and bool(a_after) and a_after[-1]["tools"] == ["pong"]
          and len(inits_a) == 1                                   # no reconnect: same connection re-listed
          and not b_after and all(x["tools"] == ["ping", "pong"] for x in b_lists))
    results.append(report("phase 3: /mcp in window A → one popup, then Claude Code's normal /mcp menu; "
                          "A re-lists on the same connection without ping; B untouched",
                          ok, f"popups {len(POPUPS) - p0}, A listings after /mcp {[x['tools'] for x in a_after]}, "
                              f"A connections {len(inits_a)}, B listings after /mcp {len(b_after)}, "
                              f"menu opened {menu_opened}"))
    for x in (a, b):
        x.close()
finally:
    HOST._server.should_exit = True
    time.sleep(1)

print("\nall passed" if all(results) else "\nSOME FAILED", flush=True)
sys.exit(0 if all(results) else 1)
