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

# Shutdown cleanups, and a latch so they run exactly once no matter which
# exit path fires first. See run_cleanups() for why there are two paths.
_cleanups: list[tuple[str, object]] = []
_cleanups_ran = False
_cleanup_lock = threading.Lock()


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
        # Deliberately does NOT say "no signal". It used to, and that was
        # actively misleading: a signal we do not handle (SIGPIPE, SIGKILL)
        # kills the process before any handler runs, so this marker looks
        # identical either way. The kernel is the authority, so point at it.
        logger.critical(
            "PREVIOUS RUN DIED without running any shutdown handler. "
            "pid=%s last_seen=%s peak_rss=%s MB. Cause is NOT visible from "
            "in-process state — an unhandled signal, SIGKILL or an OOM kill "
            "all look like this. Ask the kernel:\n"
            "    log show --last 1h --predicate 'eventMessage CONTAINS \"%s\" "
            "AND eventMessage CONTAINS \"termination reported\"' --style compact\n"
            "  status (2, 13, 13) = SIGPIPE, (2, 9, 9) = SIGKILL, (0, 0, 0) = clean exit.",
            prev.get("pid"), when, rss, prev.get("pid"),
        )
    elif status == "clean":
        logger.info("previous run exited cleanly at %s (peak_rss=%s MB)", when, rss)
    else:
        logger.warning(
            "previous run ended: status=%s detail=%s at=%s peak_rss=%s MB",
            status, prev.get("detail"), when, rss,
        )


def register_cleanup(fn, name: str | None = None) -> None:
    """Register shutdown work that must run however the app exits.

    Use this instead of ``atexit.register`` for anything that has to happen
    on the way out. In a packaged menu-bar app, ``atexit`` is not enough —
    see :func:`install_appkit_termination_observer`.
    """
    _cleanups.append((name or getattr(fn, "__qualname__", repr(fn)), fn))


def run_cleanups(reason: str) -> None:
    """Run every registered cleanup once, then mark the exit clean.

    Reached from two directions — ``atexit`` (terminal runs) and
    AppKit's will-terminate notification (the packaged app) — so it latches
    and is safe to call from both. Cleanups run in reverse registration
    order, matching ``atexit`` semantics, and one that raises does not stop
    the rest: a failed claude-link disarm must not leave subprocesses alive.
    """
    global _cleanups_ran
    with _cleanup_lock:
        if _cleanups_ran:
            return
        _cleanups_ran = True

    for name, fn in reversed(_cleanups):
        try:
            fn()
        except Exception:
            logger.exception("shutdown cleanup %s failed", name)

    _write_marker("clean", reason)
    logger.info("--- shutdown: clean exit via %s (peak_rss=%.1f MB) ---",
                reason, peak_rss_mb())


def install_appkit_termination_observer() -> None:
    """Run the cleanups when AppKit terminates the app.

    ``-[NSApplication terminate:]`` — which is what the Quit menu item, Cmd-Q
    and an AppleScript quit all reach through ``rumps.quit_application()`` —
    ends the process with a C ``exit()``. That never drives
    ``Py_FinalizeEx``, so **Python's atexit handlers do not run at all**.

    Measured, not assumed: a PyObjC process that registers an atexit handler
    and then calls ``NSApp.terminate_(None)`` leaves the handler unexecuted.

    Everything the app did on the way out was registered with ``atexit``, so
    on every ordinary quit it silently skipped subprocess teardown (hence
    "reclaiming port N from orphan pid" on literally every startup) and the
    claude-link disarm (which had never once run — ``claude_link: disarmed on
    quit`` appears zero times in any log). It also left this module's run
    marker at ``running``, so the next launch mistook a clean quit for a
    silent kill.

    ``NSApplicationWillTerminateNotification`` is delivered before that
    ``exit()``, on the main thread, which is where the cleanups belong.
    """
    try:
        from AppKit import NSApplicationWillTerminateNotification
        from Foundation import NSNotificationCenter
    except ImportError:
        return  # terminal mode — atexit is sufficient there

    def _on_terminate(_notification):
        run_cleanups("NSApplicationWillTerminate")

    # The token must outlive this call or the observer is collected.
    global _termination_token
    _termination_token = (
        NSNotificationCenter.defaultCenter()
        .addObserverForName_object_queue_usingBlock_(
            NSApplicationWillTerminateNotification, None, None, _on_terminate
        )
    )
    logger.info("AppKit termination observer installed")


_termination_token = None


def heartbeat() -> None:
    """Refresh the marker so a silent kill leaves a recent RSS reading.

    Called from the request-logger watchdog, which already ticks on a
    timer — no extra thread for this.
    """
    _write_marker("running", "heartbeat")


def _ignore_sigpipe() -> None:
    """Stop a dead peer from killing the process.

    This is the bug that made the app "crash when the internet is flaky".

    Writing to a socket whose peer has gone away raises SIGPIPE, whose
    default disposition is to terminate the process immediately — no
    traceback, no atexit, and no crash report, because the kernel only
    writes those for SIGSEGV/SIGBUS/SIGILL/SIGABRT. Any dropped connection
    mid-stream could take the whole app down, leaving its MCP subprocesses
    orphaned. That is exactly the reported symptom, and the kernel log
    confirms it: two independent deaths reported as
    ``termination reported by launchd (2, 13, 13)`` — domain signal, code 13.

    Normally CPython does this for us: ``Py_InitializeFromConfig`` sets
    SIGPIPE to SIG_IGN when ``install_signal_handlers`` is on. Briefcase's
    launcher builds its config with ``PyConfig_InitIsolatedConfig()`` (the
    stub binary announces "Configuring isolated Python"), and the isolated
    config turns that flag OFF. So the disposition stays at the OS default
    and every socket write becomes a potential kill — in the packaged app
    only. Running ``python app.py`` from a terminal inherits SIG_IGN and
    never shows the bug, which is why it only ever bit the shipped build.

    With SIGPIPE ignored, the write returns EPIPE and Python raises
    BrokenPipeError, which aiohttp already handles as a normal client
    disconnect.
    """
    try:
        previous = signal.getsignal(signal.SIGPIPE)
    except (OSError, ValueError, AttributeError):
        return
    if previous == signal.SIG_IGN:
        logger.debug("SIGPIPE already ignored")
        return
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    except (OSError, ValueError) as e:
        logger.error("could not ignore SIGPIPE (%s); a dropped connection "
                     "can still kill this process", e)
        return
    logger.warning(
        "SIGPIPE was %s (lethal) — now ignored. Without this a dropped "
        "connection terminates the app with no traceback.",
        "SIG_DFL" if previous == signal.SIG_DFL else previous,
    )


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
    _ignore_sigpipe()          # before any socket is opened
    _install_signal_handlers()
    _install_exception_hooks()

    # Covers terminal runs, where the interpreter shuts down normally. The
    # packaged app needs install_appkit_termination_observer() as well.
    atexit.register(run_cleanups, "atexit")

    logger.info("crash diagnostics installed (marker=%s)", RUN_MARKER_PATH)
