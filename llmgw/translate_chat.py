"""Anthropic Messages <-> OpenAI Chat Completions (Mistral, OpenAI API, ...)."""

import hashlib
import json
import re
import string

from . import models, providers
from .common import Unsupported, AnthropicStream, client_tools, image_url, system_text, tool_result_parts

_ALNUM = string.ascii_letters + string.digits


def _alnum9(tool_id: str) -> str:
    """Mistral only accepts tool call ids of exactly 9 alphanumerics. Ids it
    issued already qualify; others (e.g. toolu_... from an earlier Claude
    turn) are hashed, deterministically, so a call and its result still match."""
    if re.fullmatch(r"[A-Za-z0-9]{9}", tool_id):
        return tool_id
    digest = hashlib.sha256(tool_id.encode()).digest()
    return "".join(_ALNUM[b % len(_ALNUM)] for b in digest[:9])


def to_chat_request(body: dict, account: dict, model: str) -> dict:
    fix_id = _alnum9 if providers.option(account, "tool_id_style") == "alnum9" else (lambda i: i)
    messages = []
    if system := system_text(body):
        messages.append({"role": "system", "content": system})

    for msg in body.get("messages", []):
        content = msg["content"]
        if isinstance(content, str):
            messages.append({"role": msg["role"], "content": content})
            continue

        if msg["role"] == "assistant":
            text = "".join(b["text"] for b in content if b["type"] == "text")
            calls = [{"id": fix_id(b["id"]), "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                     for b in content if b["type"] == "tool_use"]
            out = {"role": "assistant", "content": text}
            if calls:
                out["tool_calls"] = calls
            if text or calls:
                messages.append(out)
            continue

        # user turn: tool results become role=tool messages, which must come
        # first (right after the assistant's tool_calls); the rest stays a user message.
        parts, late_images = [], []
        for b in content:
            if b["type"] == "tool_result":
                text, images = tool_result_parts(b)
                messages.append({"role": "tool", "tool_call_id": fix_id(b["tool_use_id"]),
                                 "content": text})
                late_images += images
            elif b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image" and (url := image_url(b)):
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif b["type"] == "document":
                raise Unsupported("this provider's Chat Completions API takes no document (PDF) attachments")
        parts += [{"type": "image_url", "image_url": {"url": u}} for u in late_images]
        if parts:
            if all(p["type"] == "text" for p in parts):
                messages.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                messages.append({"role": "user", "content": parts})

    req = {"model": model, "messages": messages, "stream": True}
    req[providers.option(account, "max_tokens_field", "max_tokens")] = body.get("max_tokens", 8192)
    if providers.option(account, "stream_usage_option", True):
        req["stream_options"] = {"include_usage": True}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("stop_sequences", "stop")):
        if body.get(src) is not None:
            req[dst] = body[src]

    if providers.option(account, "reasoning_param"):
        requested = (body.get("output_config") or {}).get("effort")
        if effort := models.resolve_effort(account, model, requested):
            req["reasoning_effort"] = effort

    if tools := client_tools(body):
        req["tools"] = [{"type": "function", "function": t} for t in tools]
        choice = body.get("tool_choice") or {"type": "auto"}
        if choice["type"] == "any":
            req["tool_choice"] = "required"
        elif choice["type"] == "tool":
            req["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif choice["type"] == "none":
            req["tool_choice"] = "none"
        else:
            req["tool_choice"] = "auto"
        if choice.get("disable_parallel_tool_use"):
            req["parallel_tool_calls"] = False
    return req


_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                 "function_call": "tool_use", "content_filter": "refusal"}


class ChatStreamTranslator:
    """Chat Completions stream chunks -> Anthropic stream events."""

    def __init__(self, model: str):
        self.out = AnthropicStream(model)
        self.stop_reason = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self._tool_keys: dict[int, str] = {}

    def start(self) -> list[dict]:
        return self.out.start()

    def feed(self, chunk: dict) -> list[dict]:
        events = []
        if usage := chunk.get("usage"):
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            self.usage = {"input_tokens": usage.get("prompt_tokens", 0) - cached,
                          "output_tokens": usage.get("completion_tokens", 0)}
            if cached:
                self.usage["cache_read_input_tokens"] = cached
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, list):  # Mistral reasoning models send typed chunks
                content = "".join(c.get("text", "") for c in content if c.get("type") == "text")
            if content:
                events += self.out.text(content)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if idx not in self._tool_keys or (tc.get("id") and tc["id"] != self._tool_keys[idx]):
                    self._tool_keys[idx] = tc.get("id") or f"call_{idx}"
                    events += self.out.tool_start(self._tool_keys[idx], fn.get("name", ""), key=("tool", idx))
                if fn.get("arguments"):
                    events += self.out.tool_args(fn["arguments"])
            if choice.get("finish_reason"):
                self.stop_reason = _STOP_REASONS.get(choice["finish_reason"], "end_turn")
        return events

    def finish(self) -> list[dict]:
        return self.out.finish(self.stop_reason, self.usage)
