"""ToolResultOptimizer — strips large tool results from older turns, stores them by hash.

Replaces BashOptimizer with a general-purpose version that handles all tool
results except file-access tools (Read, Write, Edit), which have their own
staleness-based optimizer.
"""

import hashlib
import json

from . import BaseOptimizer
from .image import vt_object_store
from middleware.parsing import find_tool_name

TOOL_RESULT_KEEP_TURNS = 5

# File-access tools are handled by FileReadOptimizer
_SKIP_TOOLS = frozenset({"Read", "Write", "Edit"})


def _content_hash(content) -> str:
    """Compute a short hash from tool result content (string or list)."""
    if isinstance(content, str):
        raw = content[:4096]
    else:
        raw = json.dumps(content, separators=(",", ":"))[:4096]
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _content_chars(content) -> int:
    """Count characters in a tool_result content field."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    total += len(b.get("text", ""))
                elif b.get("type") == "image":
                    total += len(b.get("source", {}).get("data", ""))
        return total
    return 0


def _placeholder(content, h: str, tool_name: str) -> str:
    """Build placeholder text that replaces removed tool result."""
    chars = _content_chars(content)
    return (
        f"[{tool_name} result removed — {chars} chars. "
        f'Use MCP tool get_vt_object(hash="{h}") to retrieve]'
    )


class ToolResultOptimizer(BaseOptimizer):
    """Strips large tool result content from older conversation turns.

    Removed content is stored by hash in vt_object_store
    and can be retrieved via the get_vt_object MCP tool.
    Must run before ImageOptimizer to prevent double-hashing of images.
    """

    chart_key = "tool_result"

    def __init__(self, keep_turns: int = TOOL_RESULT_KEEP_TURNS):
        super().__init__(keep_turns=keep_turns)

    def _optimize(self, messages: list, threshold_msg_idx: int) -> int:
        tokens_removed = 0

        for i in range(threshold_msg_idx):
            msg = messages[i]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            new_content = []
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    new_content.append(item)
                    continue

                tool_name = find_tool_name(messages, item.get("tool_use_id", ""))
                if tool_name in _SKIP_TOOLS:
                    new_content.append(item)
                    continue

                rc = item.get("content")
                if not rc:
                    new_content.append(item)
                    continue

                h = _content_hash(rc)
                placeholder = _placeholder(rc, h, tool_name)
                if _content_chars(rc) < len(placeholder) * 2:
                    new_content.append(item)
                    continue

                chars = _content_chars(rc)
                tool_use_id = item.get("tool_use_id", "")
                vt_object_store[h] = {"type": "tool_result", "data": rc}
                item = dict(item, content=placeholder)
                tokens_removed += chars // 4
                self.last_stripped_ids[tool_use_id] = chars

                new_content.append(item)

            messages[i] = dict(msg, content=new_content)

        return tokens_removed
