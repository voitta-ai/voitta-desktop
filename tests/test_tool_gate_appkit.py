"""Regression tests that need the real AppKit bridge, not stubs.

The headless tests in test_tool_gate_cancellation.py replace AppKit with
stubs, which is what let this ship: ``window._gate_refs = (webview, observer)``
raises AttributeError on a real PyObjC NSWindow, but a stub object accepts any
attribute, so the whole class of bug was invisible.

That one line ran immediately before ``_gate_window = window``, so the global
was never assigned. The popup appeared, its KVO observer worked, the user's
answer was published — and the window could not be closed, because _finish
had no reference to it. OK and Cancel both looked dead.

These tests are skipped off macOS.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires the macOS AppKit bridge"
)

AppKit = pytest.importorskip("AppKit")
Foundation = pytest.importorskip("Foundation")
WebKit = pytest.importorskip("WebKit")


@pytest.fixture
def window():
    frame = Foundation.NSMakeRect(0, 0, 480, 520)
    mask = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
    w = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, mask, AppKit.NSBackingStoreBuffered, False
    )
    w.setReleasedWhenClosed_(False)
    yield w
    w.close()


def test_nswindow_rejects_python_attributes(window):
    """Pin the platform behaviour that caused the bug.

    If a future PyObjC allows this, the workaround can be revisited — but
    until then, nothing may stash state on an NSWindow.
    """
    with pytest.raises(AttributeError):
        window._gate_refs = ("anything",)


def test_module_refs_hold_the_popup_objects():
    """The replacement for the attribute stash must actually be a strong ref."""
    from ui import tool_gate

    assert isinstance(tool_gate._gate_refs, list)

    tool_gate._gate_refs[:] = [object()]
    assert len(tool_gate._gate_refs) == 1
    tool_gate._gate_refs.clear()
    assert tool_gate._gate_refs == []


def test_observer_detach_is_one_shot(window):
    """Whoever detaches first wins; the loser must not double-finish."""
    from ui.tool_gate import _GateTitleObserver

    frame = Foundation.NSMakeRect(0, 0, 480, 520)
    cfg = WebKit.WKWebViewConfiguration.alloc().init()
    cfg.setWebsiteDataStore_(WebKit.WKWebsiteDataStore.nonPersistentDataStore())
    webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(frame, cfg)

    observer = _GateTitleObserver.alloc().initWithWindow_(window)
    webview.addObserver_forKeyPath_options_context_(observer, "title", 1, None)

    assert observer.detachFrom_(webview)        # first caller detaches
    assert not observer.detachFrom_(webview)    # second is a no-op


def test_finish_closes_a_real_window(window, monkeypatch):
    """End to end on the real bridge: _finish must actually close the window.

    This is the assertion that would have caught the shipped bug.
    """
    from ui import tool_gate

    window.makeKeyAndOrderFront_(None)
    assert window.isVisible()

    monkeypatch.setattr(tool_gate, "_gate_window", window)
    monkeypatch.setattr(tool_gate, "_gate_pending", True)
    monkeypatch.setattr(tool_gate, "_gate_on_result", None)

    tool_gate._finish(["tool_a"])

    assert not window.isVisible()
    assert tool_gate._gate_window is None


def test_show_publishes_the_window_before_it_can_fail():
    """The actual regression: building the popup must set ``_gate_window``.

    Runs the real ``_show`` through ``show_tool_gate`` with callAfter made
    synchronous. Against the shipped code this fails — the AttributeError on
    ``window._gate_refs`` aborted _show one line before the assignment, so
    the window existed on screen with nothing able to close it.
    """
    import asyncio

    from PyObjCTools import AppHelper

    from ui import tool_gate

    ran = {}
    original = AppHelper.callAfter
    AppHelper.callAfter = lambda fn, *a: ran.setdefault("exc", _run(fn, a))

    def _run(fn, a):
        try:
            fn(*a)
            return None
        except Exception as e:  # surfaced in the assertion below
            return e

    async def drive():
        task = asyncio.ensure_future(
            tool_gate.show_tool_gate([{"prefix": "t", "label": "T", "tools": ["a"]}], set())
        )
        await asyncio.sleep(0)
        return task

    try:
        loop = asyncio.new_event_loop()
        try:
            task = loop.run_until_complete(drive())
            assert ran.get("exc") is None, f"_show raised: {ran['exc']!r}"
            assert tool_gate._gate_window is not None, \
                "_show finished without publishing the window — popup would be unclosable"
            tool_gate._finish(None)
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        finally:
            loop.close()
    finally:
        AppHelper.callAfter = original
        if tool_gate._gate_window is not None:
            tool_gate._finish(None)
        tool_gate._gate_pending = False
        tool_gate._gate_waiters.clear()


def test_finish_without_a_window_reference_is_loud(caplog):
    """The stuck-popup state must announce itself instead of failing silently."""
    import logging

    from ui import tool_gate

    tool_gate._gate_window = None
    tool_gate._gate_pending = True
    tool_gate._gate_on_result = None

    with caplog.at_level(logging.ERROR, logger="voitta-desktop.tool_gate"):
        tool_gate._finish(None)

    assert "no window reference" in caplog.text
