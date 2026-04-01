"""Cache simulator — measures byte-level prefix overlap between consecutive API requests.

Runs as the last middleware, after all optimizers, to capture the exact bytes
that would be sent to the API. Computes the longest common prefix (Anthropic's
cache model) between the current and previous request body per session.
"""

import logging

from .base import Middleware, ProxyRequest

logger = logging.getLogger("voitta-desktop.cache_sim")


class CacheSimulator(Middleware):
    """Tracks per-session request bodies and computes prefix cache overlap."""

    def __init__(self):
        # session_id -> last request body bytes
        self._prev_body: dict[str, bytes] = {}
        # session_id -> list of per-turn cache data dicts
        self.history: dict[str, list[dict]] = {}

    def get_history(self, session_id: str) -> list[dict]:
        """Return [{"total": int, "prefix": int, "msg_offsets": [int, ...]}, ...] per turn."""
        return self.history.get(session_id, [])

    async def on_request(self, request: ProxyRequest) -> ProxyRequest:
        if not request.path.startswith("/v1/messages"):
            return request
        if not request.body:
            return request

        # Skip quota-check requests (max_tokens=1)
        body = request.json
        if body and body.get("max_tokens") == 1:
            return request

        sid = request.headers.get("X-Claude-Code-Session-Id", "")
        if not sid:
            return request

        current = request.body
        body_dict = request.json

        # Reorder to system → tools → messages to match Anthropic's cache processing
        reordered, section_sizes, msg_offsets = _reorder_for_cache(body_dict)
        total = len(reordered)

        prev = self._prev_body.get(sid)
        if prev is None:
            prefix = 0
        else:
            prefix = _common_prefix_len(prev, reordered)

        self._prev_body[sid] = reordered

        if sid not in self.history:
            self.history[sid] = []
        self.history[sid].append({
            "total": total,
            "prefix": prefix,
            "system_bytes": section_sizes[0],
            "tools_bytes": section_sizes[1],
            "messages_bytes": section_sizes[2],
            "msg_offsets": msg_offsets,
        })

        pct = (prefix / total * 100) if total > 0 else 0
        logger.debug("cache_sim | sid=%s turn=%d total=%d prefix=%d (%.1f%%)",
                     sid[:12], len(self.history[sid]) - 1, total, prefix, pct)

        return request


def _normalize_system(system_bytes: bytes) -> bytes:
    """Strip volatile per-request identifiers from the system prompt.

    Claude Code embeds a cch=XXXXX hash that changes every request,
    breaking prefix matching. Normalize it to a fixed value.
    """
    import re
    return re.sub(rb'cch=[0-9a-f]+', b'cch=0', system_bytes)


def _reorder_for_cache(body: dict) -> tuple[bytes, tuple[int, int, int], list[int]]:
    """Serialize request body in Anthropic's cache order: system → tools → messages.

    Returns (reordered_bytes, (system_bytes, tools_bytes, messages_bytes), msg_offsets).
    msg_offsets are byte positions of each message's "role": marker in the reordered bytes.
    """
    import json

    system_bytes = _normalize_system(
        json.dumps(body.get("system", []), separators=(",", ":")).encode()
    )
    tools_bytes = json.dumps(body.get("tools", []), separators=(",", ":")).encode()
    messages_bytes = json.dumps(body.get("messages", []), separators=(",", ":")).encode()

    reordered = system_bytes + tools_bytes + messages_bytes

    # Find message "role": offsets within the messages portion
    msg_start = len(system_bytes) + len(tools_bytes)
    marker = b'"role":'
    msg_offsets = []
    pos = msg_start
    while True:
        pos = reordered.find(marker, pos)
        if pos < 0:
            break
        msg_offsets.append(pos)
        pos += len(marker)

    return reordered, (len(system_bytes), len(tools_bytes), len(messages_bytes)), msg_offsets


def _find_section_offsets(body: bytes) -> dict:
    """Find byte offsets of system, tools, and messages sections, plus per-message offsets.

    Returns {"system": int, "tools": int, "messages": int,
             "msg_offsets": [int, ...], "total": int}
    """
    result = {
        "system": 0,
        "tools": 0,
        "messages": 0,
        "msg_offsets": [],
        "total": len(body),
    }

    # Find top-level section starts
    for key, field in [("system", b'"system":'), ("tools", b'"tools":'), ("messages", b'"messages":')]:
        pos = body.find(field)
        if pos >= 0:
            result[key] = pos

    # Find per-message offsets within the messages array
    marker = b'"role":'
    pos = result["messages"]
    while True:
        pos = body.find(marker, pos)
        if pos < 0:
            break
        result["msg_offsets"].append(pos)
        pos += len(marker)

    return result


def _common_prefix_len(a: bytes, b: bytes) -> int:
    """Return the length of the longest common prefix between two byte strings."""
    min_len = min(len(a), len(b))
    # Compare in chunks for performance
    chunk = 4096
    matched = 0
    for offset in range(0, min_len, chunk):
        end = min(offset + chunk, min_len)
        a_chunk = a[offset:end]
        b_chunk = b[offset:end]
        if a_chunk == b_chunk:
            matched = end
        else:
            # Find exact divergence point within this chunk
            for i in range(len(a_chunk)):
                if a_chunk[i] != b_chunk[i]:
                    return matched + i
            return matched + len(a_chunk)
    return matched
