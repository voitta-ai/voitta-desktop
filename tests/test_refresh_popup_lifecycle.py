"""The "Refresh LLM Tools" popup must die when its window closes.

It didn't. ``open()`` registered a block-based NSNotificationCenter observer
and discarded the token, so the observer was never removed; the block holds
``self``, the center holds the block, and ``releasedWhenClosed=False`` means
AppKit never freed the window either. Result: one leaked ``StatusPopup`` —
with a live ``WKWebView`` and its own JavaScriptCore heap — per click, for
the life of the process. JSC's scavenger then swept every one of them on a
timer, which showed up as 75–95 % CPU bursts while the app sat idle.

Measured before the fix: 3 open/close cycles → 3 live popups.
"""

from __future__ import annotations

import gc
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires the macOS AppKit bridge"
)

AppKit = pytest.importorskip("AppKit")
WebKit = pytest.importorskip("WebKit")

ROWS = [("backend", "https://x.invalid/mcp", "idle", "0 tools")]


@pytest.fixture(autouse=True)
def _app():
    AppKit.NSApplication.sharedApplication()


def _live_popups():
    from ui.refresh_popup import StatusPopup

    gc.collect()
    return [o for o in gc.get_objects() if isinstance(o, StatusPopup)]


def test_open_close_cycles_leave_nothing_behind() -> None:
    from ui.refresh_popup import StatusPopup

    before = len(_live_popups())
    for _ in range(3):
        p = StatusPopup()
        p.open(ROWS)
        p.close()
        del p
    assert len(_live_popups()) == before


def test_close_releases_every_reference() -> None:
    from ui.refresh_popup import StatusPopup

    p = StatusPopup()
    p.open(ROWS)
    assert p.window is not None and p.webview is not None
    assert p.observer is not None and p._close_token is not None

    p.close()

    assert p._closed is True
    assert p.window is None
    assert p.webview is None
    assert p.observer is None
    assert p._close_token is None


def test_red_x_path_is_the_same_teardown() -> None:
    """The window's own close (red X) fires the same notification our
    ``close()`` does; both must land in ``_teardown`` exactly once."""
    from ui.refresh_popup import StatusPopup

    p = StatusPopup()
    p.open(ROWS)
    win = p.window
    win.close()  # what the red X does — not our close()

    assert p._closed is True and p.window is None and p.webview is None
    # A second close must be a no-op, not a double-teardown.
    p.close()
    assert p._closed is True


def test_close_handler_fires_once_after_teardown() -> None:
    from ui.refresh_popup import StatusPopup

    calls: list[int] = []
    p = StatusPopup()
    p.set_close_handler(lambda: calls.append(1))
    p.open(ROWS)
    p.close()
    p.close()
    assert calls == [1]


def test_update_after_close_is_a_noop() -> None:
    """The refresh loop may still push status after the user closed the
    window; that must not touch a released web view."""
    from ui.refresh_popup import StatusPopup

    p = StatusPopup()
    p.open(ROWS)
    p.close()
    p.update(0, "ok", "3 tools")  # must not raise, must not resurrect
    assert p.webview is None
