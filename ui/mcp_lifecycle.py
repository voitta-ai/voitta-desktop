"""MCP subprocess + Claude-link lifecycle for VoittaDesktopApp.

Holds:

  • Module-level constants for the MCP subprocess paths/ports (read at
    process start from apps.json, no live reload). menu.py imports them
    back from here so the dependency direction is one-way.

  • ``MCPLifecycleMixin`` — methods that manage:
      - ``.env`` sync for the Google Workspace + Jira MCP subprocesses,
      - launching/stopping those subprocesses,
      - arming/disarming ``~/.claude/settings.json`` to point at our
        local LLM proxy across the app's lifetime.
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from config import load_config, CONFIG_DIR

logger = logging.getLogger("voitta-desktop")

# ── Subprocess + OAuth settings (sourced from apps.json; env seeds defaults) ─
# Read once at module import. menu.py exposes these as module-level so other
# code (show_help, _run_mcp_proxy, _apply_settings default fallback) can use
# them too.

_startup_cfg = load_config()
_oauth_cfg = _startup_cfg.get("oauth", {})
_sub_cfg = _startup_cfg.get("mcp_subprocess", {})

OAUTH_REDIRECT_PORT = int(_oauth_cfg.get("redirect_port", 53214))
GOOGLE_MCP_PORT = int(_sub_cfg.get("google_mcp_port", 18766))
JIRA_MCP_PORT = int(_sub_cfg.get("jira_mcp_port", 18767))
GOOGLE_MCP_DIR = os.path.expanduser(_sub_cfg.get("google_mcp_dir", "~/DEVEL/google_workspace_mcp"))
GOOGLE_MCP_ENV_PATH = os.path.expanduser(_sub_cfg.get("google_mcp_env_path", "~/DEVEL/google_workspace_mcp/.env"))
JIRA_MCP_DIR = os.path.expanduser(_sub_cfg.get("jira_mcp_dir", "~/DEVEL/mcp-atlassian"))
JIRA_MCP_ENV_PATH = os.path.expanduser(_sub_cfg.get("jira_mcp_env_path", str(CONFIG_DIR / "jira.env")))


class MCPLifecycleMixin:
    """Mixin: MCP subprocess + Claude-link arm/disarm for ``VoittaDesktopApp``.

    Methods depend on ``self._config``, ``self.edit_proxy_url``,
    ``self.claude_link_armed``, ``self.llm_proxy_port``,
    ``self.llm_upstream_url``, ``self._subprocesses``.
    """

    # ── MCP .env sync ────────────────────────────────────────────────────────

    def _sync_edit_mcp_env(self):
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
            Path(GOOGLE_MCP_ENV_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(GOOGLE_MCP_ENV_PATH, "w") as f:
                f.write("\n".join(lines))
        except Exception as e:
            logger.warning("Failed to write edit MCP .env: %s", e)

    def _sync_jira_mcp_env(self):
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
            Path(JIRA_MCP_ENV_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(JIRA_MCP_ENV_PATH, "w") as f:
                f.write("\n".join(lines))
        except Exception as e:
            logger.warning("Failed to write Jira MCP .env: %s", e)

    # ── MCP subprocesses ─────────────────────────────────────────────────────

    def _start_mcp_subprocesses(self):
        self._subprocesses = []

        # Bundle launchctl strips PATH to the minimal /usr/bin:/bin:/usr/sbin:/sbin,
        # so Homebrew-installed tools (uv, uvx) aren't found. Extend PATH with
        # the standard install locations before launching subprocesses. CLI dev
        # already has these, so no-op there.
        extra_path = ":".join([
            "/opt/homebrew/bin",   # arm64 Homebrew
            "/usr/local/bin",      # x86_64 Homebrew / manual installs
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/.cargo/bin"),
        ])
        base_env = {**os.environ, "PATH": f"{extra_path}:{os.environ.get('PATH', '')}"}

        if Path(GOOGLE_MCP_DIR).is_dir():
            try:
                google_port = str(urlparse(self.edit_proxy_url).port or GOOGLE_MCP_PORT)
                env = {**base_env, "PORT": google_port}
                proc = subprocess.Popen(
                    ["uv", "run", "main.py", "--transport", "streamable-http"],
                    cwd=GOOGLE_MCP_DIR, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._subprocesses.append(proc)
                logger.info("Started google_workspace_mcp (pid %d)", proc.pid)
            except Exception as e:
                logger.warning("Failed to start google_workspace_mcp: %s", e)

        if Path(JIRA_MCP_DIR).is_dir() and Path(JIRA_MCP_ENV_PATH).exists():
            try:
                proc = subprocess.Popen(
                    [
                        "uvx", "mcp-atlassian",
                        "--transport", "streamable-http",
                        "--port", str(JIRA_MCP_PORT),
                        "--env-file", JIRA_MCP_ENV_PATH,
                    ],
                    cwd=JIRA_MCP_DIR, env=base_env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._subprocesses.append(proc)
                logger.info("Started mcp-atlassian (pid %d) on port %d", proc.pid, JIRA_MCP_PORT)
            except Exception as e:
                logger.warning("Failed to start mcp-atlassian: %s", e)

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
