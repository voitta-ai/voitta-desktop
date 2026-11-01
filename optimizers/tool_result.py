"""ToolResultOptimizer — strips large tool results from older turns, stores them by hash.

Skips Read/Write/Edit results: their lifecycle (file gets re-read/edited
later in the same conversation) is too coupled to "what the agent is doing
right now" for a generic strip-by-age policy to be safe. They remain
untouched in the upstream request.
"""

import hashlib
import json

from . import BaseOptimizer
from .image import vt_object_store
from middleware.parsing import find_tool_name

TOOL_RESULT_KEEP_TURNS = 5
# How much of a removed result's text its placeholder keeps. Results shorter
# than about twice the placeholder are kept whole (see _optimize), so this
# also sets which small results are never removed.
HEAD_CHARS = 400

# File-access tools are intentionally untouched (see module docstring).
_SKIP_TOOLS = frozenset({"Read", "Write", "Edit"})


def _content_hash(content) -> str:
    """Compute a short hash from tool result content (string or list).

    Hashes the whole result, not a prefix. A prefix hash collides whenever
    two results share an opening — search results, paginated listings and
    repeated command output all do — and the store is INSERT OR REPLACE, so
    a collision silently serves the wrong content back under the right hash.
    SHA-256 over a few hundred KB is far cheaper than that failure, and the
    result is still a pure function of the content, so the placeholder text
    stays byte-identical on re-derivation and the prompt cache holds.
    """
    if isinstance(content, str):
        raw = content
    else:
        raw = json.dumps(content, separators=(",", ":"))
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


def _content_text(content) -> str:
    """The text of a tool_result content field, images left out."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""

def _head(content) -> str:
    """The result's opening lines, up to HEAD_CHARS, cut at a line end.

    A placeholder that gave only the size left the model unable to tell which
    of 70 removed screen results held what it needed: it re-ran calls instead
    of fetching them (a voitta-kvm session, 2026-11-01). Tools put their
    verdict first, so the opening lines say what the call did.
    """
    text = _content_text(content).strip()
    if len(text) <= HEAD_CHARS:
        return text
    cut = text.rfind("\n", 0, HEAD_CHARS)
    return text[:cut if cut > 0 else HEAD_CHARS].rstrip() + " …"

def _placeholder(content, h: str, tool_name: str) -> str:
    """Build placeholder text that replaces removed tool result."""
    chars = _content_chars(content)
    head = _head(content)
    began = f" It began:\n{head}\n" if head else " "
    return (
        f"[{tool_name} result removed — {chars} chars.{began}"
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
