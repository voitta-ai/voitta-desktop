"""One asyncio loop, one worker pool, shared by the whole app.

Voitta Desktop used to run four event loops and nine ad-hoc threads: one
loop for the LLM proxy, another created inside the menu, a third inside the
settings window, a fourth owned by FastMCP's blocking ``run()``, plus a
``threading.Timer`` per OAuth token, a blocking ``HTTPServer`` for the OAuth
redirect, a watchdog thread, and a fresh ``threading.Thread`` per settings
click. Nothing coordinated them, so "which loop is this coroutine on?" and
"is this attribute safe to touch from here?" had to be re-answered by hand
at every call site — and getting it wrong produced exactly the kind of
intermittent, unattributable failure that was hard to chase.

There are now two concurrency contexts, and only two:

* **The AppKit main thread**, which macOS requires for all UI. Cross into it
  with :func:`ui.main_thread.on_main_thread`, never by hand.
* **This runtime** — one loop on one thread for everything async, plus a
  small bounded pool for genuinely blocking calls (MSAL, ``requests``,
  subprocess probes) that must not sit on the loop.

Anything that needs to reach async code from the outside calls
:meth:`AsyncRuntime.submit`; anything that needs to block calls
:meth:`AsyncRuntime.run_blocking`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any, Callable, Coroutine

import lifecycle

logger = logging.getLogger("voitta-desktop.runtime")


class AsyncRuntime:
    """The app's single event loop and blocking-work pool."""

    def __init__(self, name: str = "voitta-runtime", max_blocking_workers: int = 8):
        self._name = name
        self._max_blocking_workers = max_blocking_workers
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._ready = threading.Event()
        self._stopping = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, timeout: float = 5.0) -> None:
        """Bring the loop up and block until it is accepting work."""
        if self._thread is not None:
            return

        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_blocking_workers,
            thread_name_prefix=f"{self._name}-blocking",
        )

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.set_default_executor(self._pool)
            # Surface exceptions from tasks nobody awaited — otherwise they
            # vanish into "Task exception was never retrieved" at GC time.
            loop.set_exception_handler(lifecycle.asyncio_exception_handler)
            self._loop = loop
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    self._drain(loop)
                finally:
                    loop.close()
                    logger.info("runtime loop closed")

        self._thread = threading.Thread(target=_run, name=self._name, daemon=True)
        self._thread.start()

        if not self._ready.wait(timeout):
            raise RuntimeError(f"{self._name} did not start within {timeout}s")
        logger.info("runtime started (loop thread=%s)", self._name)

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """Cancel everything still pending and let it unwind."""
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if not pending:
            return
        logger.info("draining %d pending task(s)", len(pending))
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the loop and the pool. Safe to call more than once."""
        if self._stopping or self._loop is None:
            return
        self._stopping = True
        logger.info("runtime shutting down")
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.warning("runtime thread did not stop within %.1fs", timeout)
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    # ── Access ───────────────────────────────────────────────────────────────

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("AsyncRuntime.start() has not been called")
        return self._loop

    @property
    def running(self) -> bool:
        return self._loop is not None and not self._stopping

    # ── Submitting work ──────────────────────────────────────────────────────

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        """Run a coroutine on the loop from any thread.

        Returns a ``concurrent.futures.Future`` — call ``.result()`` to wait,
        ``.cancel()`` to cancel, or just drop it for fire-and-forget.
        """
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def spawn(self, coro: Coroutine, name: str | None = None) -> concurrent.futures.Future:
        """Fire-and-forget a coroutine, logging anything it raises.

        Use for long-lived background work (servers, watchdogs) where no
        caller is going to check the result.
        """
        future = self.submit(coro)
        label = name or getattr(coro, "__qualname__", "task")

        def _report(f: concurrent.futures.Future) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                logger.error("background task %r failed: %s", label, exc, exc_info=exc)

        future.add_done_callback(_report)
        return future

    def run_blocking(self, fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        """Run a blocking callable on the pool, off both the loop and the UI.

        This is the replacement for ``threading.Thread(target=...).start()``:
        same effect, but bounded, named, and with failures logged instead of
        silently killing an anonymous thread.
        """
        if self._pool is None:
            raise RuntimeError("AsyncRuntime.start() has not been called")
        future = self._pool.submit(fn, *args)
        label = getattr(fn, "__qualname__", repr(fn))

        def _report(f: concurrent.futures.Future) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                logger.error("blocking task %r failed: %s", label, exc, exc_info=exc)

        future.add_done_callback(_report)
        return future

    def call_later(
        self, delay: float, fn: Callable[..., Any], *args: Any
    ) -> concurrent.futures.Future:
        """Run ``fn`` after ``delay`` seconds. Replaces ``threading.Timer``.

        ``fn`` may block — it runs on the pool, not the loop. Cancel via the
        returned future's ``.cancel()``, matching Timer's interface.
        """
        async def _delayed() -> None:
            await asyncio.sleep(delay)
            await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

        return self.spawn(_delayed(), name=f"call_later({getattr(fn, '__name__', fn)})")


# The process-wide instance. Created here so every module can import it
# without threading a reference through constructors.
runtime = AsyncRuntime()
