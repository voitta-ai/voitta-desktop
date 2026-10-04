#!/usr/bin/env python3
"""Voitta's LLM proxy stack (all middleware + LLM accounts) without any UI.

For the e2e script: run with VOITTA_DESKTOP_HOME pointing at a scratch
folder so nothing touches the real config, accounts or ~/.claude.

    VOITTA_DESKTOP_HOME=/tmp/x python tests/llm_headless_proxy.py <port> [as-is upstream url]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app_base import AppBase  # noqa: E402


class Headless(AppBase):
    terminal_mode = True

    def __init__(self, port: int, upstream: str | None):
        self._init_base()
        self.llm_proxy_port = port
        if upstream:
            self.llm_upstream_url = upstream
        self._build_proxy_stack()


async def main():
    port = int(sys.argv[1])
    app = Headless(port, sys.argv[2] if len(sys.argv) > 2 else None)
    await app._proxy.start()
    print(f"llm proxy on {port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
