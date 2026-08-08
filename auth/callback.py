"""OAuth2 redirect callback handler.

A one-shot HTTP listener the provider redirects back to after the user
signs in. It used to be a blocking ``http.server.HTTPServer`` that occupied
a thread of its own for up to two minutes; it is now an aiohttp site on the
shared runtime, so it costs a coroutine and cancels cleanly.

The port is deliberately unchanged. It appears in the redirect URI
registered with Google and Microsoft, so moving it would require editing
those app registrations.
"""

import asyncio
import logging

from aiohttp import web

from config import load_config

logger = logging.getLogger("voitta-desktop.auth")

REDIRECT_PORT = int(load_config().get("oauth", {}).get("redirect_port", 53214))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"

DEFAULT_TIMEOUT_S = 120.0

_OK_HTML = "<html><body><h2>Authenticated! You can close this tab.</h2></body></html>"


def _error_html(message: str) -> str:
    return f"<html><body><h2>Error: {message}</h2></body></html>"


async def wait_for_callback_async(
    port: int = REDIRECT_PORT, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[str | None, str | None]:
    """Serve a single OAuth redirect and return ``(code, error)``."""
    answered: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handle(request: web.Request) -> web.Response:
        code = request.query.get("code")
        error = request.query.get("error_description") or request.query.get("error")
        if not answered.done():
            answered.set_result((code, error))
        body = _OK_HTML if code else _error_html(error or "Unknown error")
        return web.Response(text=body, content_type="text/html")

    app = web.Application()
    # Providers append their own path and query; accept anything.
    app.router.add_get("/{tail:.*}", handle)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        logger.error("could not listen for OAuth callback on port %d: %s", port, e)
        return None, f"port {port} unavailable: {e}"

    logger.info("waiting for OAuth callback on port %d", port)
    try:
        return await asyncio.wait_for(answered, timeout)
    except asyncio.TimeoutError:
        logger.warning("OAuth callback timed out after %.0fs", timeout)
        return None, "timed out waiting for the OAuth redirect"
    finally:
        await runner.cleanup()


def wait_for_callback(
    port: int = REDIRECT_PORT, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[str | None, str | None]:
    """Blocking wrapper for the synchronous sign-in flows in providers.py.

    Must not be called from the runtime's own loop thread — it waits on a
    coroutine scheduled there. The sign-in flows run on the runtime's
    blocking pool, which is a different thread, so this is safe.
    """
    from runtime import runtime

    return runtime.submit(wait_for_callback_async(port, timeout)).result()
