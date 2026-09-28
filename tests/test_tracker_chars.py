"""Per-turn ``chars_in`` must keep its exact values while dropping the
quadratic serialization that produced them.

The tracker used to compute each turn's inbound size as
``len(json.dumps(messages[:turn_end]))``, once per turn. That re-serialized
the whole prefix every time: on a 697-turn, 39 MB context it produced 19 GB of
JSON and took 34 s per response, on the shared event loop. ``_prefix_chars``
derives the same numbers from a single pass, so these tests pin the
equivalence rather than the new implementation's internals.
"""

import json

from middleware.tracker import _prefix_chars


def _old(messages, k):
    """The expression being replaced."""
    return len(json.dumps(messages[:k]))


def test_matches_the_old_expression_for_every_prefix():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "ls -la /tmp && echo done"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a\nb\nc"},
            {"type": "text", "text": "<system-reminder>x</system-reminder>"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    got = _prefix_chars(messages)
    assert len(got) == len(messages) + 1
    for k in range(len(messages) + 1):
        assert got[k] == _old(messages, k), f"prefix {k}: {got[k]} != {_old(messages, k)}"


def test_handles_the_empty_and_single_message_edges():
    assert _prefix_chars([]) == [2]                     # json.dumps([]) == "[]"
    one = [{"role": "user", "content": "x"}]
    assert _prefix_chars(one) == [2, _old(one, 1)]


def test_matches_with_unicode_and_escapes():
    """Lengths are of the serialized form, so escaping and non-ASCII must be
    counted the way json.dumps counts them, not by raw string length."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": 'quote " and \\ and \n'}]},
        {"role": "assistant", "content": [{"type": "text", "text": "héllo → 世界"}]},
    ]
    got = _prefix_chars(messages)
    for k in range(len(messages) + 1):
        assert got[k] == _old(messages, k)


def test_scales_linearly_not_quadratically():
    """The point of the change: doubling the conversation must roughly double
    the work, not quadruple it."""
    import time

    def timed(n):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "x" * 2000}]}
                for _ in range(n)]
        t = time.perf_counter()
        _prefix_chars(msgs)
        return time.perf_counter() - t

    timed(200)                                          # warm up
    small, large = timed(400), timed(800)
    # quadratic would be ~4x; allow generous headroom for a noisy machine
    assert large < small * 3, f"{small:.4f}s -> {large:.4f}s looks superlinear"
