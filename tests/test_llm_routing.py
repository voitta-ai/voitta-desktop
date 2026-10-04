"""LLM accounts end to end: Voitta's real LLM proxy, with its router, against a fake upstream."""

import asyncio
import json
import socket
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from llmgw import oauth
from llmgw.store import AS_IS
from llmgw.web import LlmAccounts
from proxy import AnthropicProxy
from tests.llm_fake_upstream import BASH_INPUT, FakeUpstream

TOOLS = [{"name": "Bash", "description": "run", "input_schema": {"type": "object", "properties": {}}}]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """Requests against the running proxy, relative to its address."""

    def __init__(self, port: int):
        self.host, self.port = "127.0.0.1", port
        self._session = aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}")

    def __getattr__(self, method):  # get, post, put, patch, delete
        return getattr(self._session, method)

    async def close(self):
        await self._session.close()


@pytest.fixture
async def env(tmp_path):
    """The proxy with no middleware, its "As is" upstream being the fake too."""
    fake = FakeUpstream()
    upstream = TestServer(fake.app())
    await upstream.start_server()
    base = str(upstream.make_url("")).rstrip("/")
    port = _free_port()
    llm = LlmAccounts(port, data_dir=tmp_path / "llm")
    proxy = AnthropicProxy(middlewares=[], port=port, upstream_url=base, llm=llm)
    await proxy.start()
    client = Client(port)
    yield fake, llm.store, client, base
    await client.close()
    await proxy.stop()
    await upstream.close()


def use(store, *args, **kw):
    """Add an account and make it the global default."""
    account, replaced = store.upsert(*args, **kw)
    store.set_active(account["id"])
    return account, replaced


def body(stream, **kw):
    return {"model": "claude-sonnet-4-5", "max_tokens": 100, "stream": stream, "tools": TOOLS,
            "messages": [{"role": "user", "content": "run it"}], **kw}


def parse_sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_mistral_tool_loop_streaming(env):
    fake, store, client, base = env
    use(store, "mistral", "k", "mistral", {"api_key": "sk-mistral"}, base_url=f"{base}/v1",
                 models={"big": "mistral-large-latest", "small": "mistral-small-latest"})

    r = await client.post("/v1/messages", json=body(True), headers={"authorization": "Bearer claude-code-token"})
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
    events = parse_sse(await r.text())
    assert events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop"
    tool = next(e for e in events if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use")
    args = "".join(e["delta"]["partial_json"] for e in events
                   if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta")
    assert json.loads(args) == BASH_INPUT
    assert next(e for e in events if e["type"] == "message_delta")["delta"]["stop_reason"] == "tool_use"

    sent = fake.requests[-1]
    assert sent["headers"]["Authorization"] == "Bearer sk-mistral"  # client token replaced
    assert sent["body"]["model"] == "mistral-large-latest"

    # second turn with the tool result, non-streaming
    tool_id = tool["content_block"]["id"]
    follow = body(False, messages=[
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": BASH_INPUT}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "voitta-e2e-ok"}]},
    ])
    r = await client.post("/v1/messages", json=follow)
    msg = await r.json()
    assert msg["content"][0]["text"].strip() == "Final answer: voitta-e2e-ok"
    assert msg["usage"] == {"input_tokens": 42, "output_tokens": 7}


async def test_codex_subscription_with_refresh(env, monkeypatch):
    fake, store, client, base = env
    account, _ = use(store, "openai", "u:acc", "me@x.com",
                              {"access_token": "expired", "refresh_token": "r1", "account_id": "acc-1",
                               "expires_at": time.time() - 10}, base_url=f"{base}/codex",
                              models={"big": "gpt-5.5", "small": "gpt-5.5"})

    async def fake_refresh(http, acct):
        return {"access_token": "fresh", "refresh_token": "r2", "account_id": "acc-1",
                "expires_at": time.time() + 3600}
    monkeypatch.setattr(oauth, "refresh", fake_refresh)

    r = await client.post("/v1/messages", json=body(False, model="claude-haiku-4-5"))
    msg = await r.json()
    assert r.status == 200, msg
    assert [b["type"] for b in msg["content"]] == ["thinking", "tool_use"]
    assert msg["content"][1]["input"] == BASH_INPUT
    assert msg["usage"] == {"input_tokens": 40, "output_tokens": 9, "cache_read_input_tokens": 60}

    sent = fake.requests[-1]
    assert sent["headers"]["Authorization"] == "Bearer fresh"
    assert sent["headers"]["chatgpt-account-id"] == "acc-1"
    assert sent["body"]["model"] == "gpt-5.5"
    assert store.get(account["id"])["credentials"]["refresh_token"] == "r2"

    # the thinking block goes back as a reasoning item on the next turn
    follow = body(True, messages=[
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": msg["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": msg["content"][1]["id"],
                                      "content": "voitta-e2e-ok"}]},
    ])
    r = await client.post("/v1/messages", json=follow)
    text = "".join(e["delta"]["text"] for e in parse_sse(await r.text())
                   if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
    assert text.strip() == "Final answer: voitta-e2e-ok"
    assert fake.requests[-1]["body"]["input"][1] == {"type": "reasoning", "encrypted_content": "ENC123",
                                                     "summary": [{"type": "summary_text", "text": "Thinking it over."}]}


async def test_claude_passthrough_swaps_auth_and_strips_foreign_thinking(env):
    fake, store, client, base = env
    use(store, "claude", "u:o", "me@x.com",
                 {"access_token": "sub-token", "refresh_token": "r", "expires_at": time.time() + 3600})
    store.update_settings("claude:u:o", base_url=base)

    req = body(False, messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "x", "signature": "voitta-oai:ENC"},
            {"type": "text", "text": "hello"}]},
        {"role": "user", "content": "again"},
    ])
    r = await client.post("/v1/messages?beta=true", json=req, headers={
        "authorization": "Bearer claude-code-token", "anthropic-beta": "interleaved-thinking-2025-05-14",
        "anthropic-version": "2023-06-01"})
    assert r.status == 200
    assert r.headers["anthropic-ratelimit-test"] == "1"
    sent = fake.requests[-1]
    assert sent["headers"]["Authorization"] == "Bearer sub-token"
    assert sent["headers"]["anthropic-beta"] == \
        "interleaved-thinking-2025-05-14,oauth-2025-04-20,claude-code-20250219"
    assert sent["body"]["messages"][1]["content"] == [{"type": "text", "text": "hello"}]
    assert sent["body"]["model"] == "claude-sonnet-4-5"


async def test_deepseek_compat_maps_model_and_key(env):
    fake, store, client, base = env
    use(store, "deepseek", "k", "ds", {"api_key": "sk-ds"}, base_url=f"{base}/anthropic",
                 models={"big": "deepseek-chat", "small": "deepseek-chat"})
    r = await client.post("/v1/messages", json=body(True), headers={"anthropic-beta": "some-beta"})
    assert r.status == 200
    assert parse_sse(await r.text())[-1]["type"] == "message_stop"
    sent = fake.requests[-1]
    assert sent["path"] == "/anthropic/v1/messages"
    assert sent["headers"]["x-api-key"] == "sk-ds"
    assert "anthropic-beta" not in {k.lower() for k in sent["headers"]}
    assert sent["body"]["model"] == "deepseek-chat"
    # the log records who answered, as the upstream reports it
    state = await (await client.get("/_voitta/llm/api/state")).json()
    up = state["requests"][0]["upstream"]
    assert up["id"] == "msg_fake" and up["model"] == "deepseek-chat" and up["host"] == "127.0.0.1"


async def test_upstream_errors_surface_as_anthropic_errors(env):
    fake, store, client, base = env
    use(store, "mistral", "k", "mistral", {"api_key": "sk"}, base_url=f"{base}/nope", models={"big": "m", "small": "m"})
    r = await client.post("/v1/messages", json=body(False))
    assert r.status == 404
    err = await r.json()
    assert err["type"] == "error" and err["error"]["type"] == "not_found_error"


async def test_as_is_forwards_claude_codes_own_login(env):
    fake, store, client, base = env
    assert store.active_id == AS_IS
    r = await client.post("/v1/messages?beta=true", json=body(False), headers={
        "authorization": "Bearer claude-code-own", "anthropic-beta": "some-beta-2025"})
    assert r.status == 200
    sent = fake.requests[-1]
    assert sent["headers"]["Authorization"] == "Bearer claude-code-own"
    assert sent["headers"]["anthropic-beta"] == "some-beta-2025"
    assert sent["body"]["model"] == "claude-sonnet-4-5"
    # an expired login comes back as 401, so Claude Code renews it itself
    r = await client.post("/v1/messages", json=body(False), headers={"authorization": "Bearer expired"})
    assert r.status == 401


async def test_window_picks_route_by_session_header(env):
    fake, store, client, base = env
    mistral, _ = use(store, "mistral", "k", "mistral", {"api_key": "sk-m"}, base_url=f"{base}/v1",
                     models={"big": "mistral-large-latest", "small": "mistral-small-latest"})
    ds, _ = store.upsert("deepseek", "k", "ds", {"api_key": "sk-ds"}, base_url=f"{base}/anthropic",
                         models={"big": "deepseek-v4-pro", "small": "deepseek-flash"})

    r = await client.get("/_voitta/llm/api/llm/options?session=win-A")
    view = await r.json()
    assert view["pick"] is None and view["default"]["id"] == mistral["id"]
    assert [o["id"] for o in view["options"]] == [AS_IS, mistral["id"], ds["id"]]
    assert view["options"][2]["main"] == "deepseek-v4-pro"

    r = await client.put("/_voitta/llm/api/llm/sessions/win-A", json={"account": ds["id"]})
    assert (await r.json())["pick"] == ds["id"]
    r = await client.put("/_voitta/llm/api/llm/sessions/win-A", json={"account": "nope:1"})
    assert r.status == 400

    async def send(session):
        headers = {"x-claude-code-session-id": session} if session else {}
        r = await client.post("/v1/messages", json=body(False), headers=headers)
        assert r.status == 200, await r.text()
        return fake.requests[-1]["path"]

    assert await send("win-A") == "/anthropic/v1/messages"   # the window's pick
    assert await send("win-B") == "/v1/chat/completions"     # no pick: the default
    assert await send(None) == "/v1/chat/completions"
    state = await (await client.get("/_voitta/llm/api/state")).json()
    win = next(w for w in state["windows"] if w["session"] == "win-A")
    assert win["pick"] == ds["id"] and win["route"] == "window" and win["requests"] == 1

    # picking "default" clears the window's pick
    await client.put("/_voitta/llm/api/llm/sessions/win-A", json={"account": "default"})
    assert await send("win-A") == "/v1/chat/completions"

    # a picked account that disappears is an error for that window, never a fallback
    await client.put("/_voitta/llm/api/llm/sessions/win-A", json={"account": ds["id"]})
    await client.delete(f"/_voitta/llm/api/accounts/{ds['id']}")
    r = await client.post("/v1/messages", json=body(False), headers={"x-claude-code-session-id": "win-A"})
    assert r.status == 409 and "/llm" in (await r.json())["error"]["message"]
    assert r.headers["x-should-retry"] == "false"


async def test_cross_origin_refused(env):
    fake, store, client, base = env
    use(store, "mistral", "k", "mistral", {"api_key": "sk"}, base_url=f"{base}/v1", models={"big": "m", "small": "m"})
    r = await client.post("/v1/messages", json=body(False), headers={"Origin": "https://evil.example"})
    assert r.status == 403 and "cross-origin" in (await r.json())["error"]["message"]
    assert not fake.requests  # the account was never used
    r = await client.get("/_voitta/llm/api/state", headers={"Origin": "https://evil.example"})
    assert r.status == 403
    r = await client.get("/_voitta/llm/api/state", headers={"Origin": f"http://localhost:{client.port}"})
    assert r.status == 200


async def test_count_tokens_estimate_for_translated(env):
    fake, store, client, base = env
    use(store, "mistral", "k", "mistral", {"api_key": "sk"}, base_url=f"{base}/v1", models={"big": "m", "small": "m"})
    r = await client.post("/v1/messages/count_tokens", json=body(False))
    assert (await r.json())["input_tokens"] > 0


async def test_claude_invite_link_and_code(env, monkeypatch, tmp_path):
    _, store, client, _ = env
    if True:
        r = await client.post("/_voitta/llm/api/invites", json={"note": "Alice"})
        url = (await r.json())["url"]
        assert "redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback" in url
        state = url.split("state=")[1]

        # survives a restart
        assert oauth.PendingLogins(tmp_path / "llm" / "invites.json").invites("claude")[0]["note"] == "Alice"

        calls = []

        async def fake_exchange(http, code, login, st):
            calls.append((code, st))
            if code == "bad":
                raise oauth.OAuthError("invalid code.")
            return {"identity": "alice-uuid:org", "label": "alice@x.com", "info": {},
                    "credentials": {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600}}
        monkeypatch.setattr(oauth, "claude_exchange", fake_exchange)

        r = await client.post("/_voitta/llm/api/login/claude/manual", json={"code": f"bad#{state}"})
        assert r.status == 400 and "still valid" in (await r.json())["error"]
        assert len((await (await client.get("/_voitta/llm/api/state")).json())["invites"]) == 1

        # bare code, no '#state' — matched to the only open invite; whitespace from chat apps ignored
        r = await client.post("/_voitta/llm/api/login/claude/manual", json={"code": " good-co\nde "})
        data = await r.json()
        assert r.status == 200, data
        assert calls[-1] == ("good-code", state)
        assert data["account"]["info"]["invited_as"] == "Alice"
        st = await (await client.get("/_voitta/llm/api/state")).json()
        assert st["invites"] == [] and [a["label"] for a in st["accounts"]] == ["alice@x.com"]


async def test_catalogs_and_effort(env):
    fake, store, client, base = env
    codex, _ = use(store, "openai", "u:acc", "me", {"access_token": "t", "refresh_token": "r", "account_id": "a",
                                                    "expires_at": time.time() + 3600}, base_url=f"{base}/codex")
    use(store, "mistral", "k", "mi", {"api_key": "sk"}, base_url=f"{base}/v1")
    use(store, "deepseek", "k", "ds", {"api_key": "sk"}, base_url=f"{base}/anthropic")
    for acct in ("openai:u:acc", "mistral:k", "deepseek:k"):
        r = await client.post(f"/_voitta/llm/api/accounts/{acct}/models")
        assert r.status == 200, await r.text()
    cat = {a["id"]: [m["id"] for m in a["catalog"]["models"]] for a in store.list()}
    assert cat["openai:u:acc"] == ["gpt-6", "gpt-6-luna", "gpt-5.5"]  # hidden dropped, priority order
    # no hardcoded defaults: empty tiers were filled from the provider's own list
    assert store.get("openai:u:acc")["models"] == {"big": "gpt-6", "small": "gpt-6-luna"}
    assert all(m for a in store.list() for m in a["models"].values())
    # a choice the user made is never replaced by a later listing
    await client.patch(f"/_voitta/llm/api/accounts/{codex['id']}", json={"models": {"big": "gpt-5.5", "small": "gpt-5.5"}})
    await client.post(f"/_voitta/llm/api/accounts/{codex['id']}/models")
    assert store.get("openai:u:acc")["models"] == {"big": "gpt-5.5", "small": "gpt-5.5"}
    assert cat["mistral:k"] == ["deepseek-chat", "mistral-large-latest"]  # embeddings dropped
    assert "deepseek-chat" in cat["deepseek:k"]                  # DeepSeek's root /models listing

    # Claude Code's /effort goes through as-is; one the model lacks is an error, not rounded
    store.set_active(codex["id"])
    sent_before = len(fake.requests)
    r = await client.post("/v1/messages", json=body(False, model="claude-opus-4-1", output_config={"effort": "max"}))
    assert r.status == 400 and r.headers["x-should-retry"] == "false"
    assert "supported: low, medium, high, xhigh" in (await r.json())["error"]["message"]
    assert len(fake.requests) == sent_before                     # nothing was sent upstream
    r = await client.post("/v1/messages", json=body(False, model="claude-opus-4-1", output_config={"effort": "xhigh"}))
    assert r.status == 200
    assert fake.requests[-1]["body"]["reasoning"]["effort"] == "xhigh"

    # a fixed per-account effort wins over Claude Code's
    r = await client.patch(f"/_voitta/llm/api/accounts/{codex['id']}", json={"options": {"reasoning_effort": "low"}})
    assert (await r.json())["account"]["options"] == {"reasoning_effort": "low"}
    await client.post("/v1/messages", json=body(False, output_config={"effort": "high"}))
    assert fake.requests[-1]["body"]["reasoning"]["effort"] == "low"


async def test_compat_strips_anthropic_only_fields(env):
    fake, store, client, base = env
    use(store, "deepseek", "k", "ds", {"api_key": "sk"}, base_url=f"{base}/anthropic",
                 models={"big": "deepseek-chat", "small": "deepseek-chat"})
    req = body(False, thinking={"type": "adaptive"}, output_config={"effort": "high"},
               context_management={"edits": []}, safeguards=[{"type": "x", "classifier_context": {"home_dir": "/Users/me"}}],
               tools=TOOLS)
    r = await client.post("/v1/messages?beta=true", json=req)
    assert r.status == 200
    sent = fake.requests[-1]
    assert sent["path"] == "/anthropic/v1/messages"
    assert set(sent["body"]) == {"model", "max_tokens", "stream", "tools", "messages"}
    assert [t["name"] for t in sent["body"]["tools"]] == ["Bash"]


async def test_long_quota_429_stops_retries_and_marks_account(env):
    fake, store, client, base = env
    use(store, "openai", "u:acc", "me", {"access_token": "t", "refresh_token": "r", "account_id": "a",
                                          "expires_at": time.time() + 3600}, base_url=f"{base}/codex",
                 models={"big": "gpt-5.5", "small": "gpt-5.5"})
    fake.fail_next = (429, {"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached",
                                      "resets_in_seconds": 2592000}})
    r = await client.post("/v1/messages", json=body(False))
    assert r.status == 429 and r.headers["x-should-retry"] == "false"
    assert (await r.json())["error"]["type"] == "rate_limit_error"
    acct = store.get("openai:u:acc")
    assert acct["status"] == "limited" and "resets" in acct["status_detail"]
    r = await client.post("/v1/messages", json=body(False))       # works again: status clears
    assert r.status == 200 and store.get("openai:u:acc")["status"] == "ok"



async def test_chatgpt_device_code_invite(env, monkeypatch):
    fake, store, client, base = env
    monkeypatch.setitem(oauth.OPENAI, "device_usercode_url", f"{base}/api/accounts/deviceauth/usercode")
    monkeypatch.setitem(oauth.OPENAI, "device_token_url", f"{base}/api/accounts/deviceauth/token")
    monkeypatch.setitem(oauth.OPENAI, "token_url", f"{base}/oauth/token")

    r = await client.post("/_voitta/llm/api/device-invites", json={"note": "Friend"})
    invite = (await r.json())["invite"]
    assert invite["user_code"] == "TEST-CODE1" and invite["url"] == "https://auth.openai.com/codex/device"
    assert invite["status"] == "waiting" and "device_auth_id" not in invite

    for _ in range(50):
        await asyncio.sleep(0.1)
        state = await (await client.get("/_voitta/llm/api/state")).json()
        if state["device_invites"][0]["status"] != "waiting":
            break
    d = state["device_invites"][0]
    assert d["status"] == "added", d
    account = store.get("openai:user-friend:acct-friend")
    assert account and account["label"].startswith("friend@example.com")
    assert account["info"]["invited_as"] == "Friend"
    assert account["credentials"]["refresh_token"] == "rt-friend"
    exchange = next(q for q in fake.requests if q["path"] == "/oauth/token")["body"]
    assert exchange["code"] == "authcode-1" and exchange["code_verifier"] == "ver-1"
    assert exchange["redirect_uri"] == "https://auth.openai.com/deviceauth/callback"


async def test_chatgpt_device_code_unexpected_error_fails_visibly(env, monkeypatch):
    fake, store, client, base = env
    monkeypatch.setitem(oauth.OPENAI, "device_usercode_url", f"{base}/api/accounts/deviceauth/usercode")
    monkeypatch.setitem(oauth.OPENAI, "device_token_url", f"{base}/nope")
    r = await client.post("/_voitta/llm/api/device-invites", json={})
    for _ in range(50):
        await asyncio.sleep(0.1)
        d = (await (await client.get("/_voitta/llm/api/state")).json())["device_invites"][0]
        if d["status"] != "waiting":
            break
    assert d["status"] == "failed" and "404" in d["detail"]
    assert not [a for a in store.list() if a["provider"] == "openai"]


async def test_client_hangup_is_499_not_upstream_502(env):
    import asyncio
    fake, store, client, base = env
    a, _ = store.upsert("mistral", "k", "mistral", {"api_key": "sk"}, base_url=f"{base}/v1",
                        models={"big": "slow-model", "small": "slow-model"})
    store.set_active(a["id"])
    payload = json.dumps(body(True, tools=[])).encode()
    reader, writer = await asyncio.open_connection(client.host, client.port)
    writer.write(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                 b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
    await writer.drain()
    await reader.readuntil(b"text_delta")  # the answer is streaming...
    writer.close()                         # ...and Claude Code hangs up
    for _ in range(100):
        entry = (await (await client.get("/_voitta/llm/api/state")).json())["requests"][0]
        if entry["status"] == 499:
            break
        await asyncio.sleep(0.05)
    assert entry["status"] == 499, json.dumps(entry)
    assert "closed the connection" in entry["error"]
    assert entry["upstream"]["host"]  # the upstream did answer; the receipt says so



async def test_compat_respells_regex_and_refuses_server_tools(env):
    fake, store, client, base = env
    a, _ = store.upsert("deepseek", "k", "ds", {"api_key": "sk-ds"}, base_url=f"{base}/anthropic",
                        models={"big": "deepseek-v4-pro", "small": "deepseek-flash"})
    store.set_active(a["id"])
    tool = {"name": "Artifact", "description": "a", "input_schema": {"type": "object", "properties": {
        "p": {"type": "string", "pattern": r"^[^\0]*$"}}}}
    r = await client.post("/v1/messages", json=body(False, tools=[tool]))
    assert r.status == 200
    sent = fake.requests[-1]["body"]["tools"][0]["input_schema"]["properties"]["p"]["pattern"]
    assert sent == r"^[^\x00]*$"
    log = (await (await client.get("/_voitta/llm/api/state")).json())["requests"][0]
    assert log["notes"] == [r"Artifact: regex ^[^\0]*$ sent as ^[^\x00]*$"]

    r = await client.post("/v1/messages", json=body(False, tools=[{"type": "web_search_20250305", "name": "web_search"}]))
    assert r.status == 400 and "server tool" in (await r.json())["error"]["message"]


async def test_claude_passthrough_keeps_regex_verbatim(env):
    fake, store, client, base = env
    a, _ = store.upsert("claude", "u:o", "me", {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 3600})
    store.update_settings(a["id"], base_url=base)
    store.set_active(a["id"])
    tool = {"name": "Artifact", "description": "a", "input_schema": {"type": "object", "properties": {
        "p": {"type": "string", "pattern": r"^[^\0]*$"}}}}
    await client.post("/v1/messages", json=body(False, tools=[tool]))
    assert fake.requests[-1]["body"]["tools"][0]["input_schema"]["properties"]["p"]["pattern"] == r"^[^\0]*$"


class Recorder:
    """A middleware that rewrites the request (as the optimizers do) and
    records every hook call, to check routed requests keep the full chain."""

    def __init__(self):
        self.calls, self.chunks = [], b""

    async def on_request(self, req):
        self.calls.append("request")
        data = json.loads(req.body)
        data["messages"][0]["content"] = "rewritten by middleware"
        req.body = json.dumps(data).encode()
        return req

    async def on_response_started(self, req, resp):
        self.calls.append(f"started {resp.status}")
        return resp

    async def on_response_chunk(self, req, chunk):
        self.chunks += chunk
        return chunk

    async def on_response_done(self, req, resp):
        self.calls.append(f"done {resp.status}")


async def test_routed_requests_keep_the_middleware_chain(tmp_path):
    fake = FakeUpstream()
    upstream = TestServer(fake.app())
    await upstream.start_server()
    base = str(upstream.make_url("")).rstrip("/")
    port, rec = _free_port(), Recorder()
    llm = LlmAccounts(port, data_dir=tmp_path / "llm")
    proxy = AnthropicProxy(middlewares=[rec], port=port, upstream_url=base, llm=llm)
    await proxy.start()
    client = Client(port)
    try:
        use(llm.store, "mistral", "k", "mistral", {"api_key": "sk"}, base_url=f"{base}/v1",
            models={"big": "mistral-large-latest", "small": "mistral-small-latest"})
        r = await client.post("/v1/messages", json=body(True, tools=[]))
        text = await r.text()
        assert r.status == 200 and "message_stop" in text
        # the account got the body the middleware produced...
        sent = fake.requests[-1]["body"]["messages"]
        assert sent[-1] == {"role": "user", "content": "rewritten by middleware"}
        # ...and the middleware saw the translated (Anthropic-shaped) answer
        assert rec.calls == ["request", "started 200", "done 200"]
        assert b"event: message_start" in rec.chunks and b"text_delta" in rec.chunks

        # the page's Test button goes straight to the account, never through the middleware
        rec.calls.clear()
        r = await client.post(f"/_voitta/llm/api/accounts/{llm.store.active_id}/test")
        assert (await r.json())["ok"] is True
        assert rec.calls == []
    finally:
        await client.close()
        await proxy.stop()
        await upstream.close()
