"""A fake upstream that speaks Chat Completions, Codex Responses and Anthropic.

Scripted agent: when the request offers a tool named ``Bash`` and contains no
tool result yet, it asks to run ``echo voitta-e2e-ok``; once a tool result is
present it answers with that result. Anything else gets "ok".
"""

import asyncio
import json

from aiohttp import web

BASH_INPUT = {"command": "echo voitta-e2e-ok", "description": "Print a marker"}
SUBAGENT_INPUT = {"description": "Run the marker", "subagent_type": "general-purpose", "run_in_background": False,
                  "prompt": "Run the marker command and report its output."}


def _sse(event: dict, name: str | None = None) -> bytes:
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {json.dumps(event)}\n\n".encode()


class FakeUpstream:
    def __init__(self):
        self.requests: list[dict] = []  # {"path", "headers", "body"}
        self.fail_next: tuple[int, dict] | None = None  # (status, json body) for the next request

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        app.router.add_post("/codex/responses", self.responses)
        app.router.add_post("/anthropic/v1/messages", self.anthropic)
        app.router.add_post("/v1/messages", self.anthropic)
        app.router.add_post("/api/accounts/deviceauth/usercode", self.device_usercode)
        app.router.add_post("/api/accounts/deviceauth/token", self.device_token)
        app.router.add_post("/oauth/token", self.oauth_token)
        app.router.add_get("/codex/models", self.codex_models)
        app.router.add_get("/v1/models", self.openai_models)
        app.router.add_get("/models", self.openai_models)  # DeepSeek-style root listing
        return app

    async def codex_models(self, request):
        assert request.query["client_version"]
        return web.json_response({"models": [
            {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide", "priority": 1,
             "supported_reasoning_levels": [{"effort": "low"}]},
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 13,
             "description": "Legacy coding model.", "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]},
            {"slug": "gpt-6", "display_name": "GPT-6", "visibility": "list", "priority": 2,
             "default_reasoning_level": "low",
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
            {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list", "priority": 4,
             "description": "Fast and affordable model for easier tasks.", "default_reasoning_level": "medium",
             "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")]},
        ]})

    async def openai_models(self, request):
        return web.json_response({"object": "list", "data": [
            {"id": "mistral-large-latest", "capabilities": {"completion_chat": True, "function_calling": True}},
            {"id": "mistral-embed", "capabilities": {"completion_chat": False, "function_calling": False}},
            {"id": "deepseek-chat"},
        ]})

    async def _record(self, request) -> dict:
        body = await request.json()
        self.requests.append({"path": request.path, "headers": dict(request.headers), "body": body})
        return body

    # ---- Chat Completions ------------------------------------------------

    async def chat(self, request):
        body = await self._record(request)
        msgs = body["messages"]
        tool_names = [t["function"]["name"] for t in body.get("tools", [])]
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        def chunk(delta, finish=None):
            return _sse({"id": "c1", "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        async def call(name, call_id, tool_input):
            await resp.write(chunk({"role": "assistant", "content": "Running it."}))
            await resp.write(chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                    "function": {"name": name, "arguments": ""}}]}))
            args = json.dumps(tool_input)
            for i in range(0, len(args), 10):
                await resp.write(chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 10]}}]}))
            await resp.write(chunk({}, "tool_calls"))

        if body["model"] == "slow-model":  # a long answer, so the client can hang up mid-stream
            for i in range(100):
                await resp.write(chunk({"content": f"word{i} "}))
                await asyncio.sleep(0.02)
            await resp.write(chunk({}, "stop"))
            await resp.write(b"data: [DONE]\n\n")
            return resp

        # "SUBAGENT" in the user's prompt: hand the marker command to a subagent first.
        user_text = " ".join(m["content"] if isinstance(m["content"], str)
                             else " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
                             for m in msgs if m["role"] == "user")
        agent_tool = next((n for n in ("Agent", "Task") if n in tool_names), None)
        if "SUBAGENT" in user_text and agent_tool and not tool_msgs:
            await call(agent_tool, "AgEnT0001", SUBAGENT_INPUT)
        elif "Bash" in tool_names and not tool_msgs:
            await call("Bash", "AbC123xYz", BASH_INPUT)
        else:
            text = f"Final answer: {tool_msgs[-1]['content'].strip()}" if tool_msgs else "ok"
            for word in text.split(" "):
                await resp.write(chunk({"content": word + " "}))
            await resp.write(chunk({}, "stop"))
        await resp.write(_sse({"id": "c1", "choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 7}}))
        await resp.write(b"data: [DONE]\n\n")
        return resp

    # ---- Codex Responses -------------------------------------------------

    async def responses(self, request):
        body = await self._record(request)
        if self.fail_next:
            (status, err), self.fail_next = self.fail_next, None
            return web.json_response(err, status=status)
        tool_names = [t["name"] for t in body.get("tools", [])]
        outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        async def send(ev):
            await resp.write(_sse(ev, ev["type"]))

        await send({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}})
        await send({"type": "response.output_item.added", "output_index": 0,
                    "item": {"id": "rs_1", "type": "reasoning", "summary": []}})
        await send({"type": "response.reasoning_summary_part.added", "item_id": "rs_1", "summary_index": 0})
        await send({"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "Thinking it over."})
        await send({"type": "response.output_item.done", "output_index": 0,
                    "item": {"id": "rs_1", "type": "reasoning", "encrypted_content": "ENC123"}})
        if "Bash" in tool_names and not outputs:
            args = json.dumps(BASH_INPUT)
            await send({"type": "response.output_item.added", "output_index": 1,
                        "item": {"id": "fc_1", "type": "function_call", "call_id": "call_xyz", "name": "Bash", "arguments": ""}})
            for i in range(0, len(args), 10):
                await send({"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": args[i:i + 10]})
            await send({"type": "response.output_item.done", "output_index": 1,
                        "item": {"id": "fc_1", "type": "function_call", "call_id": "call_xyz", "name": "Bash", "arguments": args}})
        else:
            text = f"Final answer: {outputs[-1]['output'].strip()}" if outputs else "ok"
            await send({"type": "response.output_item.added", "output_index": 1,
                        "item": {"id": "msg_1", "type": "message", "role": "assistant", "content": []}})
            for word in text.split(" "):
                await send({"type": "response.output_text.delta", "item_id": "msg_1", "delta": word + " "})
            await send({"type": "response.output_item.done", "output_index": 1,
                        "item": {"id": "msg_1", "type": "message"}})
        await send({"type": "response.completed", "response": {
            "id": "resp_1", "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 9, "input_tokens_details": {"cached_tokens": 60}}}})
        return resp

    # ---- Anthropic ---------------------------------------------------------

    async def anthropic(self, request):
        body = await self._record(request)
        if request.headers.get("Authorization") == "Bearer expired":
            return web.json_response({"type": "error", "error": {"type": "authentication_error",
                                                                 "message": "OAuth token has expired"}}, status=401)
        msg = {"id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"],
               "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
               "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}
        if not body.get("stream"):
            return web.json_response(msg, headers={"anthropic-ratelimit-test": "1"})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        start = dict(msg, content=[], stop_reason=None)
        for ev in ({"type": "message_start", "message": start},
                   {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                   {"type": "content_block_stop", "index": 0},
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                   {"type": "message_stop"}):
            await resp.write(_sse(ev, ev["type"]))
        return resp




# ---- OpenAI device-code login (pending once, then approved) -------------------

def _jwt(claims: dict) -> str:
    import base64
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


async def _device_usercode(self, request):
    body = await request.json()
    self.requests.append({"path": request.path, "headers": dict(request.headers), "body": body})
    self.device_polls = 0
    return web.json_response({"device_auth_id": "deviceauth_abc123def456", "user_code": "TEST-CODE1",
                              "interval": "1", "expires_at": "2099-01-01T00:00:00+00:00"})


async def _device_token(self, request):
    body = await request.json()
    self.requests.append({"path": request.path, "headers": dict(request.headers), "body": body})
    self.device_polls += 1
    if self.device_polls < 2:
        return web.json_response({"error": {"code": "deviceauth_authorization_pending",
                                            "message": "Device authorization is pending."}}, status=403)
    return web.json_response({"authorization_code": "authcode-1", "code_challenge": "ch", "code_verifier": "ver-1"})


async def _oauth_token(self, request):
    form = dict(await request.post())
    self.requests.append({"path": request.path, "headers": dict(request.headers), "body": form})
    auth = {"chatgpt_account_id": "acct-friend", "chatgpt_user_id": "user-friend", "chatgpt_plan_type": "plus"}
    return web.json_response({
        "access_token": _jwt({"exp": 4102444800, "https://api.openai.com/auth": auth}),
        "refresh_token": "rt-friend", "expires_in": 3600,
        "id_token": _jwt({"email": "friend@example.com", "https://api.openai.com/auth": auth}),
    })


FakeUpstream.device_usercode = _device_usercode
FakeUpstream.device_token = _device_token
FakeUpstream.oauth_token = _oauth_token


if __name__ == "__main__":
    import sys
    web.run_app(FakeUpstream().app(), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 18999)
