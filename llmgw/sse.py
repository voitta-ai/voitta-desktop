"""SSE plumbing shared by the translating adapters.

Translators turn an upstream stream into Anthropic stream events (plain
dicts). The server either writes those events to Claude Code as SSE or, for
a non-streaming request, folds them into one Message with MessageBuilder,
so every translator only has to handle the streaming case.
"""

import json

import aiohttp


async def iter_sse(resp: aiohttp.ClientResponse):
    """Yield ``(event, data)`` pairs from an upstream SSE response."""
    event, data = None, []
    async for raw in resp.content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield event, "\n".join(data)


def encode(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


_ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error",
                403: "permission_error", 404: "not_found_error",
                413: "request_too_large", 429: "rate_limit_error",
                529: "overloaded_error"}


def error_type(status: int) -> str:
    return _ERROR_TYPES.get(status, "api_error" if status >= 500 else "invalid_request_error")


def error_body(status: int, message: str) -> dict:
    return {"type": "error", "error": {"type": error_type(status), "message": message}}


class MessageBuilder:
    """Folds Anthropic stream events back into a single Message."""

    def __init__(self):
        self.message = None
        self._json_parts: dict[int, list[str]] = {}

    def feed(self, ev: dict):
        t = ev["type"]
        if t == "message_start":
            self.message = dict(ev["message"])
            self.message["content"] = []
        elif t == "content_block_start":
            block = dict(ev["content_block"])
            if block["type"] == "tool_use":
                self._json_parts[ev["index"]] = []
            self.message["content"].append(block)
        elif t == "content_block_delta":
            block, d = self.message["content"][ev["index"]], ev["delta"]
            if d["type"] == "text_delta":
                block["text"] += d["text"]
            elif d["type"] == "input_json_delta":
                self._json_parts[ev["index"]].append(d["partial_json"])
            elif d["type"] == "thinking_delta":
                block["thinking"] += d["thinking"]
            elif d["type"] == "signature_delta":
                block["signature"] = d["signature"]
        elif t == "content_block_stop":
            parts = self._json_parts.pop(ev["index"], None)
            if parts is not None:
                raw = "".join(parts)
                self.message["content"][ev["index"]]["input"] = json.loads(raw) if raw else {}
        elif t == "message_delta":
            self.message.update(ev["delta"])
            self.message["usage"] = {**self.message.get("usage", {}), **ev.get("usage", {})}

    def result(self) -> dict:
        return self.message
