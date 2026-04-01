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
        total = len(current)

        prev = self._prev_body.get(sid)
        if prev is None:
            prefix = 0
        else:
            prefix = _common_prefix_len(prev, current)

        sections = _find_section_offsets(current)

        self._prev_body[sid] = current

        if sid not in self.history:
            self.history[sid] = []
        self.history[sid].append({
            "total": total,
            "prefix": prefix,
            "system_offset": sections["system"],
            "tools_offset": sections["tools"],
            "messages_offset": sections["messages"],
            "msg_offsets": sections["msg_offsets"],
        })

        pct = (prefix / total * 100) if total > 0 else 0
        logger.debug("cache_sim | sid=%s turn=%d total=%d prefix=%d (%.1f%%)",
                     sid[:12], len(self.history[sid]) - 1, total, prefix, pct)

        return request


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
