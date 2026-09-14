"""Transcript parsing: chain walking, turn grouping, defensive handling."""

import json

from middleware.transcripts import TranscriptStore


SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _write(tmp_path, lines, sid=SID):
    proj = tmp_path / "projects" / "-Users-x-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{sid}.jsonl"
    path.write_text("\n".join(
        line if isinstance(line, str) else json.dumps(line) for line in lines
    ) + "\n")
    return TranscriptStore(projects_dir=tmp_path / "projects"), path


def _user(uuid, parent, text, **kw):
    e = {"type": "user", "uuid": uuid, "parentUuid": parent,
         "timestamp": f"2026-08-27T10:00:0{uuid[-1]}Z",
         "message": {"role": "user", "content": text}}
    e.update(kw)
    return e


def _asst(uuid, parent, text, usage=None):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent,
            "timestamp": f"2026-08-27T10:00:0{uuid[-1]}Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text", "text": text}],
                        "usage": usage or {}}}


def test_basic_two_turns(tmp_path):
    store, path = _write(tmp_path, [
        _user("u1", None, "first question"),
        _asst("a1", "u1", "first answer",
              usage={"input_tokens": 10, "output_tokens": 5,
                     "cache_read_input_tokens": 100}),
        _user("u2", "a1", "second question"),
        _asst("a2", "u2", "second answer",
              usage={"input_tokens": 20, "output_tokens": 7}),
    ])
    result = store.parse(path)
    turns = result["turns"]
    assert len(turns) == 2
    assert turns[0]["usage"]["cache_read_input_tokens"] == 100
    assert turns[1]["entries"][0]["blocks"][0]["text"] == "second question"


def test_tool_result_stays_in_same_turn(tmp_path):
    store, path = _write(tmp_path, [
        _user("u1", None, "run something"),
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "ls -la"}}], "usage": {"output_tokens": 3}}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "total 42\nfile.txt"}]}},
        _asst("a2", "u2", "done"),
    ])
    turns = store.parse(path)["turns"]
    assert len(turns) == 1
    kinds = [b["t"] for e in turns[0]["entries"] for b in e["blocks"]]
    assert kinds == ["text", "tool_use", "tool_result", "text"]


def test_abandoned_branch_is_dropped(tmp_path):
    """A rewind leaves a dead branch in the file; only the live chain renders."""
    store, path = _write(tmp_path, [
        _user("u1", None, "question"),
        _asst("a1", "u1", "dead-end answer"),
        _asst("a2", "u1", "retried answer"),  # branch: same parent
        _user("u3", "a2", "follow-up"),
        _asst("a3", "u3", "final"),
    ])
    turns = store.parse(path)["turns"]
    texts = [b["text"] for t in turns for e in t["entries"] for b in e["blocks"]]
    assert "dead-end answer" not in texts
    assert "retried answer" in texts


def test_compact_boundary_becomes_marker_turn(tmp_path):
    store, path = _write(tmp_path, [
        _user("u1", None, "before compact"),
        _asst("a1", "u1", "answer"),
        {"type": "system", "subtype": "compact_boundary", "uuid": "s1",
         "parentUuid": "a1", "timestamp": "2026-08-27T10:00:05Z"},
        _user("u2", "s1", "after compact"),
        _asst("a2", "u2", "answer two"),
    ])
    turns = store.parse(path)["turns"]
    assert [t["compact"] for t in turns] == [False, True, False]


def test_junk_lines_and_unknown_types_are_skipped(tmp_path):
    store, path = _write(tmp_path, [
        "not json at all {{{",
        {"type": "file-history-snapshot", "uuid": "f1"},
        {"type": "ai-title", "title": "whatever"},
        _user("u1", None, "real question"),
        _asst("a1", "u1", "real answer"),
        {"type": "queue-operation", "uuid": "q1"},
    ])
    turns = store.parse(path)["turns"]
    assert len(turns) == 1
    assert len(turns[0]["entries"]) == 2


def test_thinking_and_caps(tmp_path):
    big = "x" * 50_000
    store, path = _write(tmp_path, [
        _user("u1", None, "q"),
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": big},
             {"type": "text", "text": "short"}], "usage": {}}},
    ])
    blocks = store.parse(path)["turns"][0]["entries"][1]["blocks"]
    assert blocks[0]["t"] == "thinking"
    assert len(blocks[0]["text"]) == 16_000
    assert blocks[0]["chars"] == 50_000


def test_missing_file_degrades_to_empty(tmp_path):
    store = TranscriptStore(projects_dir=tmp_path / "nope")
    assert store.find_main(SID) is None
    assert store.list_agents(SID) == []
    assert store.find_agent_by_seed(SID, "anything") is None


def test_task_block_carries_agent_hint(tmp_path):
    store, path = _write(tmp_path, [
        _user("u1", None, "go"),
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Task",
              "input": {"description": "Probe things",
                        "subagent_type": "Explore",
                        "prompt": "Look at every file."}}], "usage": {}}},
    ])
    block = store.parse(path)["turns"][0]["entries"][1]["blocks"][0]
    assert block["task"]["description"] == "Probe things"
    assert block["task"]["prompt_seed"] == "Look at every file."


def test_raw_entry_roundtrip(tmp_path):
    store, path = _write(tmp_path, [
        _user("u1", None, "hello"),
        _asst("a1", "u1", "world"),
    ])
    raw = store.raw_entry(path, "a1")
    assert json.loads(raw)["uuid"] == "a1"
