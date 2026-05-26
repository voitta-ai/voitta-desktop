"""MCP subprocess + Claude-link lifecycle for VoittaDesktopApp.

Subprocess launching is data-driven from ``self._config['mcp_servers']``:
each entry with ``kind=subprocess`` and ``subprocess.template ∈
{google_mcp, jira_mcp}`` triggers the matching launch + .env-sync flow.

Two templates are supported in v1 — Google Workspace MCP and Jira MCP.
Adding a third means adding a branch in ``_start_mcp_subprocesses`` and
(optionally) a sync hook for its .env file. The UI doesn't yet let users
create custom subprocess templates from scratch; those flow through code.
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
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

    def _start_mcp_subprocesses(self):
        """Launch every mcp_servers entry with kind=subprocess. Currently
        supports two templates: google_mcp and jira_mcp. Each template
        knows its command shape — only cwd/env_path/port flow through
        from config."""
        self._subprocesses = []

        # Bundle launchctl strips PATH to the minimal /usr/bin:/bin:/usr/sbin:/sbin,
        # so Homebrew-installed tools (uv, uvx) aren't found. Extend PATH with
        # the standard install locations before launching subprocesses. CLI dev
        # already has these, so no-op there.
        extra_path = ":".join([
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/.cargo/bin"),
        ])
        base_env = {**os.environ, "PATH": f"{extra_path}:{os.environ.get('PATH', '')}"}

        for server in self._config.get("mcp_servers", []) or []:
            if server.get("kind") != "subprocess":
                continue
            sp = server.get("subprocess") or {}
            template = sp.get("template", "")
            name = server.get("name") or server.get("prefix", "")
            cwd = os.path.expanduser(sp.get("cwd", "") or "")
            env_path = os.path.expanduser(sp.get("env_path", "") or "")
            port = int(sp.get("port", 0) or 0)

            if template == "google_mcp":
                if not cwd or not Path(cwd).is_dir():
                    logger.info("subprocess %r skipped: cwd %r missing", name, cwd)
                    continue
                if not port:
                    logger.warning("subprocess %r skipped: no port", name)
                    continue
                try:
                    env = {**base_env, "PORT": str(port)}
                    proc = subprocess.Popen(
                        ["uv", "run", "main.py", "--transport", "streamable-http"],
                        cwd=cwd, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    self._subprocesses.append(proc)
                    logger.info("Started %s (pid %d) on port %d", name, proc.pid, port)
                except Exception as e:
                    logger.warning("Failed to start %s: %s", name, e)

            elif template == "jira_mcp":
                if not cwd or not Path(cwd).is_dir():
                    logger.info("subprocess %r skipped: cwd %r missing", name, cwd)
                    continue
                if not env_path or not Path(env_path).exists():
                    logger.info("subprocess %r skipped: env_path %r missing", name, env_path)
                    continue
                if not port:
                    logger.warning("subprocess %r skipped: no port", name)
                    continue
                try:
                    proc = subprocess.Popen(
                        ["uvx", "mcp-atlassian", "--transport", "streamable-http",
                         "--port", str(port), "--env-file", env_path],
                        cwd=cwd, env=base_env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    self._subprocesses.append(proc)
                    logger.info("Started %s (pid %d) on port %d", name, proc.pid, port)
                except Exception as e:
                    logger.warning("Failed to start %s: %s", name, e)

            elif template in ("npx", "command"):
                # Stdio servers: fastmcp owns the process lifecycle via
                # NpxStdioTransport / StdioTransport — nothing to launch here.
                logger.info("subprocess %r: stdio transport, managed by fastmcp", name)

            else:
                logger.warning("subprocess %r: unknown template %r", name, template)

        atexit.register(self._stop_mcp_subprocesses)

    def _stop_mcp_subprocesses(self):
        for proc in getattr(self, "_subprocesses", []):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

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
