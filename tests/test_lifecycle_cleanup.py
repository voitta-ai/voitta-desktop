"""Shutdown cleanups must run on the packaged app's quit path.

``-[NSApplication terminate:]`` — reached by the Quit item, Cmd-Q and an
AppleScript quit — ends the process with a C ``exit()`` that never drives
``Py_FinalizeEx``, so Python's ``atexit`` handlers do not run at all.

Everything the app did on the way out was registered with ``atexit``, so on
every ordinary quit it skipped MCP subprocess teardown (hence "reclaiming
port N from orphan pid" on every startup), skipped the claude-link disarm
(``claude_link: disarmed on quit`` appeared zero times in any log), skipped
the event-loop shutdown, and left the run marker at ``running`` — which made
the next launch report a clean quit as a silent kill.

The last test here is the one that matters: it drives a real AppKit
terminate in a subprocess and checks the cleanups actually ran.
"""

import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

import lifecycle

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def reset_cleanups(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "_cleanups", [])
    monkeypatch.setattr(lifecycle, "_cleanups_ran", False)
    monkeypatch.setattr(lifecycle, "RUN_MARKER_PATH", tmp_path / "last_run.json")
    monkeypatch.setattr(lifecycle, "ensure_dirs", lambda: None)


def test_registered_cleanups_run():
    ran = []
    lifecycle.register_cleanup(lambda: ran.append("a"), "a")

    lifecycle.run_cleanups("test")

    assert ran == ["a"]


def test_cleanups_run_only_once():
    """Both exit paths can fire; the work must not be repeated."""
    ran = []
    lifecycle.register_cleanup(lambda: ran.append("a"), "a")

    lifecycle.run_cleanups("first")
    lifecycle.run_cleanups("second")

    assert ran == ["a"]


def test_cleanups_run_in_reverse_order():
    """Matches atexit semantics: tear down in the reverse of setup."""
    order = []
    lifecycle.register_cleanup(lambda: order.append("first"), "first")
    lifecycle.register_cleanup(lambda: order.append("second"), "second")

    lifecycle.run_cleanups("test")

    assert order == ["second", "first"]


def test_a_failing_cleanup_does_not_block_the_others():
    """A failed claude-link disarm must not leave subprocesses running."""
    ran = []

    def boom():
        raise RuntimeError("disarm failed")

    lifecycle.register_cleanup(lambda: ran.append("subprocesses"), "subprocs")
    lifecycle.register_cleanup(boom, "boom")

    lifecycle.run_cleanups("test")

    assert ran == ["subprocesses"]


def test_marker_records_a_clean_exit():
    lifecycle.run_cleanups("NSApplicationWillTerminate")

    marker = json.loads(lifecycle.RUN_MARKER_PATH.read_text())
    assert marker["status"] == "clean"
    assert marker["detail"] == "NSApplicationWillTerminate"


def test_marker_stays_running_when_cleanups_never_run(tmp_path):
    """The genuine-silent-kill signal must still work after this change."""
    lifecycle._write_marker("running", "startup")

    marker = json.loads(lifecycle.RUN_MARKER_PATH.read_text())
    assert marker["status"] == "running"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires AppKit")
def test_appkit_terminate_runs_cleanups(tmp_path):
    """End to end on a real terminate: — the regression this whole file exists for.

    Without install_appkit_termination_observer() this fails: the file keeps
    its initial contents because atexit never fires under terminate:.
    """
    out = tmp_path / "result.txt"
    home = tmp_path / "home"
    script = textwrap.dedent(f"""
        import os, pathlib, sys
        sys.path.insert(0, {str(REPO)!r})
        os.environ["VOITTA_DESKTOP_HOME"] = {str(home)!r}
        import lifecycle
        out = pathlib.Path({str(out)!r})
        out.write_text("cleanups did not run")
        lifecycle.install()
        lifecycle.register_cleanup(lambda: out.write_text("ran"), "probe")
        lifecycle.install_appkit_termination_observer()
        import AppKit
        from PyObjCTools import AppHelper
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
        AppHelper.callLater(0.4, lambda: AppKit.NSApp().terminate_(None))
        AppHelper.runEventLoop()
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=90
    )

    assert out.read_text() == "ran", (
        f"cleanups skipped under NSApp.terminate_\n{proc.stderr[-800:]}"
    )
    marker = json.loads((home / "state" / "last_run.json").read_text())
    assert marker["status"] == "clean"


# ── SIGPIPE: the crash that looked like "the network is flaky" ───────────────
#
# Writing to a socket whose peer has gone away raises SIGPIPE, and its default
# disposition terminates the process instantly — no traceback, no atexit, and
# no crash report (the kernel only writes those for SIGSEGV/SIGBUS/SIGILL/
# SIGABRT). CPython normally sets SIGPIPE to SIG_IGN during
# Py_InitializeFromConfig, but briefcase's launcher uses
# PyConfig_InitIsolatedConfig(), which disables install_signal_handlers — so
# the packaged app kept the lethal default while `python app.py` did not.
#
# Confirmed in the kernel log: two independent deaths of the main app process
# reported as "termination reported by launchd (2, 13, 13)".

def test_sigpipe_is_ignored_after_install():
    import signal
    lifecycle._ignore_sigpipe()
    assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN


def test_ignoring_sigpipe_twice_is_harmless():
    import signal
    lifecycle._ignore_sigpipe()
    lifecycle._ignore_sigpipe()
    assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN


def test_a_dead_peer_kills_an_unprotected_process():
    """The negative control. Without this the fix below proves nothing."""
    assert _write_to_dead_peer(protected=False) == "killed:SIGPIPE"


def test_a_dead_peer_cannot_kill_a_protected_process():
    """The fix: the write raises instead of terminating the process."""
    assert _write_to_dead_peer(protected=True) == "BrokenPipeError"


def _write_to_dead_peer(protected: bool) -> str:
    """Write to a closed socket in a subprocess; report how it ended."""
    import signal
    prog = textwrap.dedent(f"""
        import signal, socket, sys, os
        if {protected!r}:
            sys.path.insert(0, {str(REPO)!r})
            os.environ["VOITTA_DESKTOP_HOME"] = "/tmp/vd-sigpipe-test"
            import lifecycle
            lifecycle._ignore_sigpipe()
        else:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # the bundle's state
        a, b = socket.socketpair()
        b.close()
        try:
            for _ in range(200):
                a.send(b"x" * 65536)
        except BrokenPipeError:
            print("BrokenPipeError")
        sys.exit(0)
    """)
    proc = subprocess.run([sys.executable, "-c", prog],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode < 0:
        return f"killed:{signal.Signals(-proc.returncode).name}"
    return proc.stdout.strip()
