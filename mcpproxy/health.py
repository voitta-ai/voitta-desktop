"""Watch the MCP server, show when it is broken, and rebuild it once.

On 2026-10-13 the MCP endpoint answered every request with HTTP 500 for hours
after its StreamableHTTP session manager died (see VoittaStatefulProxyClient
in resilient.py for the cause). Nothing showed it: uvicorn wrote the errors
to stderr, which the app drops, and the menu bar looked normal.

* ``HealthWatcher`` wraps the MCP ASGI app and records how every request
  ended. It only observes; responses and exceptions pass through unchanged.
* ``McpHealth`` turns that into a state the menu bar reads: ``broken`` after
  3 server errors in a row or a self-check that failed twice, ``healthy``
  again on the next successful request.
* ``McpServerHost`` runs the server itself (fastmcp's app under our own
  uvicorn.Server, so it can be stopped and started in place, and so uvicorn
  logs to desktop.log), checks it once a minute when no request succeeded,
  and rebuilds it on the same port when it breaks. It rebuilds at most once
  per breakage and never twice within 10 minutes; past that it stays broken
  (red) and says so, rather than looping.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time

import httpx
import uvicorn
from starlette.middleware import Middleware

logger = logging.getLogger("voitta-desktop.mcp")

BROKEN_AFTER = 3              # server errors in a row
CHECK_EVERY_S = 60.0          # self-check when no request succeeded for this long
TICK_S = 5.0
RECHECK_AFTER_S = 5.0         # a failed self-check is repeated once before it counts
PROBE_TIMEOUT_S = 10.0
REBUILD_COOLOFF_S = 600.0     # never rebuild twice within this


def _clear_sse_shutdown_flag(stopped: uvicorn.Server):
    """sse_starlette keeps ONE process-wide "shutting down" flag
    (AppStatus.should_exit). Stopping the old server can set it — directly,
    or through its watcher, which finds a uvicorn server via the SIGTERM
    handler — and every response stream of the new server would then end
    before its reply is written. Clear both once the old server is gone."""
    stopped.should_exit = False  # finished; this only stops a watcher re-raising the flag
    try:
        from sse_starlette.sse import AppStatus
    except ImportError:
        return
    AppStatus.should_exit = False


def _hhmm(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


class McpHealth:
    """The MCP server's state, written on the event loop, read by the menu bar."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.state = "starting"          # starting | healthy | broken
        self.consecutive = 0             # server errors in a row
        self.since: float | None = None  # when the current breakage began
        self.failures = 0                # failed requests during it
        self.last_error: str | None = None
        self.last_ok: float | None = None
        self.note: str | None = None     # e.g. why it won't rebuild again
        self.recovered: dict | None = None  # the last breakage that ended

    def ok(self):
        with self._lock:
            self.consecutive = 0
            self.last_ok = self._clock()
            if self.state == "broken":
                self.recovered = {"at": self.last_ok, "since": self.since, "failures": self.failures,
                                  "note": self.note}
                logger.info("MCP server recovered (it had been failing since %s, %d failed request(s))",
                            _hhmm(self.since), self.failures)
                self.since, self.failures, self.note = None, 0, None
            self.state = "healthy"

    def client_error(self):
        """A 4xx: the server answered; not a success, not a failure."""
        with self._lock:
            self.consecutive = 0

    def failed(self, error: str):
        with self._lock:
            self.consecutive += 1
            self.last_error = error
            if self.state == "broken":
                self.failures += 1
            elif self.consecutive >= BROKEN_AFTER:
                self._break(f"{self.consecutive} requests in a row failed")

    def mark_broken(self, error: str):
        with self._lock:
            self.last_error = error
            if self.state != "broken":
                self._break("the self-check failed twice")
            else:
                self.failures += 1

    def set_note(self, note: str | None):
        with self._lock:
            self.note = note

    def _break(self, why: str):
        self.state = "broken"
        self.since = self._clock()
        self.failures = max(self.consecutive, 1)
        logger.error("MCP server broken: %s; last error: %s", why, self.last_error)

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "since": self.since, "failures": self.failures,
                    "last_error": self.last_error, "last_ok": self.last_ok, "note": self.note,
                    "recovered": dict(self.recovered) if self.recovered else None}


SELF_CHECK_HEADER = "x-voitta-self-check"


class _NoNewTransportLines(logging.Filter):
    """The MCP library logs every new session at INFO; HealthWatcher logs the
    real ones (with the client) and leaves out the self-check's."""

    def filter(self, record):
        return not record.getMessage().startswith("Created new transport with session ID")


class HealthWatcher:
    """ASGI middleware: records each HTTP request's outcome in ``health``, and
    logs MCP sessions opening and closing (not the self-check's)."""

    def __init__(self, app, health: McpHealth):
        self.app = app
        self.health = health

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = None
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        method = scope.get("method")
        quiet = SELF_CHECK_HEADER in headers

        async def watched_send(message):
            nonlocal status
            if message["type"] == "http.response.start" and status is None:
                status = message["status"]
                if status >= 500:
                    self.health.failed(f"HTTP {status} for {method} {scope.get('path')}")
                elif status >= 400:
                    self.health.client_error()
                else:
                    self.health.ok()
                    if not quiet:
                        self._log_session(method, headers, message)
            await send(message)

        try:
            await self.app(scope, receive, watched_send)
        except Exception as e:
            self.health.failed(f"{type(e).__name__}: {e}")
            raise

    @staticmethod
    def _log_session(method, headers, start):
        if method == "POST" and "mcp-session-id" not in headers:
            sid = next((v.decode("latin-1") for k, v in start.get("headers") or []
                        if k.lower() == b"mcp-session-id"), None)
            if sid:
                logger.info("MCP session %s opened by %s", sid[:8], headers.get("user-agent") or "?")
        elif method == "DELETE" and "mcp-session-id" in headers:
            logger.info("MCP session %s closed by the client", headers["mcp-session-id"][:8])


class McpServerHost:
    """Serves the MCP proxy, watches it, and rebuilds it once if it breaks."""

    def __init__(self, app_ref, port: int, build, *, host: str = "127.0.0.1", clock=time.time):
        self.app_ref = app_ref
        self.port = port
        self.host = host
        self._build = build
        self._clock = clock
        self.health = McpHealth(clock)
        self._server: uvicorn.Server | None = None
        self._rebuild_requested = False
        self._rebuilding = False
        self.rebuilt_at: float | None = None
        self._rebuilt_episode: float | None = None   # the breakage (its `since`) a rebuild answered
        self._blocked: str | None = None             # why it won't rebuild again
        self._last_probe = 0.0
        manager_log = logging.getLogger("mcp.server.streamable_http_manager")
        if not any(isinstance(f, _NoNewTransportLines) for f in manager_log.filters):
            manager_log.addFilter(_NoNewTransportLines())

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    # ---- serving -------------------------------------------------------------------

    def _uvicorn_server(self) -> tuple[object, uvicorn.Server]:
        import fastmcp
        main = self._build(self.app_ref, self.port)
        # The same host check fastmcp's own run_http_async applies to a
        # loopback server: Host must be this host (or a configured one).
        protection = fastmcp.settings.http_host_origin_protection
        allowed = fastmcp.settings.http_allowed_hosts
        if protection == "auto":
            allowed = [*(allowed or []), self.host]
        app = main.http_app(transport="streamable-http",
                            middleware=[Middleware(HealthWatcher, health=self.health)],
                            host_origin_protection=protection, allowed_hosts=allowed)
        config = uvicorn.Config(
            app, host=self.host, port=self.port,
            timeout_graceful_shutdown=2, lifespan="on", ws="none",
            # No uvicorn logging config: its loggers propagate to the root
            # handler (desktop.log) instead of stderr, which the app drops.
            log_config=None, access_log=False)
        return main, uvicorn.Server(config)

    async def serve(self):
        watcher = asyncio.create_task(self._watch(), name="mcp-health")
        first = True
        try:
            while True:
                self._rebuild_requested = False
                try:
                    main, server = self._uvicorn_server()
                except Exception as e:
                    if first:
                        raise
                    self._give_up(f"rebuilding it failed ({type(e).__name__}: {e})")
                    logger.exception("MCP server rebuild failed")
                    return
                self._server = server
                lifespan = getattr(main, "_lifespan_manager", None)
                try:
                    async with (lifespan() if lifespan else contextlib.nullcontext()):
                        started = asyncio.create_task(self._note_started(server, first))
                        try:
                            await server.serve()
                        finally:
                            started.cancel()
                except SystemExit:
                    # uvicorn exits the process when it can't bind; that must
                    # not take the app's event loop down with it.
                    self._give_up(f"it could not start on port {self.port} (see the error above)")
                    logger.error("MCP server could not start on port %d", self.port)
                    return
                except Exception:
                    if not self._rebuild_requested:
                        raise
                    logger.exception("stopping the broken MCP server raised; rebuilding anyway")
                if not self._rebuild_requested:
                    return
                _clear_sse_shutdown_flag(server)
                first = False
        finally:
            watcher.cancel()

    async def _note_started(self, server: uvicorn.Server, first: bool):
        while not server.started:
            await asyncio.sleep(0.05)
        if first:
            logger.info("MCP server listening on %s", self.url)
        else:
            self.rebuilt_at = self._clock()
            self._rebuilding = False
            logger.info("MCP server rebuilt and listening again on %s", self.url)

    def _give_up(self, why: str):
        self._rebuilding = False
        self._blocked = why
        self.health.mark_broken(why)
        self.health.set_note(f"Not rebuilt: {why}. Restart Voitta Desktop.")

    # ---- watching -------------------------------------------------------------------

    async def _watch(self):
        await asyncio.sleep(TICK_S)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MCP health check failed")
            await asyncio.sleep(TICK_S)

    async def tick(self):
        if self._rebuilding or self._server is None or not self._server.started:
            return
        snap = self.health.snapshot()
        now = self._clock()
        if snap["state"] != "broken":
            idle_since = max(snap["last_ok"] or 0.0, self._last_probe)
            if now - idle_since >= CHECK_EVERY_S:
                ok, err = await self.probe()
                if not ok:
                    await asyncio.sleep(RECHECK_AFTER_S)
                    ok, err = await self.probe()
                    if not ok:
                        self.health.mark_broken(f"self-check: {err}")
            return
        episode = snap["since"]
        if self._blocked is None and self._rebuilt_episode != episode:
            if self.rebuilt_at is not None and now - self.rebuilt_at < REBUILD_COOLOFF_S:
                self._blocked = f"it broke again within {int(REBUILD_COOLOFF_S // 60)} minutes of the last rebuild"
                logger.error("MCP server broken again within %d minutes of the last rebuild (%s); "
                             "not rebuilding again — restart Voitta Desktop",
                             int(REBUILD_COOLOFF_S // 60), _hhmm(self.rebuilt_at))
                self.health.set_note(f"Not rebuilt: {self._blocked}. Restart Voitta Desktop.")
                return
            self._rebuilt_episode = episode
            self.rebuild(f"broken since {_hhmm(episode)}: {snap['last_error']}")
            return
        if self._rebuilt_episode == episode and self._blocked is None and self.rebuilt_at:
            # The rebuild is up; see whether it answers.
            ok, err = await self.probe()
            if not ok:
                await asyncio.sleep(RECHECK_AFTER_S)
                ok, err = await self.probe()
            if not ok:
                self._blocked = f"the rebuilt server fails too ({err})"
                logger.error("MCP server still broken after the rebuild (%s); not rebuilding again — "
                             "restart Voitta Desktop", err)
                self.health.set_note(f"Not rebuilt: {self._blocked}. Restart Voitta Desktop.")
            return
        if now - self._last_probe >= CHECK_EVERY_S:
            await self.probe()  # stays red; turns green if it recovers by itself

    def rebuild(self, cause: str):
        server = self._server
        if server is None:
            return
        logger.error("MCP server %s — rebuilding it on port %d", cause, self.port)
        self.health.set_note(f"Rebuilding the MCP server ({_hhmm(self._clock())}).")
        self._rebuilding = True
        self._rebuild_requested = True
        server.should_exit = True

    async def probe(self) -> tuple[bool, str | None]:
        """Open and close one MCP session on our own endpoint."""
        self._last_probe = self._clock()
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "voitta-self-check", "version": "1"}}}
        headers = {"accept": "application/json, text/event-stream", "content-type": "application/json",
                   SELF_CHECK_HEADER: "1"}
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                r = await client.post(self.url, json=body, headers=headers)
                if r.status_code != 200:
                    return False, f"initialize answered HTTP {r.status_code}"
                sid = r.headers.get("mcp-session-id")
                if sid:
                    with contextlib.suppress(httpx.HTTPError):
                        await client.delete(self.url, headers={"mcp-session-id": sid, SELF_CHECK_HEADER: "1",
                                                               "mcp-protocol-version": "2025-06-18"})
        except httpx.HTTPError as e:
            return False, f"{type(e).__name__}: {e}"
        self.health.ok()
        return True, None
