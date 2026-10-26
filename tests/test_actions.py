"""Settings → Actions → Configure Claude, on a fresh "machine".

The Claude Code config here is the test suite's throwaway one (conftest sets
CLAUDE_CONFIG_DIR, which both Claude Code and claude_link follow). The last
test runs the real `claude` CLI against it, so what Configure Claude writes is
checked by Claude Code itself; the developer's own ~/.claude* is checked to be
untouched.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import claude_link
from llmgw import window_header

MCP_URL = "http://127.0.0.1:18768/mcp"


def _fake_claude(cfg: Path, calls: list, refuse_add: bool = False):
    async def run(*args):
        calls.append(args)
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
        servers = data.setdefault("mcpServers", {})
        if args[:2] == ("mcp", "remove"):
            servers.pop("voitta", None)
        elif args[:2] == ("mcp", "add-json"):
            if refuse_add:
                return 1, "invalid"
            servers["voitta"] = json.loads(args[-1])
        cfg.write_text(json.dumps(data))
        return 0, "ok"
    return run


def _write(cfg: Path, entry):
    cfg.write_text(json.dumps({"numStartups": 1, "mcpServers": {"voitta": entry} if entry else {}}))


async def test_entry_is_created_on_a_fresh_machine(tmp_path):
    cfg, script, calls = tmp_path / ".claude.json", tmp_path / "bin" / "claude-window-header", []
    status, _ = await window_header.ensure_voitta_entry(script, MCP_URL, _fake_claude(cfg, calls), cfg)
    assert status == "set"
    assert window_header.voitta_entry(cfg) == {"type": "http", "url": MCP_URL, "headersHelper": str(script)}
    assert [c[:2] for c in calls] == [("mcp", "add-json")] and "-s" in calls[0] and "user" in calls[0]
    assert os.access(script, os.X_OK)


async def test_entry_pointing_elsewhere_is_repointed_and_keeps_its_fields(tmp_path):
    cfg, script, calls = tmp_path / ".claude.json", tmp_path / "bin" / "claude-window-header", []
    _write(cfg, {"type": "http", "url": "http://127.0.0.1:18765/mcp", "headers": {"X-Keep": "1"}})
    status, _ = await window_header.ensure_voitta_entry(script, MCP_URL, _fake_claude(cfg, calls), cfg)
    assert status == "set"
    assert window_header.voitta_entry(cfg) == {"type": "http", "url": MCP_URL, "headers": {"X-Keep": "1"},
                                               "headersHelper": str(script)}
    calls.clear()                                  # right already: nothing run
    assert (await window_header.ensure_voitta_entry(script, MCP_URL, _fake_claude(cfg, calls), cfg))[0] == "set"
    assert not calls


async def test_users_own_header_script_is_kept_but_the_url_is_fixed(tmp_path):
    cfg, script, calls = tmp_path / ".claude.json", tmp_path / "bin" / "claude-window-header", []
    _write(cfg, {"type": "http", "url": "http://old/mcp", "headersHelper": "/my/own"})
    status, _ = await window_header.ensure_voitta_entry(script, MCP_URL, _fake_claude(cfg, calls), cfg)
    assert status == "other_helper"
    assert window_header.voitta_entry(cfg) == {"type": "http", "url": MCP_URL, "headersHelper": "/my/own"}


async def test_a_hand_made_stdio_entry_is_left_alone(tmp_path):
    cfg, script, calls = tmp_path / ".claude.json", tmp_path / "bin" / "claude-window-header", []
    entry = {"type": "stdio", "command": "voitta-bridge"}
    _write(cfg, entry)
    assert (await window_header.ensure_voitta_entry(script, MCP_URL, _fake_claude(cfg, calls), cfg))[0] == "not_http"
    assert window_header.voitta_entry(cfg) == entry and not calls


async def test_a_refused_entry_is_put_back(tmp_path):
    cfg, script, calls = tmp_path / ".claude.json", tmp_path / "bin" / "claude-window-header", []
    entry = {"type": "http", "url": "http://old/mcp"}
    _write(cfg, entry)
    await window_header.ensure_voitta_entry(script, MCP_URL, _fake_claude(cfg, calls, refuse_add=True), cfg)
    assert window_header.voitta_entry(cfg) is None or window_header.voitta_entry(cfg) == entry


def test_claude_link_follows_claude_config_dir():
    # conftest points CLAUDE_CONFIG_DIR at a throwaway folder; claude_link must use it.
    base = Path(os.environ["CLAUDE_CONFIG_DIR"])
    assert claude_link.CLAUDE_SETTINGS_PATH == base / "settings.json"
    assert claude_link.CLAUDE_USER_CONFIG_PATH == base / ".claude.json"


# ---- Configure Claude through the Actions mixin, with the real `claude` CLI ----------

def _claude_bin():
    from llmgw.web import LlmAccounts
    return LlmAccounts._claude_bin()


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.mark.skipif(not _claude_bin(), reason="Claude Code (the claude command) is not installed")
def test_configure_claude_on_a_fresh_machine_with_the_real_claude_cli(tmp_path, monkeypatch):
    from llmgw.web import LlmAccounts
    from runtime import AsyncRuntime
    import runtime as runtime_mod
    import ui.actions as actions_mod

    real = [Path.home() / ".claude.json", Path.home() / ".claude" / "settings.json"]
    before = [_digest(p) for p in real]
    # A Claude config of its own: nothing it writes reaches the other tests.
    base = tmp_path / "claude-config"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setattr(claude_link, "CLAUDE_SETTINGS_PATH", base / "settings.json")
    monkeypatch.setattr(claude_link, "CLAUDE_USER_CONFIG_PATH", base / ".claude.json")

    rt = AsyncRuntime()
    rt.start()
    monkeypatch.setattr(runtime_mod, "runtime", rt)
    monkeypatch.setattr(actions_mod, "runtime", rt)
    monkeypatch.setattr(actions_mod, "on_main_thread", lambda f: f)

    llm = LlmAccounts(18999, data_dir=tmp_path / "home" / "llm")
    llm.mcp_url = MCP_URL

    done = concurrent.futures.Future()

    class Host(actions_mod.ActionsMixin):
        llm_proxy_port, mcp_proxy_port, llm_upstream_url = 18901, 18768, "https://api.anthropic.com"
        claude_link_armed = False
        _config: dict = {}
        _settings_refs = None
        _llm = llm

        def _apply_claude_link_plan(self, plan):        # the settings window's, minus AppKit
            claude_link.apply_changes(plan)
            self.claude_link_armed = plan.target == "connect"
            return []

        def _push_actions(self, name=None, result=None):
            if name and not done.done():
                done.set_result(result)

    host = Host()
    try:
        state = host._actions_state()
        assert not state["settings"]["linked"] and not state["mcp_entry"]["present"]
        host._run_action("configure_claude")
        result = done.result(timeout=240)
    finally:
        rt.shutdown(timeout=5.0)

    assert result["ok"], result
    assert [s["step"] for s in result["steps"]] == ["LLM proxy", "MCP entry", "/llm mod"]
    # Claude Code itself reads back what was configured.
    env = dict(os.environ)
    get = subprocess.run([_claude_bin(), "mcp", "get", "voitta"], capture_output=True, text=True, env=env, timeout=60)
    assert MCP_URL in get.stdout and "User" in get.stdout, get.stdout + get.stderr
    entry = window_header.voitta_entry(base / ".claude.json")
    assert entry["headersHelper"] == str(llm.window_header_script)
    settings = json.loads((base / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18901"
    plugins = json.loads(subprocess.run([_claude_bin(), "plugin", "list", "--json"], capture_output=True,
                                        text=True, env=env, timeout=60).stdout)
    assert any(p["id"] == "voitta-llm@voitta" for p in plugins)
    assert host._actions_ready(host._actions_state() | {"mod": {"installed": "x", "available": "x"}})
    # and the developer's own Claude Code config was never touched
    assert [_digest(p) for p in real] == before
