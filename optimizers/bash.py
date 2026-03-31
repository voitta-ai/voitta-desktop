"""BashOptimizer — strips Bash tool outputs from older turns, stores them by hash."""

import hashlib

from . import BaseOptimizer
from .image import vt_object_store
from middleware.parsing import find_tool_name

BASH_KEEP_TURNS = 5


def _bash_hash(text: str) -> str:
    """Compute a short hash from bash output text."""
    return hashlib.sha256(text[:4096].encode()).hexdigest()[:12]


def _store_bash(text: str) -> str:
    """Store bash output in the global store and return its hash."""
    h = _bash_hash(text)
    vt_object_store[h] = {"type": "bash", "data": text}
    return h


def _bash_placeholder(text: str, h: str) -> str:
    """Build placeholder text that replaces removed bash output."""
    chars = len(text)
    return (
        f"[bash output removed — {chars} chars. "
        f'Use MCP tool get_vt_object(hash="{h}") to retrieve this output]'
    )


class BashOptimizer(BaseOptimizer):
    """Strips Bash tool result content from older conversation turns.

    Removed outputs are stored by hash in vt_object_store
    and can be retrieved via the get_vt_object MCP tool.
    """

    chart_key = "bash"

    def __init__(self, keep_turns: int = BASH_KEEP_TURNS):
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
                if tool_name != "Bash":
                    new_content.append(item)
                    continue

                rc = item.get("content", "")
                if isinstance(rc, str) and rc:
                    h = _store_bash(rc)
                    item = dict(item, content=_bash_placeholder(rc, h))
                    tokens_removed += len(rc) // 4  # rough token estimate

                new_content.append(item)

            messages[i] = dict(msg, content=new_content)

        return tokens_removed
