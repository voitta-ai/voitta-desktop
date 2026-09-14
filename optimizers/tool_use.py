"""ToolUseOptimizer — truncates large tool-call *arguments* in older turns.

The assistant's ``tool_use`` blocks (the request side of a tool call: name +
input arguments) accumulate in context forever — Claude Code's context is
append-only and nothing strips them. In long sessions the biggest never-stripped
band is these call arguments: Bash command strings, MCP tool payloads, etc.

The block itself is kept — real name, real id, real field names. Only long
string values inside ``input`` are cut to a short prefix plus a marker that
says the proxy truncated it and how to get the full call back::

    {"type":"tool_use","name":"Bash","id":"toolu_…",
     "input":{"command":"grep -n saveThread src/app/api/… …[voitta-proxy: argument
              truncated, get_vt_object(hash=\\"1f3d…\\") has the full call]"}}

The full original block is stored by hash in ``vt_object_store``. The hash is
deterministic (id + name + input), so the truncated block is byte-identical
on every re-derivation and never re-invalidates the prompt cache.

Why not replace the pair with text? Because any proxy prose placed in the
assistant's own turn gets imitated: three wordings of a "[voitta-proxy: …]"
note standing in for the call were each copied by Fable 5.1 as plain text —
first instead of a tool call (turn ended, work undone), then as a preamble to
real calls. With the tool_use block kept, the model's history shows tool
calls at the point where it decides what to emit, and nothing else.

Composes with ToolResultOptimizer, which references long tool *results* on
the user side; short results stay inline. File-access tools (Read/Write/Edit)
are skipped here — handled separately.
"""

import hashlib
import json

from . import BaseOptimizer
from .image import vt_object_store

# Total input size (compact JSON) at or above which a call is truncated.
TOOL_USE_REF_MIN_CHARS = 500
# Kept prefix of each long string value.
_KEEP_PREFIX = 120
# String values shorter than this are left whole (cutting them saves nothing
# once the marker is added).
_MIN_STRING = _KEEP_PREFIX + 160

# File tools are handled separately (see dedup study); leave their calls whole.
_SKIP_TOOLS = frozenset({"Read", "Write", "Edit", "NotebookEdit"})

# Marker text placed inside a truncated string. Kept byte-stable.
PLACEHOLDER_TAG = "[voitta-proxy:"


def _input_chars(inp) -> int:
    if isinstance(inp, str):
        return len(inp)
    return len(json.dumps(inp, separators=(",", ":")))


def _call_hash(tool_use: dict) -> str:
    key = json.dumps(
        {"id": tool_use.get("id"), "name": tool_use.get("name"), "input": tool_use.get("input")},
        separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def truncation_marker(h: str) -> str:
    return f' …{PLACEHOLDER_TAG} argument truncated, get_vt_object(hash="{h}") has the full call]'


def _truncate(value, marker: str):
    """Return ``value`` with every long string cut to a prefix + marker.
    Containers keep their shape; nothing else changes."""
    if isinstance(value, str):
        return value[:_KEEP_PREFIX] + marker if len(value) >= _MIN_STRING else value
    if isinstance(value, dict):
        return {k: _truncate(v, marker) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, marker) for v in value]
    return value


class ToolUseOptimizer(BaseOptimizer):
    """Truncates large tool-call arguments in older turns; the call stays."""

    chart_key = "tool_use"

    def __init__(self, keep_turns: int = 5, min_chars: int = TOOL_USE_REF_MIN_CHARS):
        super().__init__(keep_turns=keep_turns)
        self.min_chars = min_chars

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        tokens_removed = 0
        for mi in range(min(threshold_msg_idx, len(messages))):
            msg = messages[mi]
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            new_content = []
            for block in content:
                if (not isinstance(block, dict) or block.get("type") != "tool_use"
                        or block.get("name", "") in _SKIP_TOOLS):
                    new_content.append(block)
                    continue
                inp = block.get("input")
                if inp is None:
                    new_content.append(block)
                    continue
                before = _input_chars(inp)
                if before < self.min_chars:
                    new_content.append(block)
                    continue

                h = _call_hash(block)
                truncated = _truncate(inp, truncation_marker(h))
                after = _input_chars(truncated)
                if after >= before:
                    new_content.append(block)  # bulk is not in strings; leave it
                    continue

                vt_object_store[h] = {"type": "tool_use", "data": block}
                new_content.append(dict(block, input=truncated))
                saved = before - after
                tokens_removed += saved // 4
                self.last_stripped_ids[block.get("id", "")] = saved

            messages[mi] = dict(msg, content=new_content)

        return tokens_removed
