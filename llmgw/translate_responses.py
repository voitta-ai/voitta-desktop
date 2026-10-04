"""Anthropic Messages <-> OpenAI Responses API, as served by the ChatGPT Codex backend.

The Codex backend only streams, requires ``store: false`` and ``instructions``,
and rejects sampling knobs (max_output_tokens, temperature, top_p), so those
are not forwarded.
"""

import json

from . import models
from .common import (OAI_SIGNATURE_PREFIX, AnthropicStream, client_tools, image_url,
                     system_text, tool_result_parts)

def _user_message(parts: list[dict]) -> dict:
    return {"type": "message", "role": "user", "content": parts}


def to_responses_request(body: dict, account: dict, model: str) -> dict:
    items = []
    for msg in body.get("messages", []):
        content = msg["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        if msg["role"] == "assistant":
            for b in content:
                if b["type"] == "text" and b["text"]:
                    items.append({"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": b["text"]}]})
                elif b["type"] == "tool_use":
                    items.append({"type": "function_call", "call_id": b["id"], "name": b["name"],
                                  "arguments": json.dumps(b.get("input", {}))})
                elif b["type"] == "thinking" and b.get("signature", "").startswith(OAI_SIGNATURE_PREFIX):
                    # Our own earlier reasoning: hand the encrypted state back.
                    items.append({"type": "reasoning",
                                  "summary": [{"type": "summary_text", "text": b["thinking"]}] if b["thinking"] else [],
                                  "encrypted_content": b["signature"][len(OAI_SIGNATURE_PREFIX):]})
                # Anthropic-signed thinking and redacted_thinking cannot be replayed here.
            continue

        parts, late_images = [], []
        for b in content:
            if b["type"] == "tool_result":
                text, images = tool_result_parts(b)
                items.append({"type": "function_call_output", "call_id": b["tool_use_id"],
                              "output": text})
                late_images += images
            elif b["type"] == "text":
                parts.append({"type": "input_text", "text": b["text"]})
            elif b["type"] == "image" and (url := image_url(b)):
                parts.append({"type": "input_image", "image_url": url})
            elif b["type"] == "document" and b.get("source", {}).get("type") == "base64":
                src = b["source"]
                parts.append({"type": "input_file", "filename": b.get("title") or "document.pdf",
                              "file_data": f"data:{src['media_type']};base64,{src['data']}"})
        parts += [{"type": "input_image", "image_url": u} for u in late_images]
        if parts:
            items.append(_user_message(parts))

    req = {
        "model": model,
        "instructions": system_text(body),
        "input": items,
        "tools": [{"type": "function", "strict": False, **t} for t in client_tools(body)],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "stream": True,
    }
    requested = (body.get("output_config") or {}).get("effort")
    if effort := models.resolve_effort(account, model, requested):
        req["reasoning"] = {"effort": effort, "summary": "auto"}
        req["include"] = ["reasoning.encrypted_content"]
    choice = body.get("tool_choice") or {}
    if choice.get("type") == "any":
        req["tool_choice"] = "required"
    elif choice.get("type") == "tool":
        req["tool_choice"] = {"type": "function", "name": choice["name"]}
    elif choice.get("type") == "none":
        req["tool_choice"] = "none"
    if choice.get("disable_parallel_tool_use"):
        req["parallel_tool_calls"] = False
    if session := _session_id(body):
        req["prompt_cache_key"] = session
    return req


def _session_id(body: dict) -> str | None:
    """Claude Code's metadata.user_id carries its session id; reusing it as the
    prompt cache key keeps one conversation on one cache."""
    user_id = (body.get("metadata") or {}).get("user_id")
    if not user_id:
        return None
    try:
        return json.loads(user_id).get("session_id") or user_id[-64:]
    except (ValueError, AttributeError):
        return user_id[-64:]


class ResponsesStreamTranslator:
    """Responses stream events -> Anthropic stream events."""

    def __init__(self, model: str):
        self.out = AnthropicStream(model)
        self.stop_reason = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.error = None
        self._args_streamed: set[str] = set()
        self._summary_parts: dict[str, int] = {}

    def start(self) -> list[dict]:
        return self.out.start()

    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type", "")
        if t == "response.output_item.added":
            item = ev["item"]
            if item["type"] == "function_call":
                return self.out.tool_start(item["call_id"], item["name"], key=item.get("id"))
            if item["type"] == "reasoning":
                return self.out.thinking_start(key=item.get("id"))
        elif t == "response.output_text.delta":
            return self.out.text(ev["delta"], key=ev.get("item_id"))
        elif t == "response.function_call_arguments.delta":
            self._args_streamed.add(ev.get("item_id"))
            return self.out.tool_args(ev["delta"])
        elif t == "response.reasoning_summary_part.added":
            item_id = ev.get("item_id")
            seen = self._summary_parts.get(item_id, 0)
            self._summary_parts[item_id] = seen + 1
            if seen and self.out.is_open("thinking", item_id):
                return self.out.thinking("\n\n")
        elif t == "response.reasoning_summary_text.delta":
            if self.out.is_open("thinking", ev.get("item_id")):
                return self.out.thinking(ev["delta"])
        elif t == "response.output_item.done":
            return self._item_done(ev["item"])
        elif t in ("response.completed", "response.incomplete"):
            self._final(ev.get("response") or {})
        elif t in ("response.failed", "error"):
            err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
            self.error = err.get("message") if isinstance(err, dict) else str(err)
        return []

    def _item_done(self, item: dict) -> list[dict]:
        key = item.get("id")
        events = []
        if item["type"] == "function_call" and self.out.is_open("tool_use", key):
            if key not in self._args_streamed and item.get("arguments"):
                events += self.out.tool_args(item["arguments"])
            events += self.out.close()
        elif item["type"] == "reasoning" and self.out.is_open("thinking", key):
            if item.get("encrypted_content"):
                events += self.out.signature(OAI_SIGNATURE_PREFIX + item["encrypted_content"])
            events += self.out.close()
        elif item["type"] == "message" and self.out.is_open("text", key):
            events += self.out.close()
        return events

    def _final(self, response: dict):
        usage = response.get("usage") or {}
        cached = (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
        self.usage = {"input_tokens": usage.get("input_tokens", 0) - cached,
                      "output_tokens": usage.get("output_tokens", 0)}
        if cached:
            self.usage["cache_read_input_tokens"] = cached
        reason = (response.get("incomplete_details") or {}).get("reason")
        if response.get("status") == "incomplete" and reason == "max_output_tokens":
            self.stop_reason = "max_tokens"

    def finish(self) -> list[dict]:
        if self.error:
            return self.out.close() + [{"type": "error", "error": {"type": "api_error", "message": self.error}}]
        return self.out.finish(self.stop_reason, self.usage)
