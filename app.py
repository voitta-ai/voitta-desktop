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

import paths


def _wire_ca_bundle() -> None:
    """Point Python's SSL machinery at certifi's CA bundle.

    Briefcase's standalone Python (what ships inside the .app) has no
    /etc/ssl/cert.pem and no SSL_CERT_FILE in its env, so aiohttp's TLS
    handshake to api.anthropic.com fails with
    "unable to get local issuer certificate". On terminal dev the system
    Python or venv certifi resolves this implicitly; in the bundle we must
    do it ourselves. Setting SSL_CERT_FILE / SSL_CERT_DIR before any
    aiohttp / ssl import is the simplest fix that requires no proxy-side
    change. Harmless when the vars are already set or on systems where
    Python found a bundle on its own.
    """
    import os
    try:
        import certifi
    except ImportError:
        return  # CLI dev without certifi installed — let system Python handle it
    bundle = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(bundle))
    # requests / urllib3 use their own env var
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description="Voitta Desktop")
    p.add_argument(
        "--terminal",
        action="store_true",
        help="Run as a terminal (Textual) app instead of the macOS menu bar app.",
    )
    return p.parse_args()


def _configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(levelname)s: %(message)s")
    file_handler = RotatingFileHandler(log_dir / "desktop.log", maxBytes=5_000_000, backupCount=3)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger("fastmcp.server.providers.aggregate").setLevel(logging.DEBUG)
    logging.getLogger("voitta-desktop.tracker").setLevel(logging.DEBUG)
    logging.getLogger("voitta-desktop.proxy").setLevel(logging.DEBUG)
    # Root sits at WARNING, so these would otherwise drop their INFO lines —
    # including "clean exit", which is exactly what distinguishes a normal
    # quit from a silent kill when reading back a crash.
    logging.getLogger("voitta-desktop.lifecycle").setLevel(logging.DEBUG)
    logging.getLogger("voitta-desktop.runtime").setLevel(logging.DEBUG)
    logging.getLogger("voitta-desktop.object_store").setLevel(logging.INFO)
    logging.getLogger("voitta-desktop.paths").setLevel(logging.INFO)
    # The gate's own decisions were invisible at WARNING: an OK that disabled
    # nothing, a JS failure that fell back to disabling nothing, and a silent
    # Cancel were indistinguishable in the log — while all three lead to very
    # different tool exposure.
    logging.getLogger("voitta-desktop.tool_gate").setLevel(logging.INFO)
    # Silence MCP SDK post-cancellation SSE spam.
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.CRITICAL)


def main() -> None:
    """Configure logging, load .env (if present), and start the app."""
    import sys
    import platform

    _wire_ca_bundle()

    args = _parse_args()

    if not args.terminal and platform.system() != "Darwin":
        print(
            "Error: Voitta Desktop's menu bar mode requires macOS.\n"
            "On Linux, run with --terminal flag:\n\n"
            "    python -m voitta_desktop --terminal\n",
            file=sys.stderr,
        )
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv()

    # Unify the on-disk tree before anything reads config or writes a log.
    paths.migrate_legacy_dirs()
    _configure_logging(paths.LOG_DIR)

    # Install crash diagnostics immediately after logging is up, so an early
    # failure still gets recorded. See lifecycle.py for why both a signal
    # handler and an on-disk marker are needed.
    import lifecycle
    lifecycle.install()

    if args.terminal:
        from ui.tui.app import TUIApp
        TUIApp().run()
    else:
        from ui.menu import VoittaDesktopApp
        VoittaDesktopApp().run()


if __name__ == "__main__":
    main()
