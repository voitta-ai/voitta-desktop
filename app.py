#!/usr/bin/env python3
"""Voitta Desktop — macOS menu bar app fusing LLM proxy and MCP auth proxy."""
# Entry point: configures logging and launches the menu bar application

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

LOG_DIR = Path.home() / ".voitta-desktop" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Console: WARNING+, File: DEBUG+ with rotation
logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(levelname)s: %(message)s")
file_handler = RotatingFileHandler(LOG_DIR / "desktop.log", maxBytes=5_000_000, backupCount=3)
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

if __name__ == "__main__":
    VoittaDesktopApp().run()
