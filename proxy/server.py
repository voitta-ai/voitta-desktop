"""Reverse proxy for Anthropic API with middleware pipeline and SSE streaming."""

import asyncio
import json
import logging
import re
import time
from urllib.parse import urlparse
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector

from middleware import Middleware, ProxyRequest, ProxyResponse

# Regex to normalize volatile cch= hash in billing header so caching works
_CCH_RE = re.compile(r'cch=[0-9a-f]+')

logger = logging.getLogger("voitta-desktop.proxy")

# Headers to NOT forward between client and upstream
HOP_BY_HOP = frozenset({
    "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "upgrade", "host", "content-length",
})

DEFAULT_UPSTREAM_URL = "https://api.anthropic.com"


class AnthropicProxy:
    """Reverse proxy that forwards requests to an Anthropic-compatible upstream."""

    def __init__(
        self,
        middlewares: list[Middleware] | None = None,
        port: int = 18900,
        upstream_url: str = DEFAULT_UPSTREAM_URL,
    ):
        self.middlewares = middlewares or []
        self.port = port
        self.upstream_url = (upstream_url or DEFAULT_UPSTREAM_URL).rstrip("/")
        self._upstream_host = urlparse(self.upstream_url).netloc
        self._session: ClientSession | None = None
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    async def start(self):
        """Start the proxy server."""
        self._session = ClientSession(
            connector=TCPConnector(limit=20),
            timeout=ClientTimeout(
                total=None,       # no overall deadline — streams can run for minutes
                sock_read=600,    # 10 min between chunks — thinking can go silent for a while
                sock_connect=30,  # 30s to establish connection
            ),
            auto_decompress=False,
        )
        self._app = web.Application(client_max_size=0)
        self._app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        logger.info("LLM proxy listening on http://127.0.0.1:%d", self.port)

    async def stop(self):
        """Shut down the proxy server."""
        if self._session:
            await self._session.close()
        if self._runner:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """Handle an incoming request: run middleware, forward, stream back."""
        try:
            return await self._handle_inner(request)
        except Exception as e:
            logger.error("Unhandled error in proxy handler: %s", e, exc_info=True)
            return web.Response(status=502, text=f"Proxy error: {e}")

    async def _handle_inner(self, request: web.Request) -> web.StreamResponse:
        started_at = time.monotonic()
        body = await request.read()

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        proxy_req = ProxyRequest(
            method=request.method,
            path=request.path_qs,
            headers=headers,
            body=body,
        )

        recv_kb = len(body) / 1024
        logger.debug("VOL recv %s %.1f KB", request.path.split("?")[0], recv_kb)

        # Every middleware that completes on_request MUST get a matching
        # on_response_done — otherwise the request leaks into RequestLogger's
        # _pending dict and the watchdog reports it forever (we've seen a 49h
        # leak from upstream-failure and middleware-failure exit paths).
        started_mws: list[Middleware] = []
        proxy_resp: ProxyResponse | None = None
        try:
            # Run request middleware
            for mw in self.middlewares:
                mw_started_at = time.monotonic()
                try:
                    proxy_req = await mw.on_request(proxy_req)
                except Exception as e:
                    logger.error("Middleware %s.on_request failed: %s", type(mw).__name__, e, exc_info=True)
                    return web.Response(status=502, text=f"Middleware error: {e}")
                started_mws.append(mw)
                mw_duration_ms = int((time.monotonic() - mw_started_at) * 1000)
                if mw_duration_ms >= 250:
                    logger.info("Middleware %s.on_request took %d ms for %s",
                                type(mw).__name__, mw_duration_ms, proxy_req.path)

            # Forward to upstream
            upstream_url = f"{self.upstream_url}{proxy_req.path}"
            upstream_headers = dict(proxy_req.headers)
            upstream_headers["Host"] = self._upstream_host
            if proxy_req.body:
                upstream_headers["Content-Length"] = str(len(proxy_req.body))

            sent_kb = len(proxy_req.body or b"") / 1024
            logger.debug("VOL send %s %.1f KB (delta %+.1f KB)",
                         proxy_req.path.split("?")[0], sent_kb, sent_kb - recv_kb)

            try:
                upstream_resp = await self._session.request(
                    method=proxy_req.method,
                    url=upstream_url,
                    headers=upstream_headers,
                    data=proxy_req.body,
                )
                logger.debug("VOL resp %s -> %d (%s)",
                             proxy_req.path.split("?")[0], upstream_resp.status,
                             upstream_resp.headers.get("Content-Type", "unknown"))
            except Exception as e:
                logger.error("Upstream request failed: %s", e)
                return web.Response(status=502, text=f"Upstream error: {e}")

            resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP}
            proxy_resp = ProxyResponse(status=upstream_resp.status, headers=resp_headers)

            # Run response-started middleware
            for mw in self.middlewares:
                mw_started_at = time.monotonic()
                proxy_resp = await mw.on_response_started(proxy_req, proxy_resp)
                mw_duration_ms = int((time.monotonic() - mw_started_at) * 1000)
                if mw_duration_ms >= 250:
                    logger.info("Middleware %s.on_response_started took %d ms for %s",
                                type(mw).__name__, mw_duration_ms, proxy_req.path)

            content_type = upstream_resp.headers.get("Content-Type", "")
            is_streaming = "text/event-stream" in content_type

            if is_streaming:
                return await self._stream_response(request, proxy_req, proxy_resp, upstream_resp, started_at)
            else:
                return await self._buffered_response(proxy_req, proxy_resp, upstream_resp, started_at)
        finally:
            # Synthetic 502 for paths that bailed before we built proxy_resp;
            # keeps downstream middlewares' contract intact (always paired).
            resp_for_done = proxy_resp if proxy_resp is not None else ProxyResponse(status=502, headers={})
            for mw in started_mws:
                try:
                    await mw.on_response_done(proxy_req, resp_for_done)
                except Exception as e:
                    logger.error("Middleware %s.on_response_done failed for %s: %s",
                                 type(mw).__name__, proxy_req.path, e, exc_info=True)

    async def _stream_response(
        self, request: web.Request, proxy_req: ProxyRequest,
        proxy_resp: ProxyResponse, upstream_resp, started_at: float
    ) -> web.StreamResponse:
        """Stream SSE response chunk by chunk."""
        response = web.StreamResponse(
            status=proxy_resp.status,
            headers=proxy_resp.headers,
        )
        await response.prepare(request)
        logger.info("Streaming response started for %s", proxy_req.path)

        chunk_count = 0
        byte_count = 0
        try:
            async for chunk in upstream_resp.content.iter_any():
                chunk_count += 1
                byte_count += len(chunk)
                for mw in self.middlewares:
                    chunk = await mw.on_response_chunk(proxy_req, chunk)
                await response.write(chunk)
        except ConnectionResetError:
            logger.debug("Client disconnected during streaming")
        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.warning("Upstream timeout during streaming for %s after %d chunks (%.1f KB): %s",
                           proxy_req.path, chunk_count, byte_count / 1024, e)
        except Exception as e:
            logger.error("Streaming failed for %s: %s", proxy_req.path, e, exc_info=True)
        finally:
            try:
                await response.write_eof()
            except ConnectionResetError:
                logger.debug("Client disconnected before write_eof for %s", proxy_req.path)
            duration_ms = int((time.monotonic() - started_at) * 1000)
            logger.debug("VOL done %s %.1f KB in %d ms (stream, %d chunks)",
                         proxy_req.path.split("?")[0], byte_count / 1024, duration_ms, chunk_count)

        return response

    async def _buffered_response(
        self, proxy_req: ProxyRequest, proxy_resp: ProxyResponse, upstream_resp, started_at: float
    ) -> web.Response:
        """Read full response body then return."""
        body = await upstream_resp.read()

        for mw in self.middlewares:
            mw_started_at = time.monotonic()
            body = await mw.on_response_chunk(proxy_req, body)
            mw_duration_ms = int((time.monotonic() - mw_started_at) * 1000)
            if mw_duration_ms >= 250:
                logger.info("Middleware %s.on_response_chunk took %d ms for %s",
                            type(mw).__name__, mw_duration_ms, proxy_req.path)

        duration_ms = int((time.monotonic() - started_at) * 1000)
        logger.debug("VOL done %s %.1f KB in %d ms (buffered)",
                     proxy_req.path.split("?")[0], len(body) / 1024, duration_ms)

        return web.Response(
            status=proxy_resp.status,
            headers=proxy_resp.headers,
            body=body,
        )
