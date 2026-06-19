"""MCP subprocess + Claude-link lifecycle for VoittaDesktopApp.

Subprocess launching is data-driven from ``self._config['mcp_servers']``:
each entry with ``kind=subprocess`` and ``subprocess.template ∈
{google_mcp, jira_mcp}`` triggers the matching launch + .env-sync flow.

Templates:
- ``google_mcp`` / ``jira_mcp`` — HTTP subprocesses with hardcoded argv; only
  cwd/env_path/port flow through from config.
- ``http_command`` — generic HTTP subprocess: a user-supplied command (from
  config) that serves MCP over HTTP on ``port``. No code branch needed per
  server; the port is conveyed via ``{port}``/``{env_path}`` argv tokens or the
  ``port_env`` env var.
- ``npx`` / ``command`` — stdio, launched by fastmcp (nothing to start here).

Adding another hardcoded-argv template means adding a branch in
``_resolve_subprocess_launch``; ``http_command`` covers the general case
without one.

Servers we launch (the HTTP-subprocess templates) are tracked in
``self._subprocesses`` (dict keyed by server id), have their merged
stdout/stderr captured to ``~/.voitta-desktop/logs/mcp-<id>.log``, and can be
started/stopped/restarted live via the public ``*_mcp_server`` methods — the
settings UI drives these through the WKWebView bridge.
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from config import load_config

logger = logging.getLogger("voitta-desktop")

# OAuth redirect port still lives in cfg.oauth (independent of MCPs).
_oauth_cfg = load_config().get("oauth", {})
OAUTH_REDIRECT_PORT = int(_oauth_cfg.get("redirect_port", 53214))


def _subprocess_servers(cfg: dict, template: str) -> list[dict]:
    """Return mcp_servers entries that match a given subprocess template."""
    out = []
    for s in cfg.get("mcp_servers", []) or []:
        if s.get("kind") != "subprocess":
            continue
        sp = s.get("subprocess") or {}
        if sp.get("template") == template:
            out.append(s)
    return out


# Templates whose process WE own (we Popen it) — these can be started/stopped/
# restarted live and have their stdout/stderr captured. npx/command are stdio
# and owned by fastmcp (no handle); plain http servers are external.
_CONTROLLABLE_TEMPLATES = {"google_mcp", "jira_mcp", "http_command"}


def _server_id(server: dict) -> str:
    """Stable id for a server — the prefix (tool namespace, always present for a
    mounted server) falling back to name."""
    return (server.get("prefix") or server.get("name") or "").strip()


def _is_controllable(server: dict) -> bool:
    if server.get("kind") != "subprocess":
        return False
    return (server.get("subprocess") or {}).get("template", "") in _CONTROLLABLE_TEMPLATES


def _listeners_on_port(port: int) -> list[int]:
    """PIDs with a LISTEN socket on `port` (macOS, via lsof). Empty on any
    error — reclaim is best-effort."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    pids = []
    for line in out.split():
        try:
            pids.append(int(line))
        except ValueError:
            pass
    return pids


def _reclaim_port(port: int, name: str):
    """Kill any process listening on `port` so a fresh launch can bind. Skips
    our own pid for safety. Best-effort: never raises."""
    my_pid = os.getpid()
    for pid in _listeners_on_port(port):
        if pid == my_pid:
            continue
        logger.warning("reclaiming port %d from orphan pid %d (for %s)", port, pid, name)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception as e:
            logger.warning("could not SIGTERM pid %d: %s", pid, e)
            continue
    # Give SIGTERM a moment, then SIGKILL anything that's still bound.
    if _listeners_on_port(port):
        for _ in range(20):  # up to ~2s
            time.sleep(0.1)
            if not _listeners_on_port(port):
                break
        for pid in _listeners_on_port(port):
            if pid == my_pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def _mcp_log_path(server_id: str) -> str:
    """Per-server combined stdout+stderr log under the desktop log dir."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in server_id) or "server"
    log_dir = os.path.expanduser("~/.voitta-desktop/logs")
    return os.path.join(log_dir, f"mcp-{safe}.log")


def _resolve_subprocess_launch(server: dict, base_env: dict):
    """Pure helper: turn a subprocess server config into (argv, env) ready for
    Popen, or (None, reason) if it can't/shouldn't launch. Knows each template's
    command shape; only cwd/env_path/port flow through from config."""
    sp = server.get("subprocess") or {}
    template = sp.get("template", "")
    cwd = os.path.expanduser(sp.get("cwd", "") or "")
    env_path = os.path.expanduser(sp.get("env_path", "") or "")
    port = int(sp.get("port", 0) or 0)

    if template == "google_mcp":
        if not cwd or not Path(cwd).is_dir():
            return None, f"cwd {cwd!r} missing"
        if not port:
            return None, "no port"
        return (["uv", "run", "main.py", "--transport", "streamable-http"],
                {**base_env, "PORT": str(port)})

    if template == "jira_mcp":
        if not cwd or not Path(cwd).is_dir():
            return None, f"cwd {cwd!r} missing"
        if not env_path or not Path(env_path).exists():
            return None, f"env_path {env_path!r} missing"
        if not port:
            return None, "no port"
        return (["uvx", "mcp-atlassian", "--transport", "streamable-http",
                 "--port", str(port), "--env-file", env_path],
                dict(base_env))

    if template == "http_command":
        # Generic HTTP subprocess: a user-supplied command that serves MCP over
        # HTTP on `port`. The proxy connects to http://localhost:{port}/mcp (see
        # mcpproxy.server._server_url). The port is conveyed via {port}/{env_path}
        # argv tokens or the port_env env var.
        if not cwd or not Path(cwd).is_dir():
            return None, f"cwd {cwd!r} missing"
        if not port:
            return None, "no port"
        argv = [
            str(a).replace("{port}", str(port)).replace("{env_path}", env_path)
            for a in (sp.get("command") or [])
        ]
        if not argv:
            return None, "no command"
        env = dict(base_env)
        if sp.get("port_env"):
            env[sp["port_env"]] = str(port)
        return argv, env

    if template in ("npx", "command"):
        return None, "stdio transport, managed by fastmcp"

    return None, f"unknown template {template!r}"


class MCPLifecycleMixin:
    """Mixin: MCP subprocess + Claude-link arm/disarm for ``VoittaDesktopApp``.

    Methods depend on ``self._config``, ``self.claude_link_armed``,
    ``self.llm_proxy_port``, ``self.llm_upstream_url``,
    ``self._subprocesses``.
    """

    # ── MCP .env sync ────────────────────────────────────────────────────────

    def _sync_edit_mcp_env(self):
        """Write Google MCP's .env file with the active OAuth client creds.

        Pulls the env_path from the relevant mcp_servers entry; if no
        google_mcp subprocess server is configured, silently skips.
        """
        google_servers = _subprocess_servers(self._config, "google_mcp")
        if not google_servers:
            return
        env_path = os.path.expanduser(
            google_servers[0]["subprocess"].get("env_path", "")
        )
        if not env_path:
            return

        gw_google = [a for a in self._config.get("apps", [])
                     if a["type"] == "google" and "google_workspace" in a.get("use_for", [])]
        if not gw_google:
            return
        app = gw_google[0]
        client_id = app.get("client_id", "")
        client_secret = app.get("client_secret", "")
        if not client_id or not client_secret:
            return
        lines = [
            "# Managed by voitta-desktop",
            f"GOOGLE_OAUTH_CLIENT_ID={client_id}",
            f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}",
            "MCP_ENABLE_OAUTH21=true",
            "EXTERNAL_OAUTH21_PROVIDER=true",
            "",
        ]
        try:
            Path(env_path).parent.mkdir(parents=True, exist_ok=True)
            Path(env_path).write_text("\n".join(lines))
        except Exception as e:
            logger.warning("Failed to write Google MCP .env: %s", e)

    def _sync_jira_mcp_env(self):
        jira_servers = _subprocess_servers(self._config, "jira_mcp")
        if not jira_servers:
            return
        env_path = os.path.expanduser(
            jira_servers[0]["subprocess"].get("env_path", "")
        )
        if not env_path:
            return

        jira = self._config.get("jira", {})
        server_url = jira.get("server_url", "")
        email = jira.get("email", "")
        token = jira.get("api_token", "")
        if not server_url or not email or not token:
            return
        project = jira.get("project", "")
        lines = [
            "# Managed by voitta-desktop",
            f"JIRA_URL={server_url}",
            f"JIRA_USERNAME={email}",
            f"JIRA_API_TOKEN={token}",
        ]
        if project:
            lines.append(f"JIRA_PROJECTS_FILTER={project}")
        lines.append("")
        try:
            Path(env_path).parent.mkdir(parents=True, exist_ok=True)
            Path(env_path).write_text("\n".join(lines))
        except Exception as e:
            logger.warning("Failed to write Jira MCP .env: %s", e)

    # ── MCP subprocesses ─────────────────────────────────────────────────────

    def _mcp_base_env(self) -> dict:
        """Environment for launched MCP subprocesses.

        Bundle launchctl strips PATH to the minimal /usr/bin:/bin:/usr/sbin:/sbin,
        so Homebrew-installed tools (uv, uvx) aren't found. Extend PATH with the
        standard install locations. CLI dev already has these, so no-op there.

        ~/.pyenv/shims is included so a bare ``python``/``python3`` resolves to the
        user's pyenv interpreter — matching their interactive shell. Without it a
        GUI launch (Finder/launchd) never sources the shell rc that puts pyenv on
        PATH, so ``python server.py`` subprocess MCPs fail with ENOENT.
        """
        extra_path = ":".join([
            os.path.expanduser("~/.pyenv/shims"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/.cargo/bin"),
        ])
        return {**os.environ, "PATH": f"{extra_path}:{os.environ.get('PATH', '')}"}

    def _launch_one_subprocess(self, server: dict) -> dict | None:
        """Launch a single subprocess server and register it in
        ``self._subprocesses`` keyed by server id. stdout+stderr are captured
        (merged) into a per-server log file, truncated on each (re)launch.
        Returns the tracking entry, or None if it didn't launch."""
        sid = _server_id(server)
        name = server.get("name") or sid
        argv, env_or_reason = _resolve_subprocess_launch(server, self._mcp_base_env())
        if argv is None:
            logger.info("subprocess %r skipped: %s", name, env_or_reason)
            return None
        env = env_or_reason
        cwd = os.path.expanduser((server.get("subprocess") or {}).get("cwd", "") or "")
        port = int((server.get("subprocess") or {}).get("port", 0) or 0)
        log_path = _mcp_log_path(sid)
        # Reclaim the port from any orphan listener before binding. atexit
        # doesn't run on force-quit/SIGKILL/crash, so a subprocess MCP can leak
        # across sessions and squat its port — the next launch then dies with
        # "address already in use". These ports are dedicated to desktop-managed
        # MCP servers, so any listener is our own orphan: kill it, then bind.
        if port:
            _reclaim_port(port, name)
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "w")  # truncate per launch
            proc = subprocess.Popen(
                argv, cwd=cwd or None, env=env,
                stdout=log_fh, stderr=subprocess.STDOUT,
            )
        except Exception as e:
            logger.warning("Failed to start %s: %s", name, e)
            return None
        entry = {
            "proc": proc, "log_fh": log_fh, "log_path": log_path,
            "name": name, "port": port, "intentional_stop": False,
        }
        self._subprocesses[sid] = entry
        logger.info("Started %s (pid %d) on port %d", name, proc.pid, port)
        return entry

    def _start_mcp_subprocesses(self):
        """Launch every mcp_servers entry whose process we own (google_mcp /
        jira_mcp / http_command). Called once at startup."""
        self._subprocesses = {}
        for server in self._config.get("mcp_servers", []) or []:
            if server.get("kind") != "subprocess":
                continue
            self._launch_one_subprocess(server)
        atexit.register(self._stop_mcp_subprocesses)

    def _stop_mcp_subprocesses(self):
        for entry in list(getattr(self, "_subprocesses", {}).values()):
            self._terminate_entry(entry)

    @staticmethod
    def _terminate_entry(entry: dict):
        proc = entry.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        fh = entry.get("log_fh")
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
            entry["log_fh"] = None

    # ── Per-server control (called from the settings bridge) ─────────────────
    #
    # Only HTTP-subprocess servers (templates in _CONTROLLABLE_TEMPLATES) can be
    # controlled: the proxy connects to http://localhost:{port}/mcp per request
    # (ResilientFastMCPProxy reconnects), so killing/relaunching the subprocess
    # is live — no proxy rebuild needed. Operations act on the running/saved
    # config (self._config); unsaved edits in the settings form don't apply
    # until Save + restart.

    def _find_subprocess_server(self, server_id: str) -> dict | None:
        for server in self._config.get("mcp_servers", []) or []:
            if _server_id(server) == server_id and _is_controllable(server):
                return server
        return None

    def start_mcp_server(self, server_id: str) -> dict:
        server = self._find_subprocess_server(server_id)
        if server is None:
            return {"id": server_id, "state": "unknown",
                    "error": "not a controllable subprocess server"}
        entry = getattr(self, "_subprocesses", {}).get(server_id)
        if entry and entry.get("proc") and entry["proc"].poll() is None:
            return self.mcp_server_status(server_id)  # already running
        self._launch_one_subprocess(server)
        return self.mcp_server_status(server_id)

    def stop_mcp_server(self, server_id: str) -> dict:
        entry = getattr(self, "_subprocesses", {}).get(server_id)
        if entry:
            entry["intentional_stop"] = True
            self._terminate_entry(entry)
        return self.mcp_server_status(server_id)

    def restart_mcp_server(self, server_id: str) -> dict:
        self.stop_mcp_server(server_id)
        server = self._find_subprocess_server(server_id)
        if server is not None:
            self._launch_one_subprocess(server)
        return self.mcp_server_status(server_id)

    def mcp_server_status(self, server_id: str) -> dict:
        server = self._find_subprocess_server(server_id)
        controllable = server is not None
        entry = getattr(self, "_subprocesses", {}).get(server_id)
        out = {"id": server_id, "controllable": controllable,
               "pid": None, "code": None}
        if not entry or entry.get("proc") is None:
            out["state"] = "stopped"
            return out
        proc = entry["proc"]
        rc = proc.poll()
        if rc is None:
            out["state"] = "running"
            out["pid"] = proc.pid
        else:
            out["code"] = rc
            out["state"] = "stopped" if entry.get("intentional_stop") else "crashed"
        return out

    def read_mcp_server_log(self, server_id: str, max_bytes: int = 65536) -> str:
        """Tail of the server's captured stdout+stderr (last ~max_bytes)."""
        log_path = _mcp_log_path(server_id)
        try:
            size = os.path.getsize(log_path)
            with open(log_path, "r", errors="replace") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    f.readline()  # drop partial first line
                text = f.read()
        except FileNotFoundError:
            return "(no output captured yet — server hasn't been started)"
        except Exception as e:
            return f"(could not read log: {e})"
        return text or "(log is empty)"

    # ── Claude link arm/disarm lifecycle ─────────────────────────────────────
    #
    # User intent (claude_link.armed in apps.json) is set/cleared by the
    # Connect/Disconnect button. While we're running, we *enforce* that
    # intent: on start, re-arm if intent==True and we're not already wired;
    # on quit, disarm regardless of current state. A crash/SIGKILL leaves
    # Claude armed for one session — next start re-evaluates and converges.

    def _rearm_claude_link_if_intended(self):
        """Re-wire ~/.claude/settings.json at startup if the user's last
        recorded intent was 'armed'. Idempotent: skips if already wired."""
        if not self.claude_link_armed:
            return
        try:
            from claude_link import (
                load_claude_settings, is_voitta_connected,
                plan_connect, apply_changes,
            )
            cfg = load_claude_settings()
            if is_voitta_connected(cfg, self.llm_proxy_port):
                return  # nothing to do — already armed
            plan = plan_connect(cfg, self.llm_proxy_port, self.llm_upstream_url)
            if plan.is_noop:
                return
            apply_changes(plan)
            logger.info("claude_link: re-armed on startup")
        except Exception as e:
            logger.warning("claude_link: re-arm failed: %s", e)

    def _disarm_claude_link_on_quit(self):
        """Strip Voitta from ~/.claude/settings.json on quit. Best-effort —
        never raises (any failure is logged and swallowed so it can't block
        shutdown). Always runs, regardless of the armed flag, so a user who
        force-quit after toggling Disconnect still gets a clean settings.json."""
        try:
            from claude_link import (
                load_claude_settings, is_voitta_connected,
                plan_disconnect, apply_changes,
            )
            cfg = load_claude_settings()
            if not is_voitta_connected(cfg, self.llm_proxy_port):
                return  # not wired, nothing to strip
            plan = plan_disconnect(cfg, self.llm_proxy_port)
            if plan.is_noop:
                return
            apply_changes(plan)
            logger.info("claude_link: disarmed on quit")
        except Exception as e:
            logger.warning("claude_link: disarm failed: %s", e)
