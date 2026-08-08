"""Conversation grouping must never refuse to proxy a request.

``_session_id`` used to raise when X-Claude-Code-Session-Id was absent. The
proxy is a plain Anthropic endpoint, so any client that isn't Claude Code
hit that path — and a raise there became a 502, meaning the proxy declined
to forward a request it could have forwarded fine, just without grouping it.
"""

from middleware.base import ProxyRequest
from middleware.tracker import ConversationTracker


def _request(headers=None):
    return ProxyRequest(
        method="POST",
        path="/v1/messages",
        headers=headers or {},
        body=b"{}",
    )


def _body(text="hello"):
    return {"messages": [{"role": "user", "content": text}]}


def test_header_wins_when_present():
    tracker = ConversationTracker()
    request = _request({"X-Claude-Code-Session-Id": "sess-42"})

    assert tracker._session_id(request, _body()) == "sess-42"


def test_missing_header_does_not_raise():
    """The bug: this used to raise and surface to the caller as a 502."""
    tracker = ConversationTracker()

    assert tracker._session_id(_request(), _body())


def test_fallback_is_stable_across_turns():
    """Grouping only works if the same conversation keeps the same id.

    Every turn re-sends the whole history, so the first user message is a
    stable seed even as the conversation grows.
    """
    tracker = ConversationTracker()
    turn_one = {"messages": [{"role": "user", "content": "first question"}]}
    turn_three = {
        "messages": [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "an answer"},
            {"role": "user", "content": "a follow-up"},
        ]
    }

    assert tracker._session_id(_request(), turn_one) == \
           tracker._session_id(_request(), turn_three)


def test_different_conversations_get_different_ids():
    tracker = ConversationTracker()

    assert tracker._session_id(_request(), _body("about cats")) != \
           tracker._session_id(_request(), _body("about dogs"))


def test_block_style_content_is_handled():
    """Anthropic accepts content as a list of blocks, not just a string."""
    tracker = ConversationTracker()
    blocks = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "block style"}]}
        ]
    }

    assert tracker._session_id(_request(), blocks).startswith("anon-")


def test_block_and_string_forms_agree():
    """The same text either way is the same conversation."""
    tracker = ConversationTracker()
    as_string = {"messages": [{"role": "user", "content": "same text"}]}
    as_blocks = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "same text"}]}
        ]
    }

    assert tracker._session_id(_request(), as_string) == \
           tracker._session_id(_request(), as_blocks)


def test_unusable_body_still_yields_an_id():
    """Last resort — one shared bucket beats refusing the request."""
    tracker = ConversationTracker()

    assert tracker._session_id(_request(), {"messages": []}) == "anon-unknown"
    assert tracker._session_id(_request(), {}) == "anon-unknown"


def test_image_only_message_does_not_crash():
    """A first message with no text at all must not blow up the lookup."""
    tracker = ConversationTracker()
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "image", "source": {"data": "x"}}]}
        ]
    }

    assert tracker._session_id(_request(), body) == "anon-unknown"
