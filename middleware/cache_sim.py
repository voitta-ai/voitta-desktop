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

        # Find byte offsets of each message in the serialized body
        msg_offsets = _find_msg_offsets(current)

        self._prev_body[sid] = current

        if sid not in self.history:
            self.history[sid] = []
        self.history[sid].append({
            "total": total,
            "prefix": prefix,
            "msg_offsets": msg_offsets,
        })

        pct = (prefix / total * 100) if total > 0 else 0
        logger.debug("cache_sim | sid=%s turn=%d total=%d prefix=%d (%.1f%%)",
                     sid[:12], len(self.history[sid]) - 1, total, prefix, pct)

        return request


def _find_msg_offsets(body: bytes) -> list[int]:
    """Find byte offsets of each message's {"role": marker in the serialized body."""
    marker = b'"role":'
    offsets = []
    pos = 0
    while True:
        pos = body.find(marker, pos)
        if pos < 0:
            break
        offsets.append(pos)
        pos += len(marker)
    return offsets


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
