"""ToolUseOptimizer — collapses large tool-call/response *pairs* from older turns.

The assistant's ``tool_use`` blocks (the request side of a tool call: name +
input arguments) accumulate in context forever — Claude Code's context is
append-only and nothing strips them. In long sessions the biggest never-stripped
band is these call arguments: Bash command strings, MCP tool payloads, etc.

We can't reference *just* the arguments by rewriting ``tool_use.input`` to a
placeholder object — that invents a non-existent tool schema the model can learn
and imitate. Instead, when a call's arguments are large, we collapse the **whole
pair** (the ``tool_use`` block and its matching ``tool_result``) into plain text:

    assistant: [tool_use Bash {...big...}]   ->  assistant: "[voitta-proxy: Bash call … replaced …]"
    user:      [tool_result for that call]   ->  user:      "[voitta-proxy: result of the Bash call above, get_vt_object(hash)]"

Both become ordinary text blocks, so the tool_use/tool_result pairing constraint
is preserved (neither is left orphaned) and no fake tool schema is introduced.
The original ``{tool_use, tool_result}`` JSON is stored by hash in
``vt_object_store`` and retrievable via ``get_vt_object``.

The assistant-side text sits exactly where the model's own tool call used to
be, and models imitate it (Fable 5.1 wrote the old "[Referenced tool call …]"
line as prose and ended its turn instead of calling Write). So the wording
says outright that the proxy wrote it and must not be copied, and the pipeline
appends a matching system note whenever a placeholder is present
(``OptimizerPipeline.PROXY_SYSTEM_NOTE``).

Policy (composes with ToolResultOptimizer):
  * short call + short response  -> left inline (nobody touches it)
  * short call + long response   -> ToolResultOptimizer references the response
  * long call  + any response    -> THIS optimizer collapses the whole pair

File-access tools (Read/Write/Edit) are skipped here — handled separately. This
optimizer must run BEFORE ToolResultOptimizer so a long-call/long-response pair
is collapsed once (this wins) rather than the response being referenced first.
"""

import hashlib
import json

from . import BaseOptimizer
from .image import vt_object_store

TOOL_USE_REF_MIN_CHARS = 500

# File tools are handled separately (see dedup study); leave their pairs inline.
_SKIP_TOOLS = frozenset({"Read", "Write", "Edit", "NotebookEdit"})

# Every placeholder starts with this tag so the system note (see
# OptimizerPipeline.PROXY_SYSTEM_NOTE) can name it, and so the model can tell
# proxy-written text from its own. Keep the wording byte-stable: the settled
# prefix of every live session is re-derived each turn and must not drift.
PLACEHOLDER_TAG = "[voitta-proxy:"


def call_placeholder(name: str, h: str) -> str:
    """Assistant-side text that stands in for a collapsed tool_use block.

    Sits exactly where the model's own tool call used to be, so it must say
    plainly that the proxy wrote it and that the model must not imitate it.
    """
    return (f'{PLACEHOLDER_TAG} {name} call and its result were replaced here to '
            f'save context. get_vt_object(hash="{h}") restores them. Do not write '
            f'text like this; issue real tool calls.]')


def result_placeholder(name: str, h: str) -> str:
    """User-side text that stands in for the matching tool_result block."""
    return f'{PLACEHOLDER_TAG} result of the {name} call above, get_vt_object(hash="{h}")]'


def _input_chars(inp) -> int:
    if isinstance(inp, str):
        return len(inp)
    return len(json.dumps(inp, separators=(",", ":")))


def _pair_hash(tool_use: dict) -> str:
    """Stable hash of the call (id + name + input). Deterministic -> the text
    replacement is byte-identical across re-derivations, so the prompt cache is
    not re-invalidated each turn."""
    key = json.dumps(
        {"id": tool_use.get("id"), "name": tool_use.get("name"), "input": tool_use.get("input")},
        separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class ToolUseOptimizer(BaseOptimizer):
    """Collapses large tool-call/response pairs from older turns into text refs."""

    chart_key = "tool_use"

    def __init__(self, keep_turns: int = 5, min_chars: int = TOOL_USE_REF_MIN_CHARS):
        super().__init__(keep_turns=keep_turns)
        self.min_chars = min_chars

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        # Index every tool_result by tool_use_id -> (msg_idx, block_idx). We scan
        # the whole conversation: a tool_result may sit at/just past the
        # threshold boundary even when its tool_use is before it.
        result_loc: dict[str, tuple[int, int]] = {}
        for mi, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid:
                        result_loc[tid] = (mi, bi)

        tokens_removed = 0
        # Mutable copies of any message we edit (avoid touching the originals).
        edited: dict[int, list] = {}

        def _content_list(mi):
            if mi not in edited:
                edited[mi] = list(messages[mi].get("content") or [])
            return edited[mi]

        for mi in range(min(threshold_msg_idx, len(messages))):
            msg = messages[mi]
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            for bi, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                inp = block.get("input")
                if name in _SKIP_TOOLS or inp is None:
                    continue
                call_chars = _input_chars(inp)
                if call_chars < self.min_chars:
                    continue  # short call -> leave for ToolResultOptimizer

                tid = block.get("id", "")
                loc = result_loc.get(tid)
                tool_result = None
                if loc:
                    rmi, rbi = loc
                    tool_result = (edited.get(rmi, messages[rmi].get("content")))[rbi]

                h = _pair_hash(block)
                vt_object_store[h] = {
                    "type": "tool_pair",
                    "data": {"tool_use": block, "tool_result": tool_result},
                }

                # Replace the tool_use block -> assistant text.
                acontent = _content_list(mi)
                acontent[bi] = {
                    "type": "text",
                    "text": call_placeholder(name, h),
                }

                result_chars = 0
                if loc:
                    rmi, rbi = loc
                    rcontent = _content_list(rmi)
                    orig = rcontent[rbi]
                    result_chars = _input_chars(orig.get("content")) if isinstance(orig, dict) else 0
                    rcontent[rbi] = {
                        "type": "text",
                        "text": result_placeholder(name, h),
                    }

                tokens_removed += (call_chars + result_chars) // 4
                self.last_stripped_ids[tid] = call_chars + result_chars

        for mi, content in edited.items():
            # Collapsing a tool_result -> text in a user turn that also holds
            # sibling tool_results (parallel/concurrent tool calls) would leave
            # a text block ahead of a real tool_result. Anthropic requires every
            # tool_result to lead its user turn, so stable-sort tool_results to
            # the front. Assistant turns have no such constraint — leave as-is.
            if messages[mi].get("role") == "user":
                content = sorted(
                    content,
                    key=lambda b: 0 if isinstance(b, dict) and b.get("type") == "tool_result" else 1,
                )
            messages[mi] = dict(messages[mi], content=content)

        return tokens_removed
