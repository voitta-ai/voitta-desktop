"""Regression tests for the crash that destroyed MCP sessions.

The failure, in order:

  1. An MCP client's tools/list times out (~5s) and it sends
     notifications/cancelled.
  2. The SDK calls ``RequestResponder.cancel()``, which has ALREADY sent an
     error response and set ``_completed``.
  3. The cancellation is raised into our handler.
  4. ``show_tool_gate`` caught ``(CancelledError, Exception)`` and returned
     None — swallowing it.
  5. The handler therefore returned a normal value, so the SDK fell through
     to a second ``respond()`` and tripped
     ``assert not self._completed``.
  6. That AssertionError escaped into the anyio TaskGroup and tore down the
     entire streamable-http session.

The SDK's own guard at ``lowlevel/server.py`` only fires inside
``except get_cancelled_exc_class()`` — so the fix is that the cancellation
has to escape. These tests pin that contract, and the second half of the
fix: the popup outlives the cancelled request and publishes its answer for
the client's retry.

AppKit is stubbed so these run headless.
"""

import asyncio
import sys
import types

import pytest


@pytest.fixture
def gate(monkeypatch):
    """Import ui.tool_gate with AppKit/WebKit replaced by stubs."""
    for name in ("AppKit", "Foundation", "WebKit", "objc", "PyObjCTools",
                 "PyObjCTools.AppHelper"):
        sys.modules.pop(name, None)

    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda _n: type("Stub", (), {})  # any symbol resolves
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    _stub("AppKit")
    _stub("Foundation")
    _stub("WebKit")
    _stub("objc", super=lambda *a: type("S", (), {"init": lambda s: s})())

    pyobjc = types.ModuleType("PyObjCTools")
    helper = types.ModuleType("PyObjCTools.AppHelper")
    # callAfter would normally hop to the main run loop; run inline instead so
    # the tests stay deterministic.
    helper.callAfter = lambda fn, *a: fn(*a)
    pyobjc.AppHelper = helper
    sys.modules["PyObjCTools"] = pyobjc
    sys.modules["PyObjCTools.AppHelper"] = helper

    sys.modules.pop("ui.tool_gate", None)
    import ui.tool_gate as tg

    # Never actually build a window.
    monkeypatch.setattr(helper, "callAfter", lambda fn, *a: None)
    yield tg

    tg._gate_window = None
    tg._gate_pending = False
    tg._gate_on_result = None
    tg._gate_waiters.clear()
    sys.modules.pop("ui.tool_gate", None)


async def _cancel_soon(task):
    await asyncio.sleep(0)
    task.cancel()


@pytest.mark.asyncio
async def test_cancellation_propagates(gate):
    """The bug. Swallowing this is what killed the session."""
    task = asyncio.create_task(gate.show_tool_gate([], set()))
    await _cancel_soon(task)

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_popup_stays_open_after_cancellation(gate):
    """The user is still reading it; tearing it down would re-prompt forever."""
    task = asyncio.create_task(gate.show_tool_gate([], set()))
    await _cancel_soon(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.gate_is_open()


@pytest.mark.asyncio
async def test_answer_is_published_after_the_caller_is_gone(gate):
    """The retry is served from this. Without it the fix is only half done."""
    published = []
    task = asyncio.create_task(
        gate.show_tool_gate([], set(), on_result=published.append)
    )
    await _cancel_soon(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    gate._finish(["tool_a"])           # the user finally clicks

    assert published == [["tool_a"]]
    assert not gate.gate_is_open()


@pytest.mark.asyncio
async def test_cancelled_waiter_is_not_resumed(gate):
    """A cancelled task must stay cancelled even though its event fires."""
    task = asyncio.create_task(gate.show_tool_gate([], set()))
    await _cancel_soon(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    gate._finish(["tool_a"])
    assert task.cancelled()


@pytest.mark.asyncio
async def test_second_request_attaches_instead_of_stacking(gate):
    """A retry while the popup is up must not open a second window."""
    first = asyncio.create_task(gate.show_tool_gate([], set()))
    await _cancel_soon(first)
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(gate.show_tool_gate([], set()))
    await asyncio.sleep(0)

    gate._finish(["tool_a"])
    assert await second == ["tool_a"]


@pytest.mark.asyncio
async def test_normal_answer_returns_to_the_caller(gate):
    """The uncancelled path still works."""
    task = asyncio.create_task(gate.show_tool_gate([], set()))
    await asyncio.sleep(0)
    gate._finish(["tool_a", "tool_b"])

    assert await task == ["tool_a", "tool_b"]


@pytest.mark.asyncio
async def test_cancel_is_distinct_from_empty(gate):
    """None means 'allow nothing'; [] means 'disable nothing'. Never conflate."""
    task = asyncio.create_task(gate.show_tool_gate([], set()))
    await asyncio.sleep(0)
    gate._finish(None)

    assert await task is None


@pytest.mark.asyncio
async def test_finish_is_idempotent(gate):
    """Closing the window re-enters _finish; the second call must be a no-op."""
    published = []
    task = asyncio.create_task(
        gate.show_tool_gate([], set(), on_result=published.append)
    )
    await asyncio.sleep(0)

    gate._finish(["tool_a"])
    gate._finish(["tool_b"])

    assert await task == ["tool_a"]
    assert published == [["tool_a"]]


@pytest.mark.asyncio
async def test_failing_callback_still_wakes_the_waiter(gate):
    """A broken consumer must not strand the request that opened the gate."""
    def _boom(_result):
        raise RuntimeError("callback exploded")

    task = asyncio.create_task(gate.show_tool_gate([], set(), on_result=_boom))
    await asyncio.sleep(0)
    gate._finish(["tool_a"])

    assert await task == ["tool_a"]
