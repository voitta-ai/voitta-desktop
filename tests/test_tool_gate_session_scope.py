"""The gate remembers answers per session, not globally.

Two requirements pull in opposite directions:

* A client abandons a tools/list after about five seconds, long before a
  human answers, then retries. The popup outlives that cancellation and
  publishes its answer, so the retry must find it — a window of minutes.
* A new session must get its own popup rather than silently inheriting
  whatever an earlier session chose.

A single global answer plus a minutes-long window satisfies the first and
breaks the second: every new session was served the previous session's
choice with no prompt. Keying by session satisfies both.
"""

import time

import pytest

from mcpproxy.server import ToolGateMiddleware


class _App:
    terminal_mode = False
    suppress_codex_popup = True
    disabled_tools: set[str] = set()


@pytest.fixture
def gate():
    return ToolGateMiddleware(_App())


def test_a_sessions_answer_is_reused(gate):
    """The retry-after-cancellation path: same session, no second popup."""
    gate._remember("sess-a", {"tool_x"})

    recalled = gate._recall("sess-a")
    assert recalled is not None
    assert recalled[0] == {"tool_x"}


def test_a_new_session_gets_no_answer(gate):
    """The regression: a fresh session must prompt, not inherit."""
    gate._remember("sess-a", {"tool_x"})

    assert gate._recall("sess-b") is None


def test_sessions_do_not_overwrite_each_other(gate):
    gate._remember("sess-a", {"tool_x"})
    gate._remember("sess-b", {"tool_y"})

    assert gate._recall("sess-a")[0] == {"tool_x"}
    assert gate._recall("sess-b")[0] == {"tool_y"}


def _skip_ahead(monkeypatch, gate):
    """Move time.time() past the TTL, using the same clock the code reads."""
    later = time.time() + gate.ANSWER_TTL_S + 10
    monkeypatch.setattr(time, "time", lambda: later)


def test_answers_expire(gate, monkeypatch):
    """Bounds memory and lets a very long-lived session be re-gated."""
    gate._remember("sess-a", {"tool_x"})
    _skip_ahead(monkeypatch, gate)

    assert gate._recall("sess-a") is None


def test_expired_entries_are_dropped(gate, monkeypatch):
    gate._remember("sess-a", {"tool_x"})
    _skip_ahead(monkeypatch, gate)
    gate._recall("sess-a")

    assert "sess-a" not in gate._answers


def test_rearm_forgets_every_session(gate):
    """The menu's recovery path — a mistaken Cancel must be undoable."""
    gate._remember("sess-a", {"tool_x"})
    gate._remember("sess-b", {"tool_y"})

    gate.rearm()

    assert gate._recall("sess-a") is None
    assert gate._recall("sess-b") is None


def test_remembered_answers_are_bounded(gate):
    """A long-running proxy must not accumulate sessions without limit."""
    for i in range(gate.MAX_REMEMBERED + 20):
        gate._remember(f"sess-{i}", {"tool_x"})

    assert len(gate._answers) <= gate.MAX_REMEMBERED


def test_eviction_drops_the_oldest(gate):
    gate._remember("oldest", {"tool_x"})
    time.sleep(0.01)
    for i in range(gate.MAX_REMEMBERED):
        gate._remember(f"sess-{i}", {"tool_y"})

    assert gate._recall("oldest") is None
    assert gate._recall(f"sess-{gate.MAX_REMEMBERED - 1}") is not None


def test_sessionless_clients_share_one_slot(gate):
    """They still need their own retries coalesced."""
    gate._remember(gate.ANON_KEY, {"tool_x"})

    assert gate._recall(gate.ANON_KEY)[0] == {"tool_x"}


def test_cancel_is_remembered_as_everything_disabled(gate):
    """Cancel means 'allow nothing'. An empty set would leak every tool."""
    all_names = {"tool_x", "tool_y", "tool_z"}
    gate._remember("sess-a", all_names)

    assert gate._recall("sess-a")[0] == all_names
