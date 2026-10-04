import pytest
import json

from llmgw.common import OAI_SIGNATURE_PREFIX, Unsupported
from llmgw.sse import MessageBuilder
from llmgw.translate_chat import ChatStreamTranslator, _alnum9, to_chat_request
from llmgw.translate_responses import ResponsesStreamTranslator, to_responses_request

MISTRAL = {"provider": "mistral", "options": {}}
OPENAI_API = {"provider": "openai_api", "options": {}}
CODEX = {"provider": "openai", "options": {}}

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}

CONVERSATION = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 1000,
    "system": [{"type": "text", "text": "You are Claude Code."}, {"type": "text", "text": "Be brief."}],
    "tools": [
        {"name": "Bash", "description": "run", "input_schema": {"$schema": "x", "type": "object",
                                                                "properties": {"command": {"type": "string"}}}},
    ],
    "messages": [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "anthropic-sig"},
            {"type": "text", "text": "Sure."},
            {"type": "tool_use", "id": "toolu_01ABC", "name": "Bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_01ABC", "content": [
                {"type": "text", "text": "a.txt"}, IMG]},
            {"type": "text", "text": "and now?"},
        ]},
    ],
}


def test_chat_request_shape():
    req = to_chat_request(CONVERSATION, OPENAI_API, "gpt-x")
    assert req["model"] == "gpt-x" and req["stream"] is True
    assert req["max_completion_tokens"] == 1000 and "max_tokens" not in req
    assert req["stream_options"] == {"include_usage": True}
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert req["messages"][0]["content"] == "You are Claude Code.\n\nBe brief."
    asst = req["messages"][2]
    assert asst["content"] == "Sure."
    assert asst["tool_calls"][0]["id"] == "toolu_01ABC"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}
    assert req["messages"][3] == {"role": "tool", "tool_call_id": "toolu_01ABC", "content": "a.txt"}
    # the tool result's image rides along in the following user message
    last = req["messages"][4]["content"]
    assert last[0] == {"type": "text", "text": "and now?"}
    assert last[1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    # server tools dropped, $schema stripped
    assert [t["function"]["name"] for t in req["tools"]] == ["Bash"]
    assert "$schema" not in req["tools"][0]["function"]["parameters"]


def test_mistral_tool_ids_and_fields():
    req = to_chat_request(CONVERSATION, MISTRAL, "mistral-large-latest")
    assert "stream_options" not in req and req["max_tokens"] == 1000
    call_id = req["messages"][2]["tool_calls"][0]["id"]
    assert len(call_id) == 9 and call_id.isalnum()
    assert req["messages"][3]["tool_call_id"] == call_id
    assert _alnum9("AbC123xYz") == "AbC123xYz"


def collect(translator, upstream_events):
    events = translator.start()
    for e in upstream_events:
        events += translator.feed(e)
    events += translator.finish()
    b = MessageBuilder()
    for e in events:
        b.feed(e)
    return events, b.result()


def test_chat_stream_to_anthropic():
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
        {"choices": [{"index": 0, "delta": {"content": "lo"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "Bash", "arguments": "{\"comm"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "and\": \"ls\"}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                  "prompt_tokens_details": {"cached_tokens": 4}}},
    ]
    events, msg = collect(ChatStreamTranslator("claude-sonnet-4-5"), chunks)
    assert [e["type"] for e in events][:2] == ["message_start", "content_block_start"]
    assert events[-1]["type"] == "message_stop"
    assert msg["content"][0] == {"type": "text", "text": "Hello"}
    assert msg["content"][1] == {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}}
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 6, "output_tokens": 5, "cache_read_input_tokens": 4}
    assert msg["model"] == "claude-sonnet-4-5"


def test_responses_request_shape():
    convo = json.loads(json.dumps(CONVERSATION))
    convo["metadata"] = {"user_id": json.dumps({"session_id": "sess-1"})}
    convo["output_config"] = {"effort": "high"}
    # an earlier OpenAI turn's reasoning, carried in a thinking block
    convo["messages"][1]["content"].insert(0, {"type": "thinking", "thinking": "prior",
                                               "signature": OAI_SIGNATURE_PREFIX + "ENC"})
    req = to_responses_request(convo, CODEX, "gpt-5.5")
    assert req["instructions"] == "You are Claude Code.\n\nBe brief."
    assert req["store"] is False and req["stream"] is True
    assert req["reasoning"]["effort"] == "high"
    assert req["prompt_cache_key"] == "sess-1"
    assert "max_output_tokens" not in req and "temperature" not in req
    types = [i["type"] for i in req["input"]]
    assert types == ["message", "reasoning", "message", "function_call", "function_call_output", "message"]
    assert req["input"][1]["encrypted_content"] == "ENC"  # the Anthropic-signed thinking was dropped
    assert req["input"][3] == {"type": "function_call", "call_id": "toolu_01ABC", "name": "Bash",
                               "arguments": json.dumps({"command": "ls"})}
    assert req["input"][4]["output"] == "a.txt"
    assert req["input"][5]["content"][1]["type"] == "input_image"
    assert req["tools"] == [{"type": "function", "strict": False, "name": "Bash", "description": "run",
                             "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}]


def test_responses_stream_to_anthropic():
    evs = [
        {"type": "response.created", "response": {}},
        {"type": "response.output_item.added", "item": {"id": "rs_1", "type": "reasoning"}},
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "A"},
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1"},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "delta": "B"},
        {"type": "response.output_item.done", "item": {"id": "rs_1", "type": "reasoning", "encrypted_content": "E"}},
        {"type": "response.output_item.added", "item": {"id": "m1", "type": "message"}},
        {"type": "response.output_text.delta", "item_id": "m1", "delta": "Hi"},
        {"type": "response.output_item.done", "item": {"id": "m1", "type": "message"}},
        {"type": "response.output_item.added", "item": {"id": "fc1", "type": "function_call", "call_id": "c1", "name": "Bash"}},
        # no argument deltas: arguments only arrive on .done
        {"type": "response.output_item.done", "item": {"id": "fc1", "type": "function_call", "call_id": "c1",
                                                       "name": "Bash", "arguments": "{\"command\":\"ls\"}"}},
        {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 50, "output_tokens": 5}}},
    ]
    _, msg = collect(ResponsesStreamTranslator("claude-opus-4-1"), evs)
    assert msg["content"][0] == {"type": "thinking", "thinking": "A\n\nB", "signature": OAI_SIGNATURE_PREFIX + "E"}
    assert msg["content"][1] == {"type": "text", "text": "Hi"}
    assert msg["content"][2]["input"] == {"command": "ls"}
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"]["input_tokens"] == 50


def test_responses_failure_becomes_error_event():
    t = ResponsesStreamTranslator("m")
    t.start()
    t.feed({"type": "response.failed", "response": {"error": {"message": "usage limit reached"}}})
    assert t.finish()[-1] == {"type": "error", "error": {"type": "api_error", "message": "usage limit reached"}}


def test_resolve_effort():
    from llmgw.models import resolve_effort
    acct = {"provider": "openai", "label": "me", "options": {}, "catalog": {"models": [
        {"id": "a", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
        {"id": "nothink", "efforts": [], "default_effort": None}]}}
    assert resolve_effort(acct, "a", "high") == "high"
    for unsupported in ("max", "minimal"):                      # never rounded to a nearby level
        with pytest.raises(Unsupported, match="supported: low, medium, high"):
            resolve_effort(acct, "a", unsupported)
    assert resolve_effort(acct, "a", None) is None              # nothing asked: send nothing
    assert resolve_effort(acct, "nothink", "high") is None      # model has no knob
    assert resolve_effort(acct, "unlisted", "xhigh") == "xhigh"  # unknown model: provider decides
    acct["options"]["reasoning_effort"] = "low"
    assert resolve_effort(acct, "a", "high") == "low"           # fixed setting wins
    with pytest.raises(Unsupported, match="Follow Claude Code"):
        resolve_effort(acct, "nothink", "high")                 # fixed level on a model without one


def test_no_silent_substitutions():
    server_tool = dict(CONVERSATION, tools=[{"type": "web_search_20250305", "name": "web_search"}])
    for translate in (lambda b: to_chat_request(b, MISTRAL, "m"), lambda b: to_responses_request(b, CODEX, "m")):
        with pytest.raises(Unsupported, match="web_search"):
            translate(server_tool)
    pdf = dict(CONVERSATION, messages=[{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "x"}}]}])
    with pytest.raises(Unsupported, match="document"):
        to_chat_request(pdf, MISTRAL, "m")
    from llmgw.providers import map_model
    with pytest.raises(Unsupported, match="no Background model"):
        map_model({"label": "x", "models": {"big": "gpt-5.5", "small": ""}}, "claude-haiku-4-5")
    empty = dict(CONVERSATION, system=None, messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": ""}]}])
    assert to_responses_request(empty, CODEX, "m")["instructions"] == ""
    assert to_responses_request(empty, CODEX, "m")["input"][0]["output"] == ""


def test_portable_pattern_respells_nul_only():
    from llmgw.common import portable_pattern, portable_tools
    assert portable_pattern(r"^[^\0]*$") == r"^[^\x00]*$"
    assert portable_pattern(r"[\0-\x1f]") == r"[\x00-\x1f]"
    for unchanged in (r"a\\0b", r"\01", r"^[0-9a-f]{32}$", r"^(0|[1-9]\d{0,3})$"):
        assert portable_pattern(unchanged) == unchanged
    body = {"tools": [{"name": "Artifact", "input_schema": {"type": "object", "properties": {
        "pattern": {"type": "string"},  # a property *named* pattern is not a regex
        "paths": {"type": "array", "items": {"type": "string", "pattern": r"^[^\0]*$"}}}}}]}
    notes = []
    portable_tools(body, notes)
    props = body["tools"][0]["input_schema"]["properties"]
    assert props["paths"]["items"]["pattern"] == r"^[^\x00]*$"
    assert props["pattern"] == {"type": "string"}
    assert notes == [r"Artifact: regex ^[^\0]*$ sent as ^[^\x00]*$"]
