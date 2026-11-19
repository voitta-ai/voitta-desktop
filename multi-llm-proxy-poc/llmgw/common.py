"""Helpers shared by the Chat Completions and Responses translators."""

import json
import uuid

# Prefix for the signature of thinking blocks the gateway made from OpenAI
# reasoning items. The rest of the signature is the encrypted reasoning, so
# the next turn can hand it back to OpenAI; adapters that talk to anyone
# else drop these blocks (Anthropic would reject the signature).
OAI_SIGNATURE_PREFIX = "voitta-oai:"


class Unsupported(ValueError):
    """The request needs something this account cannot do. Reported to Claude
    Code as a 400; the gateway never substitutes something else instead."""


def system_text(body: dict) -> str:
    system = body.get("system")
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text")


def image_url(block: dict) -> str | None:
    src = block.get("source", {})
    if src.get("type") == "base64":
        return f"data:{src['media_type']};base64,{src['data']}"
    if src.get("type") == "url":
        return src["url"]
    return None


def tool_result_parts(block: dict) -> tuple[str, list[str]]:
    """A tool_result's content as (text, image urls)."""
    content = block.get("content")
    texts, images = [], []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if part.get("type") == "text":
                texts.append(part["text"])
            elif part.get("type") == "image" and (url := image_url(part)):
                images.append(url)
    text = "\n".join(texts)
    if block.get("is_error"):
        text = f"Error: {text}"
    return text, images


def client_tools(body: dict) -> list[dict]:
    """Function tools. Anthropic server tools (web_search_*, ...) have a
    ``type`` and no input_schema; nothing outside Anthropic can run them."""
    tools = []
    for t in body.get("tools") or []:
        if t.get("type") not in (None, "custom") or "input_schema" not in t:
            raise Unsupported(f"tool {t.get('name') or t.get('type')!r} is an Anthropic server tool; "
                              "this account's provider cannot run it")
        schema = {k: v for k, v in t["input_schema"].items() if k != "$schema"}
        tools.append({"name": t["name"], "description": t.get("description", ""),
                      "parameters": schema})
    return tools


def portable_pattern(pattern: str) -> str:
    """The same regex in a spelling every provider's schema validator accepts.

    Claude Code's tool schemas use ``\\0`` for NUL, which DeepSeek (and
    OpenAI-style validators) reject as "not a regex". ``\\x00`` matches exactly
    the same character. Escaped backslashes (``\\\\0``) and octal-looking
    ``\\01`` are left alone."""
    out, i = [], 0
    while i < len(pattern):
        if pattern[i] == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt == "0" and not (i + 2 < len(pattern) and pattern[i + 2].isdigit()):
                out.append("\\x00")
            else:
                out.append(pattern[i:i + 2])
            i += 2
        else:
            out.append(pattern[i])
            i += 1
    return "".join(out)


def portable_tools(body: dict, notes: list) -> None:
    """Respell tool-schema regexes for non-Anthropic providers, in place,
    noting each change (shown in the request log)."""
    def walk(node, tool):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "pattern" and isinstance(v, str):
                    new = portable_pattern(v)
                    if new != v:
                        node[k] = new
                        notes.append(f"{tool}: regex {v} sent as {new}")
                else:
                    walk(v, tool)
        elif isinstance(node, list):
            for v in node:
                walk(v, tool)
    for t in body.get("tools") or []:
        walk(t.get("input_schema"), t.get("name", "?"))


def estimate_tokens(body: dict) -> int:
    """Rough count for providers without a count_tokens endpoint (~4 chars/token)."""
    return max(1, len(json.dumps(body, ensure_ascii=False)) // 4)


class AnthropicStream:
    """Builds the Anthropic stream event sequence one content block at a time.

    Each method returns the list of events to send. Blocks are identified by
    an upstream ``key`` so interleaved upstream deltas land in the right place.
    """

    def __init__(self, model: str):
        self.model = model
        self.index = -1
        self.open = None  # (block type, key)
        self.saw_tool_use = False

    def start(self) -> list[dict]:
        return [{
            "type": "message_start",
            "message": {
                "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
                "model": self.model, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }]

    def close(self) -> list[dict]:
        if self.open is None:
            return []
        self.open = None
        return [{"type": "content_block_stop", "index": self.index}]

    def _begin(self, kind: str, key, block: dict) -> list[dict]:
        events = self.close()
        self.index += 1
        self.open = (kind, key)
        return events + [{"type": "content_block_start", "index": self.index, "content_block": block}]

    def is_open(self, kind: str, key=None) -> bool:
        return self.open == (kind, key)

    def text(self, delta: str, key=None) -> list[dict]:
        events = [] if self.is_open("text", key) else self._begin("text", key, {"type": "text", "text": ""})
        return events + [{"type": "content_block_delta", "index": self.index,
                          "delta": {"type": "text_delta", "text": delta}}]

    def tool_start(self, tool_id: str, name: str, key=None) -> list[dict]:
        self.saw_tool_use = True
        return self._begin("tool_use", key, {"type": "tool_use", "id": tool_id, "name": name, "input": {}})

    def tool_args(self, partial: str) -> list[dict]:
        return [{"type": "content_block_delta", "index": self.index,
                 "delta": {"type": "input_json_delta", "partial_json": partial}}]

    def thinking_start(self, key=None) -> list[dict]:
        return self._begin("thinking", key, {"type": "thinking", "thinking": "", "signature": ""})

    def thinking(self, delta: str) -> list[dict]:
        return [{"type": "content_block_delta", "index": self.index,
                 "delta": {"type": "thinking_delta", "thinking": delta}}]

    def signature(self, sig: str) -> list[dict]:
        return [{"type": "content_block_delta", "index": self.index,
                 "delta": {"type": "signature_delta", "signature": sig}}]

    def finish(self, stop_reason: str, usage: dict) -> list[dict]:
        if stop_reason == "end_turn" and self.saw_tool_use:
            stop_reason = "tool_use"
        return self.close() + [
            {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
             "usage": usage},
            {"type": "message_stop"},
        ]
