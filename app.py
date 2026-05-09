#!/usr/bin/env python3
"""Voitta Desktop — macOS menu bar app fusing LLM proxy and MCP auth proxy.

Entry point: configures logging and launches the menu bar application.

Two callers:
  • Terminal dev:  `python app.py`         — runs main() under __name__
  • Briefcase:     `src/voitta_desktop/__main__.py` imports main()
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def main() -> None:
    """Configure logging, load .env (if present), and start the app."""
    # .env is dev-only convenience; in the bundled .app there is no .env on
    # disk and load_dotenv silently no-ops, which is what we want.
    from dotenv import load_dotenv
    load_dotenv()

    log_dir = Path.home() / ".voitta-desktop" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Console: WARNING+, File: DEBUG+ with rotation
    logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(levelname)s: %(message)s")
    file_handler = RotatingFileHandler(log_dir / "desktop.log", maxBytes=5_000_000, backupCount=3)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)

    # Surface FastMCP proxy errors
    logging.getLogger("fastmcp.server.providers.aggregate").setLevel(logging.DEBUG)
    logging.getLogger("voitta-desktop.tracker").setLevel(logging.DEBUG)
    logging.getLogger("voitta-desktop.proxy").setLevel(logging.DEBUG)

    # Silence the MCP SDK's post-cancellation SSE spam. When we cancel a slow
    # listing via asyncio.wait_for, the SSE reader is mid-flight and tries to
    # write into a stream that's already closed -> ClosedResourceError stacks.
    # Cosmetic only; the timeout fallback handles the actual outcome.
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.CRITICAL)

    from ui.menu import VoittaDesktopApp
    VoittaDesktopApp().run()


if __name__ == "__main__":
    main()
