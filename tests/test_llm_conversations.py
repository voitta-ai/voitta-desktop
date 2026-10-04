"""/voitta-store and stored conversations: the journal, the copy, and what the window does with it."""

import json
import stat
import zipfile

import pytest
from aiohttp.test_utils import TestServer

from llmgw import conversations as conv
from llmgw.web import LlmAccounts
from middleware.transcripts import TranscriptStore
from proxy import AnthropicProxy
from tests.llm_fake_upstream import FakeUpstream
from tests.test_llm_routing import Client, _free_port, body, use

SESSION = "4ad29fe4-cc08-4bad-9b0a-69faaa62ba18"


def rec(uuid, parent, type_, content, **kw):
    role = "assistant" if type_ == "assistant" else "user"
    msg = {"role": role, "content": content}
    if type_ == "assistant":
        msg["model"] = kw.pop("model", "claude-opus-5-5")
    return {"type": type_, "uuid": uuid, "parentUuid": parent, "sessionId": SESSION,
            "timestamp": kw.pop("ts", "2026-10-03T14:00:00.000Z"), "message": msg, **kw}


def transcript_lines():
    """Two turns, a compaction (boundary with no parent, linked back through
    logicalParentUuid), a summary, then one more turn."""
    return [
        rec("u1", None, "user", "first question", cwd="/work/proj", version="2.1.288"),
        rec("a1", "u1", "assistant", [{"type": "text", "text": "answer one"},
                                      {"type": "tool_use", "id": "t1", "name": "Bash",
                                       "input": {"command": "echo ```fenced```", "description": "Print"}}],
            effort="high", version="2.1.288"),
        rec("r1", "a1", "user", [{"type": "tool_result", "tool_use_id": "t1", "content": "```fenced```"}]),
        rec("a2", "r1", "assistant", [{"type": "text", "text": "done with one"}]),
        {"type": "system", "subtype": "compact_boundary", "uuid": "cb", "parentUuid": None,
         "logicalParentUuid": "a2", "timestamp": "2026-10-03T15:00:00.000Z"},
        rec("s1", "cb", "user", "Summary of the earlier conversation", isCompactSummary=True),
        rec("u2", "s1", "user", "after compact", ts="2026-10-03T15:01:00.000Z"),
        rec("a3", "u2", "assistant", [{"type": "text", "text": "answer two"}], ts="2026-10-03T15:01:05.000Z"),
        {"type": "ai-title", "aiTitle": "Testing the store", "sessionId": SESSION},
    ]


def write_transcript(project, records, tail='{"half-written'):
    path = project / f"{SESSION}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail)
    return path


@pytest.fixture
def claude(tmp_path):
    projects = tmp_path / "claude" / "projects"
    project = projects / "-work-proj"
    (project / SESSION / "subagents").mkdir(parents=True)
    transcript = write_transcript(project, transcript_lines())
    (project / SESSION / "subagents" / "agent-1.jsonl").write_text(
        json.dumps(rec("x1", None, "user", "explore the repo")) + "\n"
        + json.dumps(rec("x2", "x1", "assistant", [{"type": "text", "text": "found it"}],
                         model="claude-haiku-4-5")) + "\n")
    (project / SESSION / "subagents" / "agent-1.meta.json").write_text('{"agentType": "Explore"}')
    return projects, transcript


def store(tmp_path, transcript, routing=()):
    return conv.store(tmp_path / "conversations", SESSION, transcript, list(routing),
                      claude_code={"model": "claude-opus-5-5"}, llm={"route": "default", "account": {"label": "As is"}})


# ---- the full history ------------------------------------------------------------

def test_full_history_follows_compactions_back(claude):
    _, transcript = claude
    texts = lambda parsed: [b.get("text") for t in parsed["turns"] for e in t["entries"] for b in e["blocks"]
                            if b.get("t") == "text"]
    live = TranscriptStore().parse(transcript)
    full = TranscriptStore().parse(transcript, full_history=True)
    assert "first question" not in texts(live)          # the live Explorer: what the model sees now
    assert texts(full)[0] == "first question"           # stored view: everything
    assert "after compact" in texts(full) and "answer two" in texts(full)
    assert sum(t["compact"] for t in full["turns"]) == 1


# ---- storing -----------------------------------------------------------------------

def test_store_copies_files_byte_for_byte(tmp_path, claude):
    _, transcript = claude
    meta = store(tmp_path, transcript, [{"ts": 1, "model": "m", "status": 200}])
    d = tmp_path / "conversations" / SESSION
    assert (d / "transcript.jsonl").read_bytes() == transcript.read_bytes()
    assert (d / "subagents" / "agent-1.meta.json").read_text() == '{"agentType": "Explore"}'
    assert sorted(p.name for p in d.iterdir()) == ["meta.json", "routing.jsonl", "subagents", "transcript.jsonl"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in d.rglob("*") if p.is_file())
    assert meta["title"] == "Testing the store" and meta["project"] == "proj"
    assert meta["first_prompt"] == "first question"
    assert meta["models"]["requested"] == {"claude-opus-5-5": 3, "claude-haiku-4-5": 1}
    assert meta["subagents"] == [{"id": "agent-1", "label": "explore the repo"}]
    assert meta["counts"]["unreadable_lines"] == 1 and meta["bytes"] > 0
    # again: replaced, never duplicated
    store(tmp_path, transcript)
    assert [p.name for p in (tmp_path / "conversations").iterdir()] == [SESSION]


def test_list_count_delete_and_listeners(tmp_path, claude):
    _, transcript = claude
    events = []
    conv.on_change(lambda: events.append(1))
    try:
        folder = tmp_path / "conversations"
        assert conv.list_conversations(folder) == [] and conv.count(folder) == 0
        store(tmp_path, transcript)
        assert [m["session_id"] for m in conv.list_conversations(folder)] == [SESSION]
        assert conv.count(folder) == 1
        assert conv.delete(folder, ["../etc", "0000-not-stored", SESSION]) == 1
        assert conv.count(folder) == 0 and len(events) == 2
        assert transcript.exists()  # Claude Code's own file is never touched
    finally:
        conv._listeners.clear()


def test_old_single_file_format_moves_to_a_folder(tmp_path, claude):
    _, transcript = claude
    folder = tmp_path / "conversations"
    folder.mkdir()
    records = [json.loads(line) for line in transcript.read_text().splitlines()[:-1]]
    (folder / f"{SESSION}.json").write_text(json.dumps({
        "format": "voitta-conversation/1", "session_id": SESSION, "stored_at": "2026-10-04T10:12:00+0200",
        "transcript": records, "subagents": {}, "routing": [], "llm": {}, "claude_code": {}}))
    [meta] = conv.list_conversations(folder)
    assert meta["title"] == "Testing the store" and meta["stored_at"] == "2026-10-04T10:12:00+0200"
    assert not (folder / f"{SESSION}.json").exists()
    assert len((folder / SESSION / "transcript.jsonl").read_text().splitlines()) == len(records)

    # an old copy next to a newer folder: storing again (or listing) drops it
    (folder / f"{SESSION}.json").write_text("{}")
    store(tmp_path, transcript)
    assert not (folder / f"{SESSION}.json").exists()
    (folder / f"{SESSION}.json").write_text("{}")
    assert len(conv.list_conversations(folder)) == 1 and not (folder / f"{SESSION}.json").exists()


# ---- who answered ---------------------------------------------------------------------

def test_route_for_needs_exactly_one_fitting_request():
    t = conv._parse_ts("2026-10-03T15:01:05.000Z")
    r1 = {"ts": t - 4, "ms": 3000, "model": "claude-opus-5-5", "status": 200, "account": "DeepSeek",
          "route": "window", "upstream": {"host": "api.deepseek.com", "model": "deepseek-v4-pro"}}
    assert conv.route_for("2026-10-03T15:01:05.000Z", "claude-opus-5-5", [r1]) is r1
    assert conv.route_label(r1) == "deepseek-v4-pro @ api.deepseek.com · DeepSeek · window pick"
    assert conv.route_for("2026-10-03T15:01:05.000Z", "claude-haiku-4-5", [r1]) is None   # other model
    assert conv.route_for("2026-10-03T15:01:05.000Z", "claude-opus-5-5", [r1, dict(r1)]) is None  # ambiguous
    assert conv.route_for("2026-10-03T16:00:00.000Z", "claude-opus-5-5", [r1]) is None   # outside the window


# ---- exports --------------------------------------------------------------------------

def test_exports(tmp_path, claude):
    _, transcript = claude
    store(tmp_path, transcript)
    folder = tmp_path / "conversations"
    md = conv.to_markdown(folder, SESSION)
    assert md.startswith("# Testing the store")
    assert md.index("first question") < md.index("Context compacted here") < md.index("after compact")
    assert "````\n```fenced```\n````" in md          # fences longer than anything inside
    assert "# Subagent: explore the repo" in md and "found it" in md
    assert "**Claude** · claude-opus-5-5" in md

    doc = json.loads(conv.to_json(folder, SESSION))
    assert doc["format"] == "voitta-conversation/1" and len(doc["transcript"]) == 9
    assert list(doc["subagents"]) == ["agent-1"]

    out = conv.export(folder, [SESSION], "jsonl", tmp_path / "t.jsonl")
    assert out.read_bytes() == transcript.read_bytes()

    other = "11111111-2222-3333-4444-555555555555"
    conv.store(folder, other, transcript, [], claude_code={}, llm={})
    z = conv.export(folder, [SESSION, other], "md", tmp_path / "both.zip")
    with zipfile.ZipFile(z) as zf:
        assert sorted(zf.namelist()) == ["Testing-the-store-11111111.md", "Testing-the-store-4ad29fe4.md"]
    with pytest.raises(ValueError):
        conv.export(folder, [SESSION], "pdf", tmp_path / "x.pdf")


# ---- through Voitta's real proxy ----------------------------------------------------------

@pytest.fixture
async def env(tmp_path, claude):
    projects, transcript = claude
    fake = FakeUpstream()
    upstream = TestServer(fake.app())
    await upstream.start_server()
    base = str(upstream.make_url("")).rstrip("/")
    (tmp_path / "elsewhere.jsonl").write_text("{}\n")
    port = _free_port()
    llm = LlmAccounts(port, data_dir=tmp_path / "llm", claude_projects=[projects])
    proxy = AnthropicProxy(middlewares=[], port=port, upstream_url=base, llm=llm)
    await proxy.start()
    client = Client(port)
    yield llm, client, base, tmp_path, transcript
    await client.close()
    await proxy.stop()
    await upstream.close()


async def test_journal_records_who_answered_each_window_request(env):
    llm, client, base, tmp_path, _ = env
    use(llm.store, "mistral", "k", "Mistral", {"api_key": "sk"}, base_url=f"{base}/v1",
        models={"big": "mistral-large-latest", "small": "mistral-small-latest"})
    r = await client.post("/v1/messages", json=body(False), headers={"x-claude-code-session-id": SESSION})
    assert r.status == 200
    await client.post("/v1/messages/count_tokens", json=body(False), headers={"x-claude-code-session-id": SESSION})
    await client.post("/v1/messages", json=body(False))  # no session: nothing to file it under

    [rec_] = llm.journal.read(SESSION)  # count_tokens isn't a model answer
    assert rec_["account"] == "Mistral" and rec_["account_id"] == "mistral:k" and rec_["route"] == "default"
    assert rec_["model"] == "claude-sonnet-4-5" and rec_["upstream_model"] == "mistral-large-latest"
    assert rec_["status"] == 200 and rec_["upstream"]["host"] == "127.0.0.1"
    assert stat.S_IMODE(llm.journal.path(SESSION).stat().st_mode) == 0o600


async def test_store_endpoint(env):
    llm, client, base, tmp_path, transcript = env
    use(llm.store, "mistral", "k", "Mistral", {"api_key": "sk"}, base_url=f"{base}/v1",
        models={"big": "mistral-large-latest", "small": "mistral-small-latest"})
    await client.post("/v1/messages", json=body(False), headers={"x-claude-code-session-id": SESSION})

    r = await client.post("/_voitta/llm/api/conversations", json={
        "session": SESSION, "model": "claude-opus-5-5", "cwd": "/work/proj", "transcript_path": str(transcript)})
    out = await r.json()
    assert r.status == 200, out
    assert out["path"] == str(tmp_path / "conversations" / SESSION) and out["records"] == 9
    meta = conv.load_meta(tmp_path / "conversations", SESSION)
    assert meta["models"]["answered_by"] == {"mistral-large-latest @ 127.0.0.1": 1}
    assert meta["llm"]["account"]["id"] == "mistral:k" and "credentials" not in json.dumps(meta)
    assert len(conv.routing(tmp_path / "conversations", SESSION)) == 1

    # refusals: bad id, a hint outside Claude's folder (ignored), unknown session, another website
    assert (await client.post("/_voitta/llm/api/conversations", json={"session": "../../etc/passwd"})).status == 400
    r = await client.post("/_voitta/llm/api/conversations", json={
        "session": SESSION, "transcript_path": str(tmp_path / "elsewhere.jsonl")})
    assert r.status == 200 and (await r.json())["records"] == 9
    assert (await client.post("/_voitta/llm/api/conversations",
                              json={"session": "0000-unknown-session"})).status == 404
    assert (await client.post("/_voitta/llm/api/conversations", json={"session": SESSION},
                              headers={"Origin": "https://evil.example"})).status == 403


# ---- the window's backend (no AppKit window needed) ------------------------------------

def test_window_rpc(tmp_path, claude):
    from ui.stored_conversations import StoredConversationsMixin

    class Host(StoredConversationsMixin):
        class _llm:
            conversations_dir = tmp_path / "conversations"

    _, transcript = claude
    t = conv._parse_ts("2026-10-03T15:01:05.000Z")
    store(tmp_path, transcript, [{"ts": t - 2, "ms": 1500, "model": "claude-opus-5-5", "status": 200,
                                  "account": "DeepSeek", "route": "window",
                                  "upstream": {"host": "api.deepseek.com", "model": "deepseek-v4-pro"}}])
    host = Host()
    assert [m["session_id"] for m in host._stored_rpc("list", {})["conversations"]] == [SESSION]
    r = host._stored_rpc("transcript", {"conv_id": SESSION})
    entries = [e for turn in r["turns"] for e in turn["entries"]]
    assert entries[0]["blocks"][0]["text"] == "first question"            # full history
    assert [e.get("route") for e in entries if e["role"] == "assistant"][-1] == \
        "deepseek-v4-pro @ api.deepseek.com · DeepSeek · window pick"
    sub = host._stored_rpc("transcript", {"conv_id": SESSION + "@agent-1"})
    assert sub["turns"][0]["entries"][0]["blocks"][0]["text"] == "explore the repo"
    assert "empty" in host._stored_rpc("transcript", {"conv_id": SESSION + "@../../x"})
    assert "empty" in host._stored_rpc("transcript", {"conv_id": "not-a-session"})
    assert len(host._stored_rpc("routing", {"session": SESSION})["records"]) == 1
    assert '"first question"' in host._stored_rpc("raw", {"conv_id": SESSION, "uuid": "u1"})["json"]
    assert host._stored_rpc("delete", {"sessions": [SESSION]}) == {"deleted": 1, "list": {"conversations": []}}
    assert host._stored_menu_title() == "Stored conversations…"


def test_menu_hook_needs_no_runtime(tmp_path, monkeypatch):
    """The menu is built before the runtime starts (app crashed at launch when it wasn't)."""
    import runtime as rt
    from ui.stored_conversations import StoredConversationsMixin

    def not_started(*a, **k):
        raise RuntimeError("AsyncRuntime.start() has not been called")
    monkeypatch.setattr(rt.runtime, "run_blocking", not_started)
    monkeypatch.setattr(rt.runtime, "submit", not_started, raising=False)

    class Host(StoredConversationsMixin):
        class _llm:
            conversations_dir = tmp_path / "conversations"
    try:
        Host()._stored_watch()
        assert Host()._stored_menu_title() == "Stored conversations…"
    finally:
        conv._listeners.clear()


# ---- system prompt and tools (not in Claude Code's transcript) ------------------------------

from llmgw.context import ContextRecorder, read_index  # noqa: E402
from middleware.tracker import ConversationTracker  # noqa: E402

SYSTEM = [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1; cch=abc123;"},
          {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}]
TOOLS = [{"name": "Bash", "description": "Run a command", "input_schema": {"type": "object"}}]
HDRS = {"X-Claude-Code-Session-Id": SESSION}


def test_recorder_keeps_each_distinct_version_once(tmp_path):
    rc = ContextRecorder(tmp_path / "context")
    rc.record(SESSION, None, SYSTEM, TOOLS, "claude-opus-5-5")
    # same prompt, new billing hash: not a new version
    rc.record(SESSION, None, [{**SYSTEM[0], "text": "x-anthropic-billing-header: cc_version=2.1; cch=ffff00;"},
                              SYSTEM[1]], TOOLS, "claude-opus-5-5")
    rc.record(SESSION, None, SYSTEM, TOOLS + [{"name": "mcp__x", "input_schema": {}}], "claude-opus-5-5")
    rc.record(SESSION, "agent9", SYSTEM, TOOLS, "claude-haiku-4-5")     # a subagent: its own line
    rows = read_index(rc.dir_for(SESSION))
    assert [(r["agent"], r["tools_count"]) for r in rows] == [(None, 1), (None, 2), ("agent9", 1)]
    files = sorted(p.name for p in rc.dir_for(SESSION).iterdir())
    assert len([f for f in files if f.startswith("system-")]) == 1 and len([f for f in files if f.startswith("tools-")]) == 2
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in rc.dir_for(SESSION).iterdir())
    # a restart remembers what was already kept
    ContextRecorder(tmp_path / "context").record(SESSION, None, SYSTEM, TOOLS, "claude-opus-5-5")
    assert len(read_index(rc.dir_for(SESSION))) == 3
    # nothing to name a folder after, or nothing to keep: nothing happens
    rc.submit(None, {"x-claude-code-session-id": "../../etc"}, SYSTEM, TOOLS, "m")
    rc.submit(None, HDRS, None, None, "m")
    assert sorted(p.name for p in (tmp_path / "context").iterdir()) == [SESSION]


async def test_recorder_failure_never_reaches_the_request(tmp_path):
    import asyncio
    rc = ContextRecorder(tmp_path / "file-not-dir")
    (tmp_path / "file-not-dir").write_text("in the way")      # mkdir will fail inside the worker
    rc.submit(asyncio.get_running_loop(), HDRS, SYSTEM, TOOLS, "m")
    await asyncio.sleep(0.2)                                   # logged, not raised

    # and a listener that raises does not break the tracker
    tracker = ConversationTracker()
    tracker.on_context = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    from middleware.base import ProxyRequest, ProxyResponse
    req = ProxyRequest(method="POST", path="/v1/messages", headers=dict(HDRS),
                       body=json.dumps({"model": "claude-opus-5-5", "max_tokens": 10, "system": SYSTEM, "tools": TOOLS,
                                        "messages": [{"role": "user", "content": "hi"}]}).encode())
    await tracker.on_request(req)
    await tracker.on_response_done(req, ProxyResponse(status=200, headers={}))
    assert tracker.conversations                               # tracked as usual


async def test_recorded_through_the_proxy_and_kept_by_store(tmp_path, claude):
    import asyncio
    projects, transcript = claude
    fake = FakeUpstream()
    upstream = TestServer(fake.app())
    await upstream.start_server()
    port = _free_port()
    llm = LlmAccounts(port, data_dir=tmp_path / "llm", claude_projects=[projects])
    tracker = ConversationTracker()
    tracker.on_context = llm.record_context
    proxy = AnthropicProxy(middlewares=[tracker], port=port, upstream_url=str(upstream.make_url("")).rstrip("/"), llm=llm)
    await proxy.start()
    client = Client(port)
    try:
        r = await client.post("/v1/messages", headers=HDRS, json={
            "model": "claude-opus-5-5", "max_tokens": 10, "system": SYSTEM, "tools": TOOLS,
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status == 200
        sent = fake.requests[-1]["body"]
        assert sent["system"] == SYSTEM and sent["tools"] == TOOLS   # the request itself is untouched
        for _ in range(50):
            if read_index(llm.context.dir_for(SESSION)):
                break
            await asyncio.sleep(0.05)
        assert [r_["model"] for r_ in read_index(llm.context.dir_for(SESSION))] == ["claude-opus-5-5"]

        r = await client.post("/_voitta/llm/api/conversations", json={"session": SESSION})
        assert r.status == 200
        folder = tmp_path / "conversations"
        assert conv.load_meta(folder, SESSION)["context_versions"] == 1
        [v] = conv.context_versions(folder, SESSION)
        assert conv.context_parts(folder, SESSION, 0) == (SYSTEM, TOOLS)
        view = conv.context_view(folder, SESSION, 0)
        assert [t["name"] for t in view["tools"]] == ["Bash"] and view["system"][1]["cache"] is True
        md = conv.to_markdown(folder, SESSION)
        assert "## Context (system prompt and tools)" in md and "You are Claude Code." in md
        assert conv.UNAVAILABLE not in md
        assert json.loads(conv.to_json(folder, SESSION))["context"][0]["tools"] == TOOLS
    finally:
        await client.close()
        await proxy.stop()
        await upstream.close()


def test_unavailable_when_nothing_was_recorded(tmp_path, claude):
    _, transcript = claude
    store(tmp_path, transcript)                                  # no context_dir
    folder = tmp_path / "conversations"
    assert conv.load_meta(folder, SESSION)["context_versions"] == 0
    assert conv.context_versions(folder, SESSION) == [] and conv.default_version([], None) is None
    assert f"> {conv.UNAVAILABLE}" in conv.to_markdown(folder, SESSION)
    assert json.loads(conv.to_json(folder, SESSION))["context"] is None

    from ui.stored_conversations import StoredConversationsMixin

    class Host(StoredConversationsMixin):
        class _llm:
            conversations_dir = folder
    t = Host()._stored_rpc("transcript", {"conv_id": SESSION})
    assert t["overhead"] is None and t["note"] == conv.UNAVAILABLE and t["context_versions"] == []
