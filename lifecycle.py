"""Crash diagnostics: make an unexpected exit leave a trace.

The app was dying without explanation — no macOS crash report (so not a
segfault), no traceback, and orphaned MCP subprocesses on the next boot
(so ``atexit`` never ran). A clean quit and a hard kill looked identical
in the log, which made the failure impossible to attribute.

This module closes that gap with two mechanisms:

* **Handlers** — signal, ``sys.excepthook``, ``threading.excepthook`` and
  the asyncio exception handler all log before the process goes away.
  These cover everything the process can observe about its own death.

* **A run marker** — a small JSON file rewritten on start, on heartbeat
  and on exit. If a run starts and finds the previous marker still in
  state ``running``, the previous process died without executing *any*
  handler: SIGKILL, a jetsam (out-of-memory) kill, or a kernel panic.
  That is the only way to see those, since by definition nothing in-process
  gets to run. The marker carries the last known peak RSS, which is what
  distinguishes an OOM kill from the rest.

Note that Python only runs signal handlers in the main thread between
bytecodes. While the main thread sits inside AppKit's ``[NSApp run]``,
delivery can be delayed. The marker is the reliable half; the handlers
are the informative half.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import resource
import signal
import sys
import threading
import time
import traceback

from paths import RUN_MARKER_PATH, ensure_dirs

logger = logging.getLogger("voitta-desktop.lifecycle")

# Darwin reports ru_maxrss in bytes; Linux reports kilobytes.
_RSS_SCALE = 1 if platform.system() == "Darwin" else 1024

_marker_lock = threading.Lock()
_installed = False


def peak_rss_mb() -> float:
    """Peak resident set size in MB, for this process, since start.

    Peak rather than current because it is free (no psutil, no subprocess)
    and because monotonic growth is exactly the signal that distinguishes
    an out-of-memory kill from every other cause of a silent exit.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_SCALE / 1_000_000


def _write_marker(status: str, detail: str = "") -> None:
    """Rewrite the run marker. Never raises — diagnostics must not kill the app."""
    payload = {
        "pid": os.getpid(),
        "status": status,
        "detail": detail,
        "at": time.time(),
        "at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }
    try:
        with _marker_lock:
            tmp = RUN_MARKER_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(RUN_MARKER_PATH)  # atomic; a torn marker reads as a crash
    except OSError as e:
        logger.debug("could not write run marker: %s", e)


def _report_previous_run() -> None:
    """Log how the previous run ended, reading the marker it left behind."""
    try:
        prev = json.loads(RUN_MARKER_PATH.read_text())
    except (OSError, ValueError):
        return  # first run, or marker lost — nothing to report

    status = prev.get("status")
    rss = prev.get("peak_rss_mb", "?")
    when = prev.get("at_human", "?")

    if status == "running":
        logger.critical(
            "PREVIOUS RUN DIED SILENTLY — no signal, no exception, no clean exit. "
            "pid=%s last_seen=%s peak_rss=%s MB. This is SIGKILL, an out-of-memory "
            "(jetsam) kill, or a panic. Compare peak_rss against available RAM.",
            prev.get("pid"), when, rss,
        )
    elif status == "clean":
        logger.info("previous run exited cleanly at %s (peak_rss=%s MB)", when, rss)
    else:
        logger.warning(
            "previous run ended: status=%s detail=%s at=%s peak_rss=%s MB",
            status, prev.get("detail"), when, rss,
        )


def heartbeat() -> None:
    """Refresh the marker so a silent kill leaves a recent RSS reading.

    Called from the request-logger watchdog, which already ticks on a
    timer — no extra thread for this.
    """
    _write_marker("running", "heartbeat")


def _install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        try:
            previous = signal.getsignal(sig)
        except (OSError, ValueError):
            continue

        def handler(signum, frame, _previous=previous):
            name = signal.Signals(signum).name
            _write_marker("signal", name)
            logger.critical(
                "--- shutdown: %s received (peak_rss=%.1f MB) ---",
                name, peak_rss_mb(),
            )
            if callable(_previous):
                _previous(signum, frame)
                return
            # Restore the default disposition and re-raise so the process
            # dies with the right exit status instead of being swallowed.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(sig, handler)
        except (OSError, ValueError) as e:
            logger.debug("could not install handler for %s: %s", sig, e)


def _install_exception_hooks() -> None:
    prev_excepthook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            _write_marker("exception", f"{exc_type.__name__}: {exc}")
            logger.critical(
                "--- shutdown: uncaught %s on main thread ---\n%s",
                exc_type.__name__,
                "".join(traceback.format_exception(exc_type, exc, tb)),
            )
        prev_excepthook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        # A dead background thread does not end the process, but it does
        # silently remove a proxy or a watchdog — worth shouting about.
        logger.critical(
            "thread %s died on uncaught %s\n%s",
            args.thread.name if args.thread else "?",
            args.exc_type.__name__,
            "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            ),
        )

    threading.excepthook = _thread_excepthook


def asyncio_exception_handler(loop, context) -> None:
    """Loop-level handler for exceptions no task ever retrieved.

    Installed by AsyncRuntime on the shared loop.
    """
    exc = context.get("exception")
    if exc is not None:
        logger.error(
            "unhandled asyncio exception: %s\n%s",
            context.get("message", ""),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
    else:
        logger.error("unhandled asyncio error: %s", context.get("message", context))


def install() -> None:
    """Wire up every diagnostic. Call once, as early in startup as possible."""
    global _installed
    if _installed:
        return
    _installed = True

    ensure_dirs()
    _report_previous_run()
    _write_marker("running", "startup")
    _install_signal_handlers()
    _install_exception_hooks()

    @atexit.register
    def _mark_clean() -> None:
        _write_marker("clean", "atexit")
        logger.info("--- shutdown: clean exit (peak_rss=%.1f MB) ---", peak_rss_mb())

    logger.info("crash diagnostics installed (marker=%s)", RUN_MARKER_PATH)
