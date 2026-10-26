"""Settings → Actions: everything that changes something outside Voitta, in one tab.

The tab shows where things stand (``_actions_state``, cheap file reads only,
plus the last ``claude plugin list`` result, which is slow and cached) and
runs actions the page asks for over the title bridge (``VOITTA_ACTION:<name>``).
Long work runs on the runtime; its result comes back to the page as
``_setActionResult(name, result)`` and a fresh ``_setActionsState(state)``.

Configure Claude makes Claude Code use Voitta at the user level, all three
places Claude Code reads:

  1. ``~/.claude/settings.json``: ``env.ANTHROPIC_BASE_URL`` → the LLM proxy
     (the same plan and bookkeeping as the Connect popup it replaces);
  2. Claude Code's user MCP list, ``~/.claude.json`` (where ``claude mcp add -s
     user`` writes; Claude Code keeps user-scope MCP servers there, not in
     ``~/.claude/``): a ``voitta`` HTTP entry → the MCP proxy, with Voitta's
     header script (llmgw/window_header.py);
  3. the voitta-llm mod (/llm, /voitta-store, the /mcp tool popup).

Host attributes consumed: ``_llm`` (LlmAccounts), ``llm_proxy_port``,
``mcp_proxy_port``, ``llm_upstream_url``, ``_settings_refs``, ``_config``,
``_apply_claude_link_plan``, ``_open_claude_link_popup``, ``_reset_tool_gate``,
``_show_llm_tools_status``, ``_reveal_desktop_log``, ``show_stored_conversations``.
"""

from __future__ import annotations

import json
import logging
import shutil
import time

from llmgw import window_header
from runtime import runtime
from ui.main_thread import on_main_thread

logger = logging.getLogger("voitta-desktop.actions")


class ActionsMixin:
    """Mixin: the Actions tab's state and actions."""

    # ── state ────────────────────────────────────────────────────────────────

    def _actions_state(self) -> dict:
        from claude_link import (
            CLAUDE_SETTINGS_PATH, is_codex_mcp_wired, is_voitta_connected, load_claude_settings,
            plan_connect, settings_file_is_malformed,
        )
        llm = getattr(self, "_llm", None)
        malformed = settings_file_is_malformed()
        cfg = {} if malformed else load_claude_settings()
        linked = (not malformed) and is_voitta_connected(cfg, self.llm_proxy_port)
        plan = None if malformed else plan_connect(cfg, self.llm_proxy_port, self.llm_upstream_url)
        mcp_url = f"http://127.0.0.1:{self.mcp_proxy_port}/mcp"
        script = llm.window_header_script if llm else None
        entry = window_header.entry_summary(script, mcp_url) if script else {"present": False}
        mod = getattr(self, "_actions_mod_view", None)
        claude_bin = llm._claude_bin() if llm else shutil.which("claude")
        changes = [{"label": c.label, "old": c.old, "new": c.new, "kind": c.kind}
                   for c in (plan.claude_changes if plan else [])]
        upstream = plan.voitta_upstream_change if plan else None
        return {
            "claude_found": bool(claude_bin),
            "claude_bin": claude_bin,
            "settings": {
                "path": str(CLAUDE_SETTINGS_PATH), "malformed": malformed, "linked": linked,
                "llm_url": f"http://127.0.0.1:{self.llm_proxy_port}",
                "changes": changes,
                "upstream_change": {"old": upstream.old, "new": upstream.new} if upstream else None,
                "armed": bool(getattr(self, "claude_link_armed", False)),
            },
            "mcp_entry": entry,
            "mod": mod,
            "mod_checked_at": getattr(self, "_actions_mod_checked", None),
            "codex_mcp": is_codex_mcp_wired(self.mcp_proxy_port),
            "busy": sorted(getattr(self, "_actions_busy", set())),
        }

    def _actions_ready(self, state: dict) -> bool:
        """Is Claude Code fully set up at the user level?"""
        mod = state.get("mod") or {}
        return (state["settings"]["linked"] and state["mcp_entry"].get("present")
                and state["mcp_entry"].get("url_ok") and state["mcp_entry"].get("header") == "set"
                and bool(mod.get("installed")) and mod.get("installed") == mod.get("available"))

    @on_main_thread
    def _push_actions(self, name: str | None = None, result: dict | None = None):
        refs = getattr(self, "_settings_refs", None)
        if not refs:
            return
        try:
            js = f"_setActionsState({json.dumps(self._actions_state())})"
            if name is not None:
                js = f"_setActionResult({json.dumps(name)}, {json.dumps(result or {})});" + js
            refs[1].evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            logger.debug("actions push failed", exc_info=True)

    def _actions_check_mod(self, then=None):
        """Ask Claude Code which mod is installed (slow: ``claude plugin list``)."""
        llm = getattr(self, "_llm", None)
        if llm is None:
            return

        async def check():
            return await llm._mod_view()

        def done(fut):
            try:
                self._actions_mod_view = fut.result()
            except Exception as e:
                self._actions_mod_view = {"error": f"{type(e).__name__}: {e}"}
            self._actions_mod_checked = time.time()
            if then:
                then()
            else:
                self._push_actions()

        runtime.submit(check()).add_done_callback(done)

    # ── actions ──────────────────────────────────────────────────────────────

    def _run_action(self, name: str):
        """From the page (main thread). Unknown names are ignored."""
        logger.info("action: %s", name)
        simple = {
            "refresh": lambda: self._actions_check_mod(),
            "disconnect_claude": lambda: self._open_claude_link_popup(),
            "refresh_tools": lambda: self._show_llm_tools_status(None),
            "reset_tool_gate": lambda: (self._reset_tool_gate(None), self._push_actions(
                "reset_tool_gate", {"ok": True, "message": "Tool gate reset: the next tool listing asks again."})),
            "show_log": lambda: self._reveal_desktop_log(None),
            "stored_conversations": lambda: self.show_stored_conversations(None),
        }
        if name in simple:
            simple[name]()
        elif name == "configure_claude":
            self._configure_claude()
        elif name in ("install_mod", "reinstall_mod"):
            self._install_mod(reinstall=name == "reinstall_mod")

    def _start(self, name: str) -> bool:
        busy = self.__dict__.setdefault("_actions_busy", set())
        if name in busy:
            return False
        busy.add(name)
        self._push_actions()
        return True

    def _finish(self, name: str, result: dict):
        getattr(self, "_actions_busy", set()).discard(name)
        self._actions_check_mod(then=lambda: self._push_actions(name, result))

    def _configure_claude(self):
        """All three user-level pieces; each one reports how it went."""
        if not self._start("configure_claude"):
            return
        steps = []
        from claude_link import load_claude_settings, plan_connect, settings_file_is_malformed
        if settings_file_is_malformed():
            steps.append({"step": "LLM proxy", "ok": False,
                          "detail": "~/.claude/settings.json is not valid JSON; fix it by hand, then run this again."})
        else:
            plan = plan_connect(load_claude_settings(), self.llm_proxy_port, self.llm_upstream_url)
            if plan.is_noop and getattr(self, "claude_link_armed", False):
                steps.append({"step": "LLM proxy", "ok": True, "detail": "already set"})
            else:
                try:
                    notes = self._apply_claude_link_plan(plan)
                    steps.append({"step": "LLM proxy", "ok": True,
                                  "detail": "; ".join(notes) or "ANTHROPIC_BASE_URL set"})
                except Exception as e:
                    logger.error("configure claude: settings.json failed: %s", e, exc_info=True)
                    steps.append({"step": "LLM proxy", "ok": False, "detail": str(e)[:300]})

        llm = getattr(self, "_llm", None)
        if llm is None:
            steps.append({"step": "MCP entry and mod", "ok": False, "detail": "the LLM proxy is not running"})
            self._finish("configure_claude", {"ok": False, "steps": steps})
            return

        async def rest():
            entry_status, entry_steps = await llm.configure_mcp_entry()
            mod = await llm.install_mod(reinstall=False)
            return entry_status, entry_steps, mod

        def done(fut):
            try:
                entry_status, entry_steps, mod = fut.result()
                entry_ok = entry_status in ("set", "other_helper")
                steps.append({"step": "MCP entry", "ok": entry_ok, "detail": _entry_detail(entry_status),
                              "log": entry_steps})
                steps.append({"step": "/llm mod", "ok": bool(mod.get("ok")),
                              "detail": (f"voitta-llm {mod.get('installed')}" if mod.get("ok")
                                         else (mod.get("error") or "install failed")),
                              "log": mod.get("steps")})
            except Exception as e:
                logger.exception("configure claude failed")
                steps.append({"step": "MCP entry and mod", "ok": False, "detail": f"{type(e).__name__}: {e}"})
            ok = all(s["ok"] for s in steps)
            logger.info("configure claude: %s", [(s["step"], s["ok"]) for s in steps])
            self._finish("configure_claude", {"ok": ok, "steps": steps})

        runtime.submit(rest()).add_done_callback(done)

    def _install_mod(self, reinstall: bool):
        name = "reinstall_mod" if reinstall else "install_mod"
        if not self._start(name):
            return
        llm = getattr(self, "_llm", None)

        def done(fut):
            try:
                r = fut.result()
                result = {"ok": bool(r.get("ok")), "steps": [{"step": "/llm mod", "ok": bool(r.get("ok")),
                          "detail": f"voitta-llm {r.get('installed')}" if r.get("ok")
                          else (r.get("error") or "install failed"), "log": r.get("steps")}]}
            except Exception as e:
                result = {"ok": False, "steps": [{"step": "/llm mod", "ok": False, "detail": str(e)}]}
            self._finish(name, result)

        runtime.submit(llm.install_mod(reinstall=reinstall)).add_done_callback(done)


def _entry_detail(status: str) -> str:
    return {
        "set": "voitta → this Voitta's MCP proxy, with the header script",
        "other_helper": "voitta → this Voitta's MCP proxy; it keeps your own header script, "
                        "so /mcp can't give one window its own tools",
        "not_http": "voitta is not an HTTP entry (set up by hand?); left alone",
        "no_entry": "could not add the voitta entry (see the log)",
        "missing": "could not add the header script (see the log)",
        "stale": "could not repair the header script (see the log)",
    }.get(status, status)
