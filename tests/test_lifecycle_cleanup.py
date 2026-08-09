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
