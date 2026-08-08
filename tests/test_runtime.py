"""The shared runtime replaced four event loops and nine ad-hoc threads.

Everything async in the app now depends on this one object, so its
contract — work runs, failures are visible rather than silent, and shutdown
does not hang — is worth pinning.
"""

import asyncio
import concurrent.futures
import logging
import threading
import time

import pytest

from runtime import AsyncRuntime


def _wait_for_log(caplog, needle: str, timeout: float = 3.0) -> bool:
    """Poll caplog until `needle` shows up.

    The done-callback that logs a failure runs on the loop thread, so it is
    not ordered against the waiter that .result() wakes — asserting straight
    after .result() is a race.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in caplog.text:
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def rt():
    runtime = AsyncRuntime(name="test-runtime")
    runtime.start()
    yield runtime
    runtime.shutdown(timeout=3.0)


def test_submit_runs_a_coroutine_and_returns_its_value(rt):
    async def work():
        await asyncio.sleep(0)
        return 42

    assert rt.submit(work()).result(timeout=3) == 42


def test_work_runs_off_the_calling_thread(rt):
    """The whole point is that the UI thread never blocks on this."""
    async def which_thread():
        return threading.current_thread().name

    assert rt.submit(which_thread()).result(timeout=3) != threading.current_thread().name


def test_run_blocking_uses_the_pool(rt):
    """Replaces threading.Thread(target=...) — bounded and named."""
    def work():
        return threading.current_thread().name

    name = rt.run_blocking(work).result(timeout=3)
    assert name.startswith("test-runtime-blocking")


def test_run_blocking_propagates_the_return_value(rt):
    assert rt.run_blocking(lambda: "done").result(timeout=3) == "done"


def test_spawn_logs_failures_instead_of_losing_them(rt, caplog):
    """An anonymous thread that died took its traceback with it. Not anymore."""
    async def explode():
        raise RuntimeError("background boom")

    with caplog.at_level(logging.ERROR, logger="voitta-desktop.runtime"):
        future = rt.spawn(explode(), name="exploder")
        with pytest.raises(RuntimeError):
            future.result(timeout=3)

    assert _wait_for_log(caplog, "exploder")
    assert "background boom" in caplog.text


def test_run_blocking_logs_failures(rt, caplog):
    def explode():
        raise ValueError("blocking boom")

    with caplog.at_level(logging.ERROR, logger="voitta-desktop.runtime"):
        with pytest.raises(ValueError):
            rt.run_blocking(explode).result(timeout=3)

    assert _wait_for_log(caplog, "blocking boom")


def test_call_later_eventually_runs(rt):
    """Replaces threading.Timer for OAuth token refresh."""
    done = threading.Event()
    rt.call_later(0.01, done.set)

    assert done.wait(timeout=3)


def test_call_later_is_cancellable(rt):
    """The refresh scheduler cancels and reschedules; that must still work."""
    fired = threading.Event()
    handle = rt.call_later(5.0, fired.set)
    handle.cancel()

    assert not fired.wait(timeout=0.3)


def test_concurrent_work_shares_one_loop(rt):
    """Many callers, one loop — no thread per caller."""
    async def which_loop():
        return id(asyncio.get_running_loop())

    loops = {rt.submit(which_loop()).result(timeout=3) for _ in range(10)}
    assert len(loops) == 1


def test_start_is_idempotent(rt):
    rt.start()
    assert rt.running


def test_shutdown_is_idempotent(rt):
    rt.shutdown(timeout=3.0)
    rt.shutdown(timeout=3.0)
    assert not rt.running


def test_loop_before_start_is_an_error():
    """Better a clear error than silently scheduling onto nothing."""
    with pytest.raises(RuntimeError):
        AsyncRuntime().loop


def test_shutdown_cancels_pending_work():
    """A hung task must not keep the process alive at quit."""
    runtime = AsyncRuntime(name="drain-test")
    runtime.start()

    async def forever():
        await asyncio.sleep(3600)

    runtime.spawn(forever(), name="forever")
    runtime.shutdown(timeout=3.0)

    assert not runtime._thread.is_alive()
