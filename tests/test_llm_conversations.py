"""/voitta-store: the routing journal and the conversation copy, through Voitta's real proxy."""

import json
import stat

import pytest
from aiohttp.test_utils import TestServer

from llmgw.web import LlmAccounts
from proxy import AnthropicProxy
from tests.llm_fake_upstream import FakeUpstream
from tests.test_llm_routing import Client, _free_port, body, use

SESSION = "4ad29fe4-cc08-4bad-9b0a-69faaa62ba18"


def transcript_lines():
    return [
        {"type": "user", "sessionId": SESSION, "cwd": "/work/proj", "version": "2.1.288",
         "message": {"role": "user", "content": "first question"}},
        {"type": "assistant", "sessionId": SESSION, "version": "2.1.288", "effort": "high",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "a1"}]}},
        # what /compact leaves: the summary; the turns above stay in the file
        {"type": "system", "subtype": "compact_boundary", "sessionId": SESSION},
        {"type": "user", "sessionId": SESSION, "message": {"role": "user", "content": "after compact"}},
        {"type": "assistant", "sessionId": SESSION, "version": "2.1.288",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "a2"}]}},
        {"type": "ai-title", "aiTitle": "Testing the store", "sessionId": SESSION},
    ]


@pytest.fixture
async def env(tmp_path):
    fake = FakeUpstream()
    upstream = TestServer(fake.app())
    await upstream.start_server()
    base = str(upstream.make_url("")).rstrip("/")

    projects = tmp_path / "claude" / "projects"
    project = projects / "-work-proj"
    (project / SESSION / "subagents").mkdir(parents=True)
    transcript = project / f"{SESSION}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in transcript_lines()) + '{"half-written')
    (project / SESSION / "subagents" / "agent-1.jsonl").write_text(json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-haiku-4-5", "content": []}}) + "\n")
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

    [rec] = llm.journal.read(SESSION)  # count_tokens isn't a model answer
    assert rec["account"] == "Mistral" and rec["account_id"] == "mistral:k" and rec["route"] == "default"
    assert rec["model"] == "claude-sonnet-4-5" and rec["upstream_model"] == "mistral-large-latest"
    assert rec["status"] == 200 and rec["upstream"]["host"] == "127.0.0.1"
    assert stat.S_IMODE(llm.journal.path(SESSION).stat().st_mode) == 0o600
    assert list((tmp_path / "llm" / "routes").iterdir()) == [llm.journal.path(SESSION)]


async def test_store_copies_the_whole_history_with_models(env):
    llm, client, base, tmp_path, transcript = env
    use(llm.store, "mistral", "k", "Mistral", {"api_key": "sk"}, base_url=f"{base}/v1",
        models={"big": "mistral-large-latest", "small": "mistral-small-latest"})
    await client.post("/v1/messages", json=body(False), headers={"x-claude-code-session-id": SESSION})

    r = await client.post("/_voitta/llm/api/conversations", json={
        "session": SESSION, "model": "claude-opus-5-5", "cwd": "/work/proj", "transcript_path": str(transcript)})
    out = await r.json()
    assert r.status == 200, out
    saved = tmp_path / "conversations" / f"{SESSION}.json"
    assert out["path"] == str(saved) and stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert out["records"] == 6 and out["unreadable_lines"] == 1 and out["subagents"] == 1

    doc = json.loads(saved.read_text())
    assert doc["format"] == "voitta-conversation/1" and doc["title"] == "Testing the store"
    assert [m["message"]["content"] for m in doc["transcript"] if m["type"] == "user"] == \
        ["first question", "after compact"]  # compacted turns included
    assert doc["subagents"]["agent-1"][0]["message"]["model"] == "claude-haiku-4-5"
    assert doc["models"]["requested"] == {"claude-opus-5-5": 2, "claude-haiku-4-5": 1}
    assert doc["models"]["answered_by"] == {"mistral-large-latest @ 127.0.0.1": 1}
    assert doc["claude_code"] == {"version": "2.1.288", "model": "claude-opus-5-5", "cwd": "/work/proj"}
    assert doc["llm"]["account"]["id"] == "mistral:k" and doc["llm"]["route"] == "default"
    assert doc["llm"]["account"]["models"] == {"big": "mistral-large-latest", "small": "mistral-small-latest"}
    assert "credentials" not in json.dumps(doc["llm"])
    assert len(doc["routing"]) == 1

    # storing again replaces the copy with the newer, longer one
    with transcript.open("a") as f:
        f.write("\n" + json.dumps({"type": "user", "message": {"role": "user", "content": "later"}}) + "\n")
    await client.post("/_voitta/llm/api/conversations", json={"session": SESSION})
    assert len(json.loads(saved.read_text())["transcript"]) == 7
    assert list((tmp_path / "conversations").iterdir()) == [saved]


async def test_store_refuses_what_is_not_a_claude_transcript(env):
    llm, client, base, tmp_path, transcript = env
    r = await client.post("/_voitta/llm/api/conversations", json={"session": "../../etc/passwd"})
    assert r.status == 400
    # a hint outside the Claude projects folder is ignored; the real one is found by id
    r = await client.post("/_voitta/llm/api/conversations", json={
        "session": SESSION, "transcript_path": str(tmp_path / "elsewhere.jsonl")})
    assert r.status == 200 and (await r.json())["records"] == 6
    r = await client.post("/_voitta/llm/api/conversations", json={"session": "0000-unknown-session"})
    assert r.status == 404
    r = await client.post("/_voitta/llm/api/conversations", json={"session": SESSION},
                          headers={"Origin": "https://evil.example"})
    assert r.status == 403
