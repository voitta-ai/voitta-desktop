"""Shared app_ref interface used by mcpproxy, middleware, and proxy.

Both VoittaDesktopApp (Mac) and TUIApp (terminal) inherit from this.
It holds all state the background servers need to read/write, plus a
notify_update() hook that each driver implements to push live updates
to its UI.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from config import load_config, save_config, CONFIG_DIR, CONFIG_PATH

logger = logging.getLogger("voitta-desktop")


class AppBase:
    """Minimal shared state and lifecycle for Voitta Desktop drivers.

    Subclasses must implement:
        _start_ui()      — block until the UI exits
        notify_update()  — called from proxy/middleware threads to push
                           a live update to the UI
    """

    # Set to True by terminal driver; read by ToolGateMiddleware and
    # config helpers to skip Mac-only code paths.
    terminal_mode: bool = False

    def _init_base(self) -> None:
        """Call from subclass __init__ after super().__init__() returns."""
        self._auth_lock = threading.Lock()
        self._auth: dict = {}

        self._config = self._load_or_migrate_config()
        mcp_proxy_cfg = self._config.get("mcp_proxy", {})
        llm_proxy_cfg = self._config.get("llm_proxy", {})

        self.mcp_servers: list[dict] = list(self._config.get("mcp_servers", []))
        self.mcp_proxy_port: int = mcp_proxy_cfg.get("port", 18765)
        self.llm_proxy_port: int = llm_proxy_cfg.get("port", 18900)
        self.llm_upstream_url: str = llm_proxy_cfg.get(
            "upstream_url", "https://api.anthropic.com"
        )

        self.disabled_tools: set[str] = set(self._config.get("disabled_tools", []))
        tools_cfg = self._config.get("tools", {})
        self.suppress_codex_popup: bool = bool(
            tools_cfg.get("suppress_codex_popup", True)
        )
        link_cfg = self._config.get("claude_link", {})
        self.claude_link_armed: bool = bool(link_cfg.get("armed", True))

        self._mcp_tools: dict[str, list[str]] = {}
        self._mcp_upstream_instructions: dict[str, str] = {}
        self._mcp_backends: list = []
        self._active_app: dict = {}

    # ── Config ───────────────────────────────────────────────────────

    def _load_or_migrate_config(self) -> dict:
        from config import migrate_from_legacy
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            legacy = Path.home() / ".voitta_auth_settings.json"
            if legacy.exists():
                migrate_from_legacy(legacy, CONFIG_PATH)
        return load_config()

    def _save_config(self) -> None:
        save_config(self._config)

    # ── Ports ────────────────────────────────────────────────────────

    def _is_port_free(self, port: int) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _grab_free_port(self) -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _resolve_port_terminal(self, label: str, port: int) -> int:
        if self._is_port_free(port):
            return port
        new_port = self._grab_free_port()
        logger.warning(
            "%s port %d in use; using OS-assigned port %d", label, port, new_port
        )
        return new_port

    # ── Live UI hook — subclasses override ───────────────────────────

    def notify_update(self) -> None:
        """Called from background threads when conversation state changes."""

    # ── Proxy/middleware factories — identical for both drivers ──────

    def _build_proxy_stack(self):
        """Build and return (proxy, tracker, optimizer_pipeline, cache_sim).
        Stored on self for both drivers to share identical middleware setup.
        """
        from middleware import ConversationTracker, RequestLogger
        from middleware.cache_sim import CacheSimulator
        from optimizers import OptimizerPipeline
        from optimizers.bash_compress import BashCompressor
        from optimizers.image import ImageOptimizer
        from optimizers.thinking import ThinkingOptimizer
        from optimizers.tool_result import ToolResultOptimizer
        from proxy import AnthropicProxy

        time_cfg = self._config.get("time", {})
        bash_cfg = self._config.get("bash", {})
        opt_cfg = self._config.get("optimizer", {})

        self._tracker = ConversationTracker()
        self._tracker.app_ref = self  # for notify_update callbacks
        self._request_logger = RequestLogger()
        self._tool_result_optimizer = ToolResultOptimizer(
            keep_turns=int(time_cfg.get("tool_result_keep_turns", 5))
        )
        self._image_optimizer = ImageOptimizer(
            keep_turns=int(time_cfg.get("image_keep_turns", 5))
        )
        self._thinking_optimizer = ThinkingOptimizer(
            keep_turns=int(time_cfg.get("thinking_keep_turns", 5))
        )
        self._bash_compressor = BashCompressor(
            strip_ansi=bool(bash_cfg.get("strip_ansi", True)),
            trim_whitespace=bool(bash_cfg.get("trim_whitespace", True)),
            strip_progress=bool(bash_cfg.get("strip_progress", False)),
            smart_commands=bool(bash_cfg.get("smart_commands", False)),
        )
        self._optimizer_pipeline = OptimizerPipeline(
            [
                self._bash_compressor,
                self._tool_result_optimizer,
                self._image_optimizer,
                self._thinking_optimizer,
            ],
            enabled=bool(opt_cfg.get("enabled", True)),
            haiku_only=bool(opt_cfg.get("haiku_only", False)),
        )
        self._cache_sim = CacheSimulator()
        self._proxy = AnthropicProxy(
            middlewares=[
                self._request_logger,
                self._tracker,
                self._optimizer_pipeline,
                self._cache_sim,
            ],
            port=self.llm_proxy_port,
            upstream_url=self.llm_upstream_url,
        )
        self._proxy_running = False

    def _run_llm_proxy(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._proxy.start())
            self._proxy_running = True
            logger.info("LLM proxy started on port %d", self.llm_proxy_port)
            loop.run_forever()
        except Exception as e:
            logger.error("LLM proxy failed to start: %s", e, exc_info=True)
            self._proxy_running = False

    def _run_mcp_proxy(self) -> None:
        from mcpproxy.server import run_mcp_proxy
        try:
            run_mcp_proxy(self, self.mcp_proxy_port)
        except Exception as e:
            logger.error("MCP proxy crashed: %s", e, exc_info=True)
