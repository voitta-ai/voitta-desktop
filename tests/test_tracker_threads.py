"""Thread splitting: one session, several API threads, no clobbering.

A Claude Code session multiplexes the main conversation and Task sub-agent
threads over the same X-Claude-Code-Session-Id. Keying by session id alone
meant whichever thread completed last replaced ``conv.turns`` wholesale.
Threads are now keyed by (session, first-message hash); sub-agent threads
become child conversations attributed via the on-disk transcript.
"""

import json

from middleware.base import ProxyRequest
from middleware.tracker import ConversationTracker
from middleware.transcripts import TranscriptStore


SID = "11111111-2222-3333-4444-555555555555"


def _request(sid=SID):
    headers = {"X-Claude-Code-Session-Id": sid} if sid else {}
    return ProxyRequest(method="POST", path="/v1/messages", headers=headers, body=b"{}")


def _body(first_text, extra=0):
    msgs = [{"role": "user", "content": first_text}]
    for i in range(extra):
        msgs.append({"role": "assistant", "content": f"answer {i}"})
        msgs.append({"role": "user", "content": f"follow-up {i}"})
    return {"messages": msgs}


def _store_with_agent(tmp_path, prompt, agent_id="abc123"):
    """A projects dir holding one session transcript + one sub-agent file."""
    proj = tmp_path / "projects" / "-Users-x-proj"
    sub = proj / SID / "subagents"
    sub.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text(
        json.dumps({"type": "user", "uuid": "u1",
                    "message": {"role": "user", "content": "main question"}}) + "\n"
    )
    (sub / f"agent-{agent_id}.jsonl").write_text(
        json.dumps({"type": "user", "uuid": "a1", "isSidechain": True,
                    "message": {"role": "user", "content": prompt}}) + "\n"
    )
    return TranscriptStore(projects_dir=tmp_path / "projects")


def test_first_thread_claims_session_id(tmp_path):
    tracker = ConversationTracker(TranscriptStore(projects_dir=tmp_path))
    assert tracker._session_id(_request(), _body("main question")) == SID


def test_same_thread_resolves_stably_as_history_grows(tmp_path):
    tracker = ConversationTracker(TranscriptStore(projects_dir=tmp_path))
    a = tracker._session_id(_request(), _body("main question"))
    b = tracker._session_id(_request(), _body("main question", extra=3))
    assert a == b == SID


def test_subagent_thread_becomes_child_with_agent_id(tmp_path):
    prompt = "Search the codebase for cache_control usage and report back."
    store = _store_with_agent(tmp_path, prompt)
    tracker = ConversationTracker(store)

    main_id, main_parent, _ = tracker._resolve_thread(_request(), _body("main question"))
    child_id, child_parent, agent_id = tracker._resolve_thread(_request(), _body(prompt))

    assert main_id == SID and main_parent == ""
    assert child_id != SID and child_id.startswith(SID + "#")
    assert child_parent == SID
    assert agent_id == "abc123"


def test_subagent_never_clobbers_main_turns(tmp_path):
    """The original bug: last-finished thread overwrote conv.turns."""
    prompt = "Explore middleware internals."
    store = _store_with_agent(tmp_path, prompt)
    tracker = ConversationTracker(store)

    main_id = tracker._session_id(_request(), _body("main question", extra=5))
    child_id = tracker._session_id(_request(), _body(prompt))
    assert main_id != child_id


def test_unmatched_second_thread_still_splits(tmp_path):
    """No transcript on disk yet (race at agent spawn): the thread must
    still split off — attribution can arrive later, clobbering can't."""
    tracker = ConversationTracker(TranscriptStore(projects_dir=tmp_path))
    main_id = tracker._session_id(_request(), _body("main question"))
    other_id = tracker._session_id(_request(), _body("some unrelated thread"))
    assert main_id == SID
    assert other_id.startswith(SID + "#")


def test_compact_continuation_keeps_main_id(tmp_path):
    tracker = ConversationTracker(TranscriptStore(projects_dir=tmp_path))
    tracker._session_id(_request(), _body("main question"))
    compacted = _body(
        "This session is being continued from a previous conversation that "
        "ran out of context. Summary: ..."
    )
    assert tracker._session_id(_request(), compacted) == SID


def test_headerless_subagent_attributed_via_transcript(tmp_path):
    prompt = "Run the release checklist."
    store = _store_with_agent(tmp_path, prompt, agent_id="zz9")
    tracker = ConversationTracker(store)
    tracker._session_id(_request(), _body("main question"))  # session known

    cid, parent, agent = tracker._resolve_thread(_request(sid=None), _body(prompt))
    assert parent == SID
    assert agent == "zz9"
    assert cid.startswith(SID + "#")


def test_headerless_unknown_stays_anonymous(tmp_path):
    tracker = ConversationTracker(TranscriptStore(projects_dir=tmp_path))
    cid = tracker._session_id(_request(sid=None), _body("hello there"))
    assert cid.startswith("anon-")


def test_peek_thread_id_is_read_only(tmp_path):
    tracker = ConversationTracker(TranscriptStore(projects_dir=tmp_path))
    body = _body("main question")
    assert tracker.peek_thread_id(SID, body) == SID  # nothing registered yet
    assert tracker._threads == {}
    tracker._session_id(_request(), body)
    assert tracker.peek_thread_id(SID, body) == SID
