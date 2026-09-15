"""ToolUseOptimizer — truncates large tool-call *arguments* in older turns.

The assistant's ``tool_use`` blocks (the request side of a tool call: name +
input arguments) accumulate in context forever — Claude Code's context is
append-only and nothing strips them. In long sessions the biggest never-stripped
band is these call arguments: Bash command strings, MCP tool payloads, etc.

The block is kept — real name, real id, real field names — and long string
values inside ``input`` are cut to a short prefix. Nothing is added in their
place. The pointer to the full call goes on the USER side, as a text block
after the tool_results of the turn that answered it (the same shape Claude
Code uses for its system-reminders)::

    assistant  tool_use     Bash {"command": "grep -n saveThread src/app/api/… (120 chars)"}
    user       tool_result  <result, or the ToolResultOptimizer placeholder>
    user       text         "[voitta-proxy: truncated call above: Bash get_vt_object(hash=\\"1f3d…\\")]"

The full original block is stored by hash in ``vt_object_store``. The hash is
deterministic (id + name + input), so the rewrite is byte-identical on every
re-derivation and never re-invalidates the prompt cache.

The rule behind the layout: the proxy never writes into the assistant turn.
Anything it puts there gets imitated — three wordings of a text placeholder
were each copied by Fable 5.1 as prose, and a marker appended inside the
truncated string (" …[voitta-proxy: argument truncated, …]") was copied into
freshly written commands within minutes. User-side annotations (the
ToolResultOptimizer placeholders) have never been imitated.

Independent of ToolResultOptimizer: the pointer block is outside the
tool_result, so neither optimizer sees the other's output. File-access tools
(Read/Write/Edit) are skipped here — handled separately.
"""

import hashlib
import json

from . import BaseOptimizer
from .image import vt_object_store

# Total input size (compact JSON) at or above which a call is truncated.
TOOL_USE_REF_MIN_CHARS = 500
# Kept prefix of each long string value.
_KEEP_PREFIX = 120
# String values shorter than this are left whole.
_MIN_STRING = _KEEP_PREFIX + 160

# File tools are handled separately (see dedup study); leave their calls whole.
_SKIP_TOOLS = frozenset({"Read", "Write", "Edit", "NotebookEdit"})

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


def pointer_block(calls: list[tuple[str, str]]) -> dict:
    """User-side text block naming the hash(es) that restore the truncated
    call(s) answered in this turn. ``calls`` is ``[(tool_name, hash), …]``."""
    refs = ", ".join(f'{name} get_vt_object(hash="{h}")' for name, h in calls)
    what = "truncated call above" if len(calls) == 1 else "truncated calls above"
    return {"type": "text", "text": f"{PLACEHOLDER_TAG} {what}: {refs}]"}


def _truncate(value):
    """Cut every long string to its prefix; containers keep their shape."""
    if isinstance(value, str):
        return value[:_KEEP_PREFIX] if len(value) >= _MIN_STRING else value
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    return value


class ToolUseOptimizer(BaseOptimizer):
    """Truncates large tool-call arguments in older turns; the call stays."""

    chart_key = "tool_use"

    def __init__(self, keep_turns: int = 5, min_chars: int = TOOL_USE_REF_MIN_CHARS):
        super().__init__(keep_turns=keep_turns)
        self.min_chars = min_chars

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        tokens_removed = 0
        pointers: dict[str, tuple[str, str]] = {}   # tool_use_id -> (name, hash)

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
                        or block.get("name", "") in _SKIP_TOOLS
                        or block.get("input") is None):
                    new_content.append(block)
                    continue
                inp = block["input"]
                before = _input_chars(inp)
                truncated = _truncate(inp) if before >= self.min_chars else inp
                after = _input_chars(truncated)
                if after >= before:
                    new_content.append(block)
                    continue

                h = _call_hash(block)
                vt_object_store[h] = {"type": "tool_use", "data": block}
                new_content.append(dict(block, input=truncated))
                pointers[block.get("id", "")] = (block.get("name", ""), h)
                tokens_removed += (before - after) // 4
                self.last_stripped_ids[block.get("id", "")] = before - after

            messages[mi] = dict(msg, content=new_content)

        if not pointers:
            return tokens_removed

        # The tool_result may sit at or just past the threshold (a human
        # interruption appended to a tool_result turn makes it a turn start),
        # so scan the whole list for it.
        for mi, msg in enumerate(messages):
            content = msg.get("content")
            if msg.get("role") != "user" or not isinstance(content, list):
                continue
            here = [pointers[b["tool_use_id"]] for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") in pointers]
            if here:
                messages[mi] = dict(msg, content=content + [pointer_block(here)])

        return tokens_removed
